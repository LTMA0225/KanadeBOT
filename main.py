"""直流会馆 QQ 查询机器人插件（AstrBot）。

提供以下群聊指令：
- j     查询"直流会馆"店内实时在店人员，以图片 + 文本形式发送（同人同群有冷却）。
- 25h   娱乐指令，回复 25時、ナイトコードで（不设冷却）。

数据来源为直流会馆官网 /whosin 页面，账号密码与 Cookie 只经 HTTPS 发送；
图片在本机渲染：Jinja2（自动转义）生成 HTML，Playwright 驱动本机浏览器内核截图，
不调用任何第三方渲染服务。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import math
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

# 兼容 AstrBot 不同的插件加载方式：优先相对导入，失败则退回到目录导入
try:
    from .dcs_attend import call_bot_action, format_attend_reply
    from .dcs_api import (
        DEFAULT_FALLBACK_IPS,
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
        classify_users,
        fetch_whosin_users,
        format_text_message,
        public_error_text,
    )
    from .dcs_mj import (
        download_sticker,
        find_cached_sticker,
        normalize_custom_faces,
        pick_sticker,
    )
    from .dcs_presence import (
        DEFAULT_PRICING,
        calculate_charge_fen,
        fetch_pricing_html,
        fetch_visits_text,
        format_current_rate_message,
        format_fee_yuan,
        map_role,
        parse_pricing,
        parse_visits_payload,
        parse_whosin_snapshot,
        process_visit_records,
        snapshot_from_users,
        visit_key,
    )
    from .dcs_render import WhosinRenderer
    from .dcs_webhook import WebhookReceiver
except ImportError:  # pragma: no cover - 取决于运行环境
    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)
    from dcs_attend import call_bot_action, format_attend_reply
    from dcs_api import (
        DEFAULT_FALLBACK_IPS,
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
        classify_users,
        fetch_whosin_users,
        format_text_message,
        public_error_text,
    )
    from dcs_mj import (
        download_sticker,
        find_cached_sticker,
        normalize_custom_faces,
        pick_sticker,
    )
    from dcs_presence import (
        DEFAULT_PRICING,
        calculate_charge_fen,
        fetch_pricing_html,
        fetch_visits_text,
        format_current_rate_message,
        format_fee_yuan,
        map_role,
        parse_pricing,
        parse_visits_payload,
        parse_whosin_snapshot,
        process_visit_records,
        snapshot_from_users,
        visit_key,
    )
    from dcs_render import WhosinRenderer
    from dcs_webhook import WebhookReceiver

PLUGIN_NAME = "astrbot_plugin_dcs_whosin"

# 场馆时区：UTC+8（与站点的 FLT 时间一致）
FLT = timezone(timedelta(hours=8))

# aiocqhttp 适配器补丁的标记（由控制台 apply_adapter_patch.ps1 写入；后者为旧版整文件补丁）
ADAPTER_PATCH_MARKERS = ("DCS-PATCH:filter", "[[DCS 自定义过滤]]")


def _as_list(value) -> list[str]:
    """把配置值统一为字符串列表（兼容逗号分隔的字符串写法）。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    return [str(item).strip() for item in value if str(item).strip()]


def _detect_adapter_patch() -> bool | None:
    """检查 AstrBot 的 aiocqhttp 适配器补丁是否在位；无法判断时返回 None。"""
    try:
        from astrbot.core.platform.sources.aiocqhttp import (
            aiocqhttp_platform_adapter as adapter_module,
        )

        source = inspect.getsource(adapter_module)
    except Exception:  # noqa: BLE001
        return None
    return any(marker in source for marker in ADAPTER_PATCH_MARKERS)


class DcsWhosinPlugin(Star):
    """直流会馆查询机器人插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 插件所在目录，用于定位模板与素材
        self.plugin_dir = Path(__file__).resolve().parent
        self.renderer = WhosinRenderer(self.plugin_dir, self._resolve_cache_dir())
        # 插件数据目录（图片缓存与进/离店快照）
        self.plugin_data_dir = self.renderer.cache_dir.parent
        # 静默策略状态（内存级，插件重载即清零）
        self._last_by_user: dict[str, float] = {}
        self._last_notice: dict[str, float] = {}
        self._group_next_slot: dict[str, float] = {}
        # 后台任务：状态推送 / 渲染依赖补装
        self._status_task: asyncio.Task | None = None
        self._deps_task: asyncio.Task | None = None
        self._last_health: bool | None = None
        self._last_push_at = 0.0
        self._adapter_patched: bool | None = None
        # 已见过的群（首次见到时记录一条日志，便于配置白名单）
        self._seen_groups: set[str] = set()
        # 进/离店播报：快照状态、记录流状态、计费表缓存与后台任务
        self._presence: dict[str, dict] = {}
        self._presence_ready = False
        self._presence_task: asyncio.Task | None = None
        self._presence_mode = "snapshot"  # snapshot（在店快照）/ records（访问记录流）
        self._records_state: dict | None = None
        self._records_fail_count = 0
        self._records_tick_counter = 0
        self._webhook = None  # WebhookReceiver，在 initialize 中创建
        self._mj_task: asyncio.Task | None = None  # /mj 表情预取任务
        self._attend_last: dict[str, float] = {}  # @bot 自助上机冷却
        self._pricing: dict | None = None
        self._pricing_at = 0.0
        self._pricing_default_logged = False

    @staticmethod
    def _resolve_cache_dir() -> Path:
        """图片缓存放在 AstrBot 的插件数据目录（插件目录可能是只读挂载）。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "image_cache"
        except Exception:  # noqa: BLE001
            return Path(tempfile.gettempdir()) / PLUGIN_NAME / "image_cache"

    async def initialize(self) -> None:
        """插件加载后检查适配器补丁，并启动状态推送后台任务。"""
        self._adapter_patched = _detect_adapter_patch()
        if self._adapter_patched is False:
            logger.warning(
                "[dcs_whosin] 未检测到 aiocqhttp 适配器补丁（AstrBot 升级后会被覆盖）。"
                "群锁仍由 AstrBot 白名单与插件 allowed_groups 保证；"
                "如需恢复，请在控制台选择「重新打 AstrBot 补丁」。"
            )
        elif self._adapter_patched:
            logger.info("[dcs_whosin] aiocqhttp 适配器补丁已生效。")

        if self._deps_task is None or self._deps_task.done():
            self._deps_task = asyncio.create_task(self._ensure_render_dependencies())
        self._load_presence_state()
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self._presence_loop())
            logger.info("[dcs_whosin] 进离店播报任务已启动。")
        if self._webhook is None:
            self._webhook = WebhookReceiver(
                str(self.config.get("webhook_bind", "0.0.0.0") or "0.0.0.0"),
                int(self.config.get("webhook_port", 8765) or 8765),
                str(self.config.get("webhook_secret", "") or ""),
                self._on_webhook_event,
            )
        if self.config.get("webhook_enable", False):
            await self._webhook.start()
        else:
            logger.info("[dcs_whosin] Webhook 接收服务未启用（webhook_enable=false）")
        if self.config.get("mj_enable", True):
            if self._mj_task is None or self._mj_task.done():
                self._mj_task = asyncio.create_task(self._mj_prefetch())
        if self._status_task and not self._status_task.done():
            return
        self._status_task = asyncio.create_task(self._status_loop())
        logger.info("[dcs_whosin] 状态推送任务已启动。")

    async def _ensure_render_dependencies(self) -> None:
        """渲染依赖缺失时，用 AstrBot 自带的 pip 安装器在后台补装。

        渲染依赖是按需导入的，缺失时插件照常加载（只发文字版），
        因此 AstrBot 不会自动安装它们；这里主动补装，装好后下一次查询即恢复出图。
        """
        if not self.config.get("enable_image", True):
            return
        missing = [name for name in ("jinja2", "playwright") if importlib.util.find_spec(name) is None]
        if not missing:
            return
        try:
            from astrbot.core import pip_installer
        except Exception:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 缺少图片渲染依赖 {missing}，请在 AstrBot 插件页为本插件安装依赖。")
            return
        logger.info(f"[dcs_whosin] 正在后台安装图片渲染依赖 {missing}（装好前只发文字版）……")
        try:
            await pip_installer.install(requirements_path=str(self.plugin_dir / "requirements.txt"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 渲染依赖安装失败：{exc}；可在 AstrBot 插件页手动安装依赖。")
            return
        importlib.invalidate_caches()
        logger.info("[dcs_whosin] 图片渲染依赖安装完成，下一次查询起恢复出图。")

    # ------------------------------------------------------------------
    # 状态推送
    # ------------------------------------------------------------------
    async def _status_loop(self) -> None:
        """按配置间隔检查站点状态，并按推送模式通知维护者。"""
        await asyncio.sleep(20)
        first = True
        while True:
            try:
                if self.config.get("status_push_enable", False):
                    await self._status_tick(first)
                    first = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dcs_whosin] 状态推送异常：{exc}")
            interval = int(self.config.get("status_push_interval_seconds", 600) or 600)
            await asyncio.sleep(max(interval, 60))

    async def _status_tick(self, first: bool) -> None:
        """检查一次站点状态；smart 模式只在启动、异常/恢复与心跳时推送。"""
        mode = str(self.config.get("status_push_mode", "smart") or "smart")
        result = await self._fetch_data()
        healthy = not result.error
        if healthy:
            groups = classify_users(result.users)
            venue_line = (
                f"在店：{len(groups.players) + len(groups.staff_line)} 人"
                f"（玩家 {len(groups.players)} / 管理 {len(groups.staff_line)}）"
            )
        else:
            venue_line = f"站点：异常（{public_error_text(result.error_kind)}）"
            logger.warning(f"[dcs_whosin] 状态检查失败：{result.error}")

        event_name = None
        if mode == "interval":
            event_name = "定时状态"
        elif first:
            event_name = "机器人已启动"
        elif self._last_health is not None and healthy != self._last_health:
            event_name = "站点恢复正常" if healthy else "站点访问异常"
        else:
            heartbeat_hours = float(self.config.get("status_heartbeat_hours", 24) or 0)
            if heartbeat_hours > 0 and time.monotonic() - self._last_push_at >= heartbeat_hours * 3600:
                event_name = "例行心跳"
        self._last_health = healthy
        if event_name is None:
            return

        patch_text = {True: "已生效", False: "未检测到（升级 AstrBot 后需重新打补丁）"}.get(
            self._adapter_patched, "未知"
        )
        lines = [
            "【会馆机器人·状态】",
            f"时间：{datetime.now(FLT).strftime('%m-%d %H:%M')}",
            f"事件：{event_name}",
            "运行：在线正常",
            venue_line,
        ]
        if not healthy and result.error:
            # 详情只推送给维护者，群内不展示
            lines.append(f"详情：{result.error[:200]}")
        if first or self._adapter_patched is False:
            lines.append(f"适配器补丁：{patch_text}")
        await self._push_status("\n".join(lines))
        self._last_push_at = time.monotonic()

    async def _push_status(self, text: str) -> None:
        """把状态文本推送到所有目标会话。"""
        for target in _as_list(self.config.get("status_push_targets")):
            try:
                ok = await self.context.send_message(target, MessageChain([Plain(text)]))
                if not ok:
                    logger.warning(f"[dcs_whosin] 状态推送目标不存在：{target}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dcs_whosin] 状态推送失败（{target}）：{exc}")

    # ------------------------------------------------------------------
    # 指令：j
    # ------------------------------------------------------------------
    @filter.command("j")
    async def query_whosin(self, event: AstrMessageEvent):
        """查询直流会馆店内实时人员，以图片 + 文本形式发送"""
        if not self._policy_allows(event):
            return

        allowed, notice = self._check_cooldown(event)
        if not allowed:
            if notice:
                yield event.plain_result(notice)
            return
        await self._wait_group_slot(event)

        try:
            result = await self._fetch_data()

            # 抓取失败：群内只给通用提示，详细原因写日志
            if result.error:
                logger.warning(f"[dcs_whosin] 查询失败：{result.error}")
                yield event.plain_result(f"查询失败：{public_error_text(result.error_kind)}")
                return

            groups = classify_users(result.users)

            # 重要规则：玩家数量为 0（只有管理员/士大夫或完全无人）时，
            # 不发送空信息，统一发送指定提示文案
            if not groups.players:
                yield event.plain_result(EMPTY_PLAYER_MESSAGE)
                return

            text = format_text_message(groups)
            if not self.config.get("enable_image", True):
                yield event.plain_result(text)
                return

            image_path = await self._render_image(groups, result.source_url)
            if image_path:
                # 图片 + 文本合并为一条消息发送
                yield event.chain_result(
                    [
                        Plain(text),
                        Image.fromFileSystem(image_path),
                    ]
                )
            else:
                # 图片渲染失败时降级为纯文本（附提示，避免"未返图"的困惑）
                yield event.plain_result(text + "\n\n（注：图片渲染暂时失败，本次为文字版）")
        except Exception as exc:  # noqa: BLE001 - 兜底，保证插件不因单次查询崩溃
            logger.error(f"[dcs_whosin] 查询在店人员时发生异常：{exc!r}")
            yield event.plain_result("查询失败，请稍后再试。")

    # ------------------------------------------------------------------
    # 指令：25h
    # ------------------------------------------------------------------
    @filter.command("25h")
    async def nightcord(self, event: AstrMessageEvent):
        """娱乐指令：回复 25時、ナイトコードで（不设冷却）"""
        if not self._policy_allows(event):
            return
        yield event.plain_result("25時、ナイトコードで")

    # ------------------------------------------------------------------
    # 指令：jg —— 本时段每小时游玩价格
    # ------------------------------------------------------------------
    @filter.command("jg")
    async def current_price(self, event: AstrMessageEvent):
        """查询本时段每小时游玩价格（含折扣与封顶说明，不设冷却）"""
        if not self._policy_allows(event):
            return
        try:
            pricing = await self._get_pricing("")
            prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
            text = format_current_rate_message(pricing, prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 时段价格查询失败：{exc!r}")
            text = None
        if text:
            yield event.plain_result(text)
        else:
            yield event.plain_result("价格查询失败，请稍后再试。")

    # ------------------------------------------------------------------
    # 指令：mj —— 发送 bot 收藏的表情包
    # ------------------------------------------------------------------
    @filter.command("mj")
    async def mj_sticker(self, event: AstrMessageEvent):
        """发送 bot 收藏的表情包（不设冷却；受同群最小间隔约束）"""
        if not self._policy_allows(event):
            return
        if not self.config.get("mj_enable", True):
            return
        await self._wait_group_slot(event)
        index = self._mj_index()
        path = find_cached_sticker(self.plugin_data_dir, index)
        if path is None:
            bot = getattr(event, "bot", None)
            path = await self._fetch_and_cache_sticker(bot, index)
        if path:
            yield event.chain_result([Image.fromFileSystem(str(path))])
        else:
            yield event.plain_result("表情包还没准备好，稍后再试试～")

    # ------------------------------------------------------------------
    # 指令：mjlist —— 列出收藏表情编号（用于选择 mj_sticker_index）
    # ------------------------------------------------------------------
    @filter.command("mjlist")
    async def mj_list(self, event: AstrMessageEvent):
        """列出 bot 收藏的表情（编号 + 图片，方便选择 mj_sticker_index）"""
        if not self._policy_allows(event):
            return
        if not self.config.get("mj_enable", True):
            return
        bot = getattr(event, "bot", None)
        urls = await self._fetch_custom_faces(bot)
        if not urls:
            yield event.plain_result("没有读取到收藏表情（该 QQ 号的收藏表情为空或接口暂不可用）")
            return
        for i, url in enumerate(urls[:12]):
            path = find_cached_sticker(self.plugin_data_dir, i)
            if path is None:
                path = await download_sticker(url, self.plugin_data_dir, i)
            if path:
                yield event.chain_result([Plain(f"第 {i} 个"), Image.fromFileSystem(str(path))])
                await asyncio.sleep(1)
        if len(urls) > 12:
            yield event.plain_result(f"（收藏共 {len(urls)} 个，仅显示前 12 个）")

    # ------------------------------------------------------------------
    # 指令：@bot 自助离店（按 QQ 绑定自动结算）
    # 说明：为避免"人未到店就被远程进店"的风险，不支持机器人进店（进店请在网站操作）。
    # ------------------------------------------------------------------
    @filter.command("离店")
    async def attend_leave(self, event: AstrMessageEvent):
        """@机器人 离店 —— 自动离店并结算"""
        if not self._policy_allows(event):
            return
        if not self.config.get("attend_enable", True):
            return
        text = await self._attend_action(event, "leave")
        if text:
            yield event.plain_result(text)

    # ------------------------------------------------------------------
    # 静默策略：私聊开关 / 群白名单 / 频率限制
    # ------------------------------------------------------------------
    def _policy_allows(self, event: AstrMessageEvent) -> bool:
        """私聊只看 allow_private_chat，群聊只看 allowed_groups；不放行时静默忽略。"""
        if event.is_private_chat():
            return bool(self.config.get("allow_private_chat", False))

        group_id = str(event.get_group_id() or "")
        if not group_id:
            return False
        if group_id not in self._seen_groups:
            self._seen_groups.add(group_id)
            logger.info(
                f"[dcs_whosin] 首次收到群消息：group={group_id} 平台={event.get_platform_name()}"
            )
        allowed_groups = _as_list(self.config.get("allowed_groups"))
        return not allowed_groups or group_id in allowed_groups

    def _check_cooldown(self, event: AstrMessageEvent) -> tuple[bool, str | None]:
        """/j 冷却：同一用户在同一群 cooldown_seconds 内只能查一次，返回 (是否放行, 提示文本)。

        冷却期内只提示一次，之后静默，避免刷屏。
        """
        cooldown = int(self.config.get("cooldown_seconds", 60) or 0)
        if cooldown <= 0:
            return True, None

        now = time.monotonic()
        user_key = f"{event.get_group_id()}:{event.get_sender_id() or 'unknown'}"
        last = self._last_by_user.get(user_key)
        if last is not None and now - last < cooldown:
            notice_last = self._last_notice.get(user_key)
            if notice_last is None or now - notice_last >= cooldown:
                self._last_notice[user_key] = now
                remain = int(cooldown - (now - last)) + 1
                return False, f"查询太频繁了，请 {remain} 秒后再试。"
            return False, None

        self._last_by_user[user_key] = now
        if len(self._last_by_user) > 1000:  # 定期清理过期记录，防止常驻内存增长
            for store in (self._last_by_user, self._last_notice):
                for key in [k for k, t in store.items() if now - t >= cooldown]:
                    store.pop(key, None)
        return True, None

    async def _wait_group_slot(self, event: AstrMessageEvent) -> None:
        """同群回复最小间隔：为本次回复预约发送时刻，并发请求依次排队。"""
        group_id = str(event.get_group_id() or "")
        interval = float(self.config.get("group_interval_seconds", 5) or 0)
        if not group_id or interval <= 0:
            return
        now = time.monotonic()
        slot = max(now, self._group_next_slot.get(group_id, 0.0))
        self._group_next_slot[group_id] = slot + interval
        if slot > now:
            await asyncio.sleep(slot - now)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    async def _fetch_data(self):
        """按插件配置抓取在店数据。"""
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):  # 兼容用户把地址配置成单个字符串的情况
            urls = [urls]
        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)
        cookie = str(self.config.get("cookie", "") or "")
        username = str(self.config.get("login_username", "") or "")
        password = str(self.config.get("login_password", "") or "")
        return await fetch_whosin_users(
            urls=urls,
            timeout=timeout,
            cookie=cookie,
            username=username,
            password=password,
            fallback_ips=fallback_ips,
        )

    async def _render_image(self, groups, source_url: str) -> str | None:
        """在本机渲染在店一览图片，返回 PNG 路径；渲染失败时返回 None。"""
        now = datetime.now(FLT)
        ttl = int(self.config.get("cache_ttl_seconds", 30) or 0)
        theme = str(self.config.get("image_theme", "cyberpunk-bw") or "cyberpunk-bw")

        payload = {
            "time_text": now.strftime("%Y-%m-%d %H:%M"),
            "weekday_text": "周" + "一二三四五六日"[now.weekday()],
            "players": groups.players,
            "staff_line": groups.staff_line,
            "player_count": len(groups.players),
            "staff_count": len(groups.staff_line),
            "total_count": len(groups.players) + len(groups.staff_line),
            "source_host": source_url.split("//")[-1].split("/")[0] if source_url else "",
        }
        return await self.renderer.render(
            payload,
            theme=theme,
            ttl=ttl,
            browser=str(self.config.get("render_browser", "auto") or "auto"),
            browser_path=str(self.config.get("render_browser_path", "") or ""),
            timeout=float(self.config.get("render_timeout_seconds", 20) or 20),
        )

    # ------------------------------------------------------------------
    # 进/离店播报
    # ------------------------------------------------------------------
    def _presence_state_file(self) -> Path:
        return self.plugin_data_dir / "presence_state.json"

    def _load_presence_state(self) -> None:
        """插件启动时恢复进/离店状态（统一去重表 + 在店快照）。"""
        try:
            path = self._presence_state_file()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(data.get("saved_at") or 0)
            seen = data.get("seen")
            if isinstance(seen, dict):
                self._records_state = {"seen": seen}
            # 兼容旧版本：把 recent_exits 迁移为统一去重表的离店标记
            legacy_exits = data.get("recent_exits")
            if isinstance(legacy_exits, dict):
                ledger = self._ledger_seen()
                for k, v in legacy_exits.items():
                    rec = ledger.get(str(k)) if isinstance(ledger.get(str(k)), dict) else {}
                    rec["x"] = 1
                    rec.setdefault("e", 1)
                    try:
                        rec["at"] = int(float(v))
                    except (TypeError, ValueError):
                        pass
                    ledger[str(k)] = rec
            if str(data.get("mode") or "snapshot") == "records":
                if isinstance(seen, dict):
                    self._presence_mode = "records"
                    logger.info(f"[dcs_whosin] 已恢复进离店去重状态（{len(seen)} 条）")
                return
            ttl_minutes = int(self.config.get("presence_state_ttl_minutes", 15) or 0)
            users = data.get("users")
            if (
                ttl_minutes > 0
                and time.time() - saved_at <= ttl_minutes * 60
                and isinstance(users, dict)
                and users
            ):
                self._presence = {k: v for k, v in users.items() if isinstance(v, dict)}
                self._presence_ready = True
                age_min = max(0, int((time.time() - saved_at) / 60))
                logger.info(f"[dcs_whosin] 已恢复进离店快照（{len(self._presence)} 人，约 {age_min} 分钟前）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 进离店状态恢复失败：{exc!r}")

    def _save_presence_state(self) -> None:
        try:
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            path = self._presence_state_file()
            temp = path.with_suffix(".tmp")
            payload = {
                "mode": self._presence_mode,
                "saved_at": time.time(),
                "seen": self._ledger_seen(),
            }
            if self._presence_mode != "records":
                payload["users"] = self._presence
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 进离店状态保存失败：{exc!r}")

    async def _presence_loop(self) -> None:
        """按配置间隔轮询站点，播报玩家进店 / 离店（含时长与扣费）。"""
        await asyncio.sleep(15)
        while True:
            interval = 60
            try:
                interval = int(self.config.get("presence_check_interval_seconds", 60) or 60)
                interval = min(max(interval, 15), 3600)
                if self.config.get("presence_notify_enable", False):
                    await self._presence_tick(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dcs_whosin] 进离店播报异常：{exc!r}")
            await asyncio.sleep(interval)

    async def _presence_tick(self, interval: int) -> None:
        """进离店检查：优先访问记录流（无盲区），不可用时自动退回在店快照。"""
        if self._presence_mode == "records":
            if await self._presence_tick_records(interval):
                return
            if self._records_fail_count < 5:
                return  # 短暂故障：等下一轮立即重试，避免与快照重复播报
            self._presence_mode = "snapshot"
            self._presence_ready = False
            logger.warning(
                "[dcs_whosin] 访问记录接口持续不可用，已退回在店快照模式（每 10 分钟自动重试记录接口）"
            )
        else:
            self._records_tick_counter += 1
            if self._records_fail_count == 0 or self._records_tick_counter % 20 == 0:
                if await self._presence_tick_records(interval):
                    return
        await self._presence_tick_snapshot(interval)

    async def _presence_tick_records(self, interval: int) -> bool:
        """用站点访问记录流做进离店播报（无盲区、扣费为真实结算金额）；失败返回 False。"""
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):
            urls = [urls]
        base_url = str(self.config.get("presence_visits_url", "") or "").strip()
        if not base_url:
            base = urls[0] if urls else DEFAULT_TARGET_URLS[0]
            parts = urlsplit(base)
            base_url = f"{parts.scheme}://{parts.netloc}/api/visits/recent"
        url = f"{base_url}{'&' if '?' in base_url else '?'}limit=200"
        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)
        text = None
        try:
            text = await fetch_visits_text(
                [url],
                fallback_ips,
                timeout,
                str(self.config.get("cookie", "") or ""),
                str(self.config.get("login_username", "") or ""),
                str(self.config.get("login_password", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 访问记录接口请求失败：{exc!r}")
        visits = parse_visits_payload(text) if text else None
        if visits is None:
            self._records_fail_count += 1
            if self._records_fail_count == 1 and self._presence_mode != "records":
                logger.info(
                    "[dcs_whosin] 站点访问记录接口暂不可用，继续使用在店快照模式"
                    "（站点端接口上线后会自动切换，无需重启）"
                )
            return False
        self._records_fail_count = 0
        if self._presence_mode != "records":
            self._presence_mode = "records"
            logger.info("[dcs_whosin] 已启用访问记录模式（进离店无盲区，扣费为站点实际结算金额）")
        self._ledger_seen()  # 确保统一去重表已初始化
        now_ms = int(time.time() * 1000)
        fresh_minutes = int(self.config.get("presence_record_fresh_minutes", 10) or 10)
        prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
        pricing = await self._get_pricing("")
        messages, new_state = process_visit_records(
            self._records_state, visits, now_ms, fresh_minutes, prefix, pricing
        )
        self._records_state = new_state
        self._save_presence_state()
        await self._broadcast_presence(messages)
        return True

    def _ledger_seen(self) -> dict:
        """统一去重表：{"用户ID:进店毫秒": {"e": 进店已播, "x": 离店已播, "at": 进店时间}}。

        Webhook、访问记录流与在店快照三种通道共用，确保任何事件只播报一次。
        """
        if self._records_state is None:
            self._records_state = {"seen": {}}
        seen = self._records_state.get("seen")
        if not isinstance(seen, dict):
            seen = {}
            self._records_state["seen"] = seen
        return seen

    def _ledger_get(self, key) -> dict:
        if not key:
            return {}
        record = self._ledger_seen().get(key)
        return record if isinstance(record, dict) else {}

    def _ledger_mark(self, key, e=None, x=None, at=None) -> None:
        if not key:
            return
        seen = self._ledger_seen()
        record = seen.get(key) if isinstance(seen.get(key), dict) else {}
        if e is not None:
            record["e"] = 1 if e else 0
        if x is not None:
            record["x"] = 1 if x else 0
        if at is not None:
            try:
                record["at"] = int(at)
            except (TypeError, ValueError):
                pass
        seen[key] = record

    async def _on_webhook_event(self, payload: dict) -> None:
        """处理站点 webhook 事件：进店/离店实时播报（与轮询通道共用统一去重表）。"""
        if not self.config.get("presence_notify_enable", False):
            return
        event = str(payload.get("event") or "").strip().lower()
        if event not in ("enter", "leave"):
            return
        name = str(payload.get("nickname") or "").strip()
        role = map_role(payload.get("role"))
        uid = payload.get("userId")
        entered = payload.get("enteredAt")
        if not name or role != "player" or uid is None or entered is None:
            return
        try:
            entered_ms = int(entered)
        except (TypeError, ValueError):
            return

        key = visit_key(uid, entered_ms)
        now_ms = int(time.time() * 1000)
        fresh_minutes = int(self.config.get("presence_record_fresh_minutes", 10) or 10)
        fresh_ms = max(1, fresh_minutes) * 60000
        prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
        record = self._ledger_get(key)
        messages: list[str] = []

        if event == "enter":
            if not record.get("e"):
                if now_ms - entered_ms <= fresh_ms:
                    messages.append(f"{prefix}{name}进店了")
                self._ledger_mark(key, e=1, at=entered_ms)
        else:
            left = payload.get("leftAt")
            try:
                left_ms = int(left) if left is not None else now_ms
            except (TypeError, ValueError):
                left_ms = now_ms
            if not record.get("e"):
                if now_ms - entered_ms <= fresh_ms:
                    messages.append(f"{prefix}{name}进店了")
            if not record.get("x"):
                if now_ms - left_ms <= fresh_ms:
                    charge = payload.get("charge")
                    if charge is None:
                        try:
                            multiplier = float(payload.get("chargeMultiplier")) if payload.get("chargeMultiplier") is not None else 1.0
                        except (TypeError, ValueError):
                            multiplier = 1.0
                        try:
                            pass_multiplier = float(payload.get("passMultiplier")) if payload.get("passMultiplier") is not None else 1.0
                        except (TypeError, ValueError):
                            pass_multiplier = 1.0
                        pricing = await self._get_pricing("")
                        charge = calculate_charge_fen(
                            entered_ms, left_ms, pricing, abs(multiplier) * abs(pass_multiplier)
                        )
                    minutes = max(0, math.ceil((left_ms - entered_ms) / 60000))
                    messages.append(
                        f"{prefix}{name}离店了\n"
                        f"游玩{minutes}分钟\n"
                        f"扣费{format_fee_yuan(charge or 0)}元（线上余额）"
                    )
            self._ledger_mark(key, e=1, x=1, at=entered_ms)

        self._save_presence_state()
        if messages:
            logger.info(f"[dcs_whosin] Webhook({event}) 已播报 {name}")
        await self._broadcast_presence(messages)

    async def _presence_tick_snapshot(self, interval: int) -> None:
        result = await self._fetch_data()
        if result.error or not result.raw_html:
            logger.warning(f"[dcs_whosin] 进离店检查失败：{result.error_kind or '未获取到页面'}")
            return
        snapshot = parse_whosin_snapshot(result.raw_html)
        if snapshot is None:
            snapshot = snapshot_from_users(result.users)
        now_ms = int(time.time() * 1000)
        current = {item["key"]: dict(item, last_seen_ms=now_ms) for item in snapshot}

        if not self._presence_ready:
            self._presence = current
            self._presence_ready = True
            self._save_presence_state()
            logger.info(f"[dcs_whosin] 进离店基线已建立（{len(current)} 人，不播报存量玩家）")
            return

        previous = self._presence
        entries = [
            user for key, user in current.items()
            if key not in previous and user.get("role") == "player"
        ]
        exits = [
            user for key, user in previous.items()
            if key not in current and user.get("role") == "player"
        ]

        prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
        messages: list[str] = []
        for user in entries:
            key = visit_key(user.get("id"), user.get("entered_ms"))
            record = self._ledger_get(key)
            if record.get("e"):
                continue  # Webhook / 记录流已播报过进店
            messages.append(f"{prefix}{user.get('name', '')}进店了")
            self._ledger_mark(key, e=1, at=user.get("entered_ms"))
        for user in exits:
            entered_ms = user.get("entered_ms")
            if not entered_ms:
                logger.warning(f"[dcs_whosin] {user.get('name', '')} 离店但缺少入店时间，跳过播报")
                continue
            key = visit_key(user.get("id"), entered_ms)
            record = self._ledger_get(key)
            if record.get("x"):
                continue  # Webhook / 记录流已播报过离店
            last_seen = int(user.get("last_seen_ms") or now_ms)
            gap = max(0, now_ms - last_seen)
            # 真实离店发生在（最后在店, 本次发现]之间，取中点降低误差
            settle_ms = last_seen + min(gap, interval * 1000) // 2
            # 游玩时长向上取整（不足 1 分钟按 1 分钟计）
            minutes = max(0, math.ceil((settle_ms - int(entered_ms)) / 60000))
            pricing = await self._get_pricing(result.source_url)
            try:
                multiplier = float(user.get("multiplier")) if user.get("multiplier") is not None else 1.0
            except (TypeError, ValueError):
                multiplier = 1.0
            fee_fen = calculate_charge_fen(int(entered_ms), settle_ms, pricing, multiplier)
            messages.append(
                f"{prefix}{user.get('name', '')}离店了\n"
                f"游玩{minutes}分钟\n"
                f"扣费{format_fee_yuan(fee_fen)}元（线上余额）"
            )
            self._ledger_mark(key, e=1, x=1, at=int(entered_ms))

        self._presence = current
        self._save_presence_state()
        await self._broadcast_presence(messages)

    async def _broadcast_presence(self, messages: list[str]) -> None:
        if not messages:
            return
        targets = _as_list(self.config.get("presence_targets"))
        if not targets:
            # 未配置时退回会馆群（平台 id 与状态推送约定一致）
            targets = [f"napcat:GroupMessage:{gid}" for gid in _as_list(self.config.get("allowed_groups"))]
        if not targets:
            logger.warning("[dcs_whosin] 进离店播报未配置目标，已跳过")
            return
        gap = float(self.config.get("presence_min_gap_seconds", 2) or 0)
        for text in messages:
            for target in targets:
                try:
                    ok = await self.context.send_message(target, MessageChain([Plain(text)]))
                    if not ok:
                        logger.warning(f"[dcs_whosin] 进离店播报目标不存在：{target}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[dcs_whosin] 进离店播报失败（{target}）：{exc!r}")
            logger.info(f"[dcs_whosin] 进离店播报：{text}")
            if gap > 0:
                await asyncio.sleep(gap)

    async def _get_pricing(self, source_url: str) -> dict:
        """获取站点计费表（缓存 presence_pricing_refresh_hours 小时；失败用旧表/内置表）。"""
        now = time.monotonic()
        ttl = max(1, int(self.config.get("presence_pricing_refresh_hours", 6) or 6)) * 3600
        if self._pricing and now - self._pricing_at < ttl:
            return self._pricing

        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):
            urls = [urls]
        price_url = str(self.config.get("presence_pricing_url", "") or "").strip()
        if not price_url:
            base = urls[0] if urls else (source_url or DEFAULT_TARGET_URLS[0])
            parts = urlsplit(base)
            price_url = f"{parts.scheme}://{parts.netloc}/chargecalc"

        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)
        html = None
        try:
            html = await fetch_pricing_html(
                [price_url],
                fallback_ips,
                timeout,
                str(self.config.get("cookie", "") or ""),
                str(self.config.get("login_username", "") or ""),
                str(self.config.get("login_password", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 计费表抓取失败：{exc!r}")

        pricing = parse_pricing(html) if html else None
        if pricing:
            self._pricing = pricing
            self._pricing_at = now
            return pricing
        if self._pricing:
            self._pricing_at = now  # 沿用旧表，下轮再试
            return self._pricing
        if not self._pricing_default_logged:
            self._pricing_default_logged = True
            logger.warning("[dcs_whosin] 未能获取站点计费表，暂用内置默认费率（自动重试中）")
        self._pricing = DEFAULT_PRICING
        self._pricing_at = now - ttl // 2
        return self._pricing

    # ------------------------------------------------------------------
    # /mj 收藏表情辅助
    # ------------------------------------------------------------------
    def _platform_id(self) -> str:
        for key in ("presence_targets", "status_push_targets"):
            for target in _as_list(self.config.get(key)):
                if ":" in target:
                    return target.split(":", 1)[0]
        return "napcat"

    def _mj_index(self) -> int:
        try:
            index = int(self.config.get("mj_sticker_index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        return max(0, index)

    async def _fetch_custom_faces(self, bot) -> list[str]:
        if bot is None:
            return []
        try:
            data = await bot.call_action("fetch_custom_face")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 获取收藏表情失败：{exc!r}")
            return []
        return normalize_custom_faces(data)

    async def _fetch_and_cache_sticker(self, bot, index: int):
        urls = await self._fetch_custom_faces(bot)
        url = pick_sticker(urls, index)
        if not url:
            logger.warning("[dcs_whosin] /mj 收藏表情列表为空或索引越界")
            return None
        path = await download_sticker(url, self.plugin_data_dir, index)
        if path:
            logger.info(f"[dcs_whosin] /mj 表情包已缓存（第 {index} 个，收藏共 {len(urls)} 个）")
        else:
            logger.warning("[dcs_whosin] /mj 表情包下载失败（链接可能已过期，稍后重试）")
        return path

    async def _mj_prefetch(self) -> None:
        """插件启动后预取并缓存 /mj 表情；日志会显示收藏数量便于核对。"""
        await asyncio.sleep(25)
        try:
            index = self._mj_index()
            if find_cached_sticker(self.plugin_data_dir, index):
                return
            platform = self.context.get_platform_inst(self._platform_id())
            bot = getattr(platform, "bot", None) if platform is not None else None
            if bot is None and platform is not None and hasattr(platform, "get_client"):
                try:
                    bot = platform.get_client()
                except Exception:  # noqa: BLE001
                    bot = None
            if bot is None:
                logger.info("[dcs_whosin] /mj 预取：napcat 平台未就绪，将在首次使用时获取")
                return
            await self._fetch_and_cache_sticker(bot, index)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] /mj 预取失败：{exc!r}")

    # ------------------------------------------------------------------
    # @bot 自助上机
    # ------------------------------------------------------------------
    async def _attend_action(self, event: AstrMessageEvent, action: str) -> str:
        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            return "无法识别你的 QQ 号"
        now = time.monotonic()
        cooldown = int(self.config.get("attend_cooldown_seconds", 30) or 0)
        last = self._attend_last.get(qq, 0.0)
        if cooldown > 0 and now - last < cooldown:
            remain = int(cooldown - (now - last)) + 1
            return f"操作太频繁，请 {remain} 秒后再试"
        self._attend_last[qq] = now
        if len(self._attend_last) > 500:
            cutoff = now - max(cooldown, 60) * 2
            self._attend_last = {k: v for k, v in self._attend_last.items() if v >= cutoff}

        token = str(self.config.get("webhook_secret", "") or "")
        if not token:
            return "机器人还没配置站点令牌，请联系维护者"
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):
            urls = [urls]
        base = urls[0] if urls else DEFAULT_TARGET_URLS[0]
        parts = urlsplit(base)
        action_url = f"{parts.scheme}://{parts.netloc}/api/bot/action"
        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)

        result = None
        try:
            result = await call_bot_action([action_url], fallback_ips, timeout, token, action, qq)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] @bot 自助操作请求失败：{exc!r}")

        prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
        text, meta = format_attend_reply(action, result, prefix)
        if meta and meta.get("key"):
            self._ledger_mark(
                meta.get("key"),
                e=1 if meta.get("e") else None,
                x=1 if meta.get("x") else None,
                at=meta.get("at"),
            )
            self._save_presence_state()
        first_line = text.splitlines()[0] if text else ""
        logger.info(f"[dcs_whosin] @bot 自助（{action}）QQ={qq}：{first_line}")
        return text

    async def terminate(self):
        """插件被卸载 / 停用时停止后台任务与 Webhook 服务，关闭本地浏览器并清理图片缓存。"""
        if self._webhook is not None:
            await self._webhook.stop()
        for task in (self._status_task, self._deps_task, self._presence_task, self._mj_task):
            if task and not task.done():
                task.cancel()
        await self.renderer.close()
        logger.info("[dcs_whosin] 插件已停用，本地浏览器与图片缓存已清理。")
