"""直流会馆 QQ 查询机器人插件（AstrBot）。

提供以下群聊指令：
- j     查询"直流会馆"店内实时在店人员，以图片 + 文本形式发送。
- 25h   娱乐指令，回复 25時、ナイトコードで。

数据来源为直流会馆官网 /whosin 页面；图片渲染使用 AstrBot 内置的
html_render（底层为 Playwright 本地无头浏览器渲染），不依赖后端生成图片。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

# 兼容 AstrBot 不同的插件加载方式：优先相对导入，失败则退回到目录导入
try:
    from .dcs_api import (
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
        classify_users,
        fetch_whosin_users,
        format_text_message,
    )
    from .dcs_render import WhosinRenderer
except ImportError:  # pragma: no cover - 取决于运行环境
    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)
    from dcs_api import (
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
        classify_users,
        fetch_whosin_users,
        format_text_message,
    )
    from dcs_render import WhosinRenderer

# 场馆时区：UTC+8（与站点的 FLT 时间一致）
FLT = timezone(timedelta(hours=8))


class DcsWhosinPlugin(Star):
    """直流会馆查询机器人插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 插件所在目录，用于定位模板与 Logo 素材
        self.plugin_dir = Path(__file__).resolve().parent
        self.renderer = WhosinRenderer(self.plugin_dir)
        # 静默策略状态（内存级，插件重载即清零）
        self._last_by_user: dict[str, float] = {}
        self._last_by_group: dict[str, float] = {}
        self._last_notice: dict[str, float] = {}
        # 状态推送后台任务
        self._status_task: asyncio.Task | None = None
        # 已见过的群（首次见到时记录一条日志，便于配置白名单）
        self._seen_groups: set[str] = set()

    async def initialize(self) -> None:
        """插件加载后启动状态推送后台任务。"""
        if self._status_task and not self._status_task.done():
            return
        self._status_task = asyncio.create_task(self._status_loop())
        logger.info("[dcs_whosin] 状态推送任务已启动。")

    async def _status_loop(self) -> None:
        """按配置间隔向目标会话推送机器人状态。"""
        await asyncio.sleep(20)
        while True:
            try:
                if self.config.get("status_push_enable", False):
                    await self._push_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dcs_whosin] 状态推送异常：{exc}")
            interval = int(self.config.get("status_push_interval_seconds", 600) or 600)
            await asyncio.sleep(max(interval, 60))

    async def _push_status(self) -> None:
        """构建状态文本并推送到所有目标会话。"""
        targets = self.config.get("status_push_targets") or []
        if isinstance(targets, str):
            targets = [
                item.strip()
                for item in targets.replace("，", ",").split(",")
                if item.strip()
            ]
        targets = [str(item).strip() for item in targets if str(item).strip()]
        if not targets:
            return

        now = datetime.now(FLT)
        venue_line = "在店：获取失败"
        try:
            result = await self._fetch_data()
            if not result.error:
                groups = classify_users(result.users)
                venue_line = (
                    f"在店：{len(groups.players) + len(groups.staff_line)} 人"
                    f"（玩家 {len(groups.players)} / 管理 {len(groups.staff_line)}）"
                )
        except Exception:  # noqa: BLE001
            pass

        text = (
            "【会馆机器人·状态】\n"
            f"时间：{now.strftime('%m-%d %H:%M')}\n"
            "运行：在线正常\n"
            f"{venue_line}"
        )
        for target in targets:
            try:
                ok = await self.context.send_message(
                    target, MessageChain([Plain(text)])
                )
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
        blocked = self._policy_block(event)
        if blocked is not None:
            if blocked:
                yield event.plain_result(blocked)
            return

        allowed, notice = await self._check_rate_limit(event)
        if not allowed:
            if notice:
                yield event.plain_result(notice)
            return

        try:
            result = await self._fetch_data()

            # 抓取失败：返回友好提示，而不是崩溃或发送空消息
            if result.error:
                yield event.plain_result(f"查询失败：{result.error}")
                return

            groups = classify_users(result.users)

            # 重要规则：玩家数量为 0（只有管理员/士大夫或完全无人）时，
            # 不发送空信息，统一发送指定提示文案
            if not groups.players:
                yield event.plain_result(EMPTY_PLAYER_MESSAGE)
                return

            text = format_text_message(groups)
            image_url = await self._render_image(groups, result.source_url)

            if image_url:
                # 图片 + 文本合并为一条消息发送
                yield event.chain_result(
                    [
                        Plain(text),
                        Image(file=image_url),
                    ]
                )
            else:
                # 图片渲染失败时降级为纯文本（附提示，避免"未返图"的困惑）
                yield event.plain_result(text + "\n\n（注：图片渲染暂时失败，本次为文字版）")
        except Exception as exc:  # noqa: BLE001 - 兜底，保证插件不因单次查询崩溃
            logger.error(f"[dcs_whosin] 查询在店人员时发生异常：{exc}")
            yield event.plain_result("查询失败，请稍后再试。")

    # ------------------------------------------------------------------
    # 指令：25h
    # ------------------------------------------------------------------
    @filter.command("25h")
    async def nightcord(self, event: AstrMessageEvent):
        """娱乐指令：回复 25時、ナイトコードで"""
        blocked = self._policy_block(event)
        if blocked is not None:
            if blocked:
                yield event.plain_result(blocked)
            return

        allowed, notice = await self._check_rate_limit(event)
        if not allowed:
            if notice:
                yield event.plain_result(notice)
            return

        yield event.plain_result("25時、ナイトコードで")

    # ------------------------------------------------------------------
    # 静默策略：私聊屏蔽 / 群白名单 / 频率限制
    # ------------------------------------------------------------------
    def _policy_block(self, event: AstrMessageEvent) -> str | None:
        """按静默策略拦截：空串=静默忽略，非空=回复提示，None=放行"""
        if event.is_private_chat() and not self.config.get("allow_private_chat", False):
            return ""

        group_id = event.get_group_id()
        if group_id and group_id not in self._seen_groups:
            self._seen_groups.add(group_id)
            logger.info(
                f"[dcs_whosin] 首次收到群消息：group={group_id} 平台={event.get_platform_name()}"
            )

        allowed_groups = self.config.get("allowed_groups") or []
        if isinstance(allowed_groups, str):
            allowed_groups = [
                item.strip()
                for item in allowed_groups.replace("，", ",").split(",")
                if item.strip()
            ]
        if allowed_groups:
            group_id = event.get_group_id()
            if not group_id or str(group_id) not in {str(item) for item in allowed_groups}:
                return ""
        return None

    async def _check_rate_limit(self, event: AstrMessageEvent) -> tuple[bool, str | None]:
        """频率限制：同人同群冷却 + 同群回复最小间隔，返回 (是否放行, 提示文本)"""
        now = time.monotonic()
        cooldown = int(self.config.get("cooldown_seconds", 60) or 0)
        interval = float(self.config.get("group_interval_seconds", 5) or 0)

        group_id = event.get_group_id()
        sender_id = event.get_sender_id() or "unknown"
        user_key = f"{group_id}:{sender_id}"

        if cooldown > 0:
            last = self._last_by_user.get(user_key)
            if last is not None and now - last < cooldown:
                notice_last = self._last_notice.get(user_key)
                if notice_last is None or now - notice_last >= cooldown:
                    self._last_notice[user_key] = now
                    remain = int(cooldown - (now - last)) + 1
                    return False, f"查询太频繁了，请 {remain} 秒后再试。"
                return False, None

        if group_id and interval > 0:
            last_group = self._last_by_group.get(group_id)
            if last_group is not None and now - last_group < interval:
                await asyncio.sleep(interval - (now - last_group))

        self._last_by_user[user_key] = time.monotonic()
        if group_id:
            self._last_by_group[group_id] = time.monotonic()
        return True, None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    async def _fetch_data(self):
        """按插件配置抓取在店数据。"""
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):  # 兼容用户把地址配置成单个字符串的情况
            urls = [urls]
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
        )

    async def _render_image(self, groups, source_url: str):
        """渲染在店一览图片；未启用或渲染失败时返回 None。"""
        if not self.config.get("enable_image", True):
            return None

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
        return await self.renderer.render(self, payload, theme=theme, ttl=ttl)

    async def terminate(self):
        """插件被卸载 / 停用时清理图片缓存与推送任务。"""
        if self._status_task and not self._status_task.done():
            self._status_task.cancel()
        await self.renderer.cache.clear()
        logger.info("[dcs_whosin] 插件已停用，图片缓存已清理。")
