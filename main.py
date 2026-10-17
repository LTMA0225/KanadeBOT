
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import math
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

try:
    from .dcs_attend import (
        call_force_leave,
        fetch_latest_charge_fen,
        format_attend_reply,
    )
    from .dcs_bind import (
        format_bind_reply,
        is_valid_bind_code,
        normalize_bind_code,
    )
    from .dcs_booking import (
        active_bookings,
        format_booking_confirm,
        format_booking_notice,
        format_window,
        parse_booking,
        prune_bookings,
    )
    from .dcs_botapi import bot_action, parse_present_users, public_api_error
    from .dcs_api import (
        DEFAULT_FALLBACK_IPS,
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
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
        format_current_rate_message,
        format_fee_yuan,
        parse_pricing,
        visit_key,
    )
    from .dcs_render import WhosinRenderer
except ImportError:
    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)
    from dcs_attend import (
        call_force_leave,
        fetch_latest_charge_fen,
        format_attend_reply,
    )
    from dcs_bind import (
        format_bind_reply,
        is_valid_bind_code,
        normalize_bind_code,
    )
    from dcs_booking import (
        active_bookings,
        format_booking_confirm,
        format_booking_notice,
        format_window,
        parse_booking,
        prune_bookings,
    )
    from dcs_botapi import bot_action, parse_present_users, public_api_error
    from dcs_api import (
        DEFAULT_FALLBACK_IPS,
        DEFAULT_TARGET_URLS,
        EMPTY_PLAYER_MESSAGE,
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
        format_current_rate_message,
        format_fee_yuan,
        parse_pricing,
        visit_key,
    )
    from dcs_render import WhosinRenderer

PLUGIN_NAME = "astrbot_plugin_dcs_whosin"

FLT = timezone(timedelta(hours=8))

ADAPTER_PATCH_MARKERS = ("DCS-PATCH:filter", "[[DCS 自定义过滤]]")

QUERY_QUIPS = (
    "等我吃完这口杯面帮你查哦…",
    "唔…稍等一下，我把耳机摘下来看看…",
    "啊、好的…让我看一眼…",
    "等我一小会儿，马上就好…",
    "好，我看看哦…",
    "嗯…稍等，我确认一下。",
)

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    return [str(item).strip() for item in value if str(item).strip()]

def _detect_adapter_patch() -> bool | None:
    try:
        from astrbot.core.platform.sources.aiocqhttp import (
            aiocqhttp_platform_adapter as adapter_module,
        )

        source = inspect.getsource(adapter_module)
    except Exception:
        return None
    return any(marker in source for marker in ADAPTER_PATCH_MARKERS)

class DcsWhosinPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_dir = Path(__file__).resolve().parent
        self.renderer = WhosinRenderer(self.plugin_dir, self._resolve_cache_dir())
        self.plugin_data_dir = self.renderer.cache_dir.parent
        self._last_by_user: dict[str, float] = {}
        self._last_notice: dict[str, float] = {}
        self._group_next_slot: dict[str, float] = {}
        self._status_task: asyncio.Task | None = None
        self._deps_task: asyncio.Task | None = None
        self._last_health: bool | None = None
        self._last_push_at = 0.0
        self._adapter_patched: bool | None = None
        self._seen_groups: set[str] = set()
        self._presence: dict[str, dict] = {}
        self._presence_ready = False
        self._presence_task: asyncio.Task | None = None
        self._records_state: dict | None = None
        self._mj_task: asyncio.Task | None = None
        self._attend_last: dict[str, float] = {}
        self._bind_last: dict[str, float] = {}
        self._bookings: list[dict] = []
        self._pricing: dict | None = None
        self._pricing_at = 0.0
        self._pricing_default_logged = False

    @staticmethod
    def _resolve_cache_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "image_cache"
        except Exception:
            return Path(tempfile.gettempdir()) / PLUGIN_NAME / "image_cache"

    async def initialize(self) -> None:
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
        self._load_bookings()
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self._presence_loop())
            logger.info("[dcs_whosin] 进离店播报任务已启动。")
        if self.config.get("mj_enable", True):
            if self._mj_task is None or self._mj_task.done():
                self._mj_task = asyncio.create_task(self._mj_prefetch())
        if self._status_task and not self._status_task.done():
            return
        self._status_task = asyncio.create_task(self._status_loop())
        logger.info("[dcs_whosin] 状态推送任务已启动。")

    async def _ensure_render_dependencies(self) -> None:
        if not self.config.get("enable_image", True):
            return
        missing = [name for name in ("jinja2", "playwright") if importlib.util.find_spec(name) is None]
        if not missing:
            return
        try:
            from astrbot.core import pip_installer
        except Exception:
            logger.warning(f"[dcs_whosin] 缺少图片渲染依赖 {missing}，请在 AstrBot 插件页为本插件安装依赖。")
            return
        logger.info(f"[dcs_whosin] 正在后台安装图片渲染依赖 {missing}（装好前只发文字版）……")
        try:
            await pip_installer.install(requirements_path=str(self.plugin_dir / "requirements.txt"))
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 渲染依赖安装失败：{exc}；可在 AstrBot 插件页手动安装依赖。")
            return
        importlib.invalidate_caches()
        logger.info("[dcs_whosin] 图片渲染依赖安装完成，下一次查询起恢复出图。")

    def _api_config(self):
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):
            urls = [urls]
        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)
        key = str(self.config.get("bot_api_key", "") or "").strip()
        return urls, fallback_ips, timeout, key

    def _api_qqid(self) -> str:
        cfg = str(self.config.get("bot_qqid", "") or "").strip()
        return cfg if cfg.isdigit() else "0"

    async def _fetch_present_users(self, qqid: str = ""):
        urls, fallback_ips, timeout, key = self._api_config()
        if not key:
            return None, {"ok": False, "error": "NO_KEY", "message": "未配置 Bot 明钥"}
        result = await bot_action(
            urls, fallback_ips, timeout, key, qqid or self._api_qqid(), "getPresentUsers", ""
        )
        if not result.get("ok"):
            return None, result
        users = parse_present_users(result.get("data"))
        if users is None:
            return None, {"ok": False, "error": "BAD_DATA", "message": "返回数据格式异常"}
        return users, None

    async def _status_loop(self) -> None:
        await asyncio.sleep(20)
        first = True
        while True:
            try:
                if self.config.get("status_push_enable", False):
                    await self._status_tick(first)
                    first = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[dcs_whosin] 状态推送异常：{exc}")
            interval = int(self.config.get("status_push_interval_seconds", 600) or 600)
            await asyncio.sleep(max(interval, 60))

    async def _status_tick(self, first: bool) -> None:
        mode = str(self.config.get("status_push_mode", "smart") or "smart")
        users, err = await self._fetch_present_users()
        healthy = users is not None
        err_detail = ""
        if healthy:
            players = [u for u in users if u.get("role") == "player"]
            others = [u for u in users if u.get("role") in ("staff", "admin")]
            venue_line = f"在店：{len(users)} 人（玩家 {len(players)} / 管理 {len(others)}）"
        else:
            err_detail = f"{err.get('error')} {err.get('message') or ''}".strip()
            venue_line = "站点：异常（接口访问失败）"
            logger.warning(f"[dcs_whosin] 状态检查失败：{err_detail or '未知原因'}")

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
        if not healthy and err_detail:
            lines.append(f"详情：{err_detail[:200]}")
        if first or self._adapter_patched is False:
            lines.append(f"适配器补丁：{patch_text}")
        await self._push_status("\n".join(lines))
        self._last_push_at = time.monotonic()

    async def _push_status(self, text: str) -> None:
        for target in _as_list(self.config.get("status_push_targets")):
            try:
                ok = await self.context.send_message(target, MessageChain([Plain(text)]))
                if not ok:
                    logger.warning(f"[dcs_whosin] 状态推送目标不存在：{target}")
            except Exception as exc:
                logger.warning(f"[dcs_whosin] 状态推送失败（{target}）：{exc}")

    @filter.command("j")
    async def query_whosin(self, event: AstrMessageEvent):
        if not self._policy_allows(event):
            return

        allowed, notice = self._check_cooldown(event)
        if not allowed:
            if notice:
                yield event.plain_result(notice)
            return
        await self._wait_group_slot(event)

        if self.config.get("booking_enable", True):
            active = active_bookings(self._bookings)
            if active:
                prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
                logger.info(f"[dcs_whosin] /j 命中包场提示（{len(active)} 条）")
                yield event.plain_result(format_booking_notice(active, prefix))
                return

        if self.config.get("query_quip_enable", True):
            quips = _as_list(self.config.get("query_quip_lines")) or list(QUERY_QUIPS)
            yield event.plain_result(random.choice(quips))
            try:
                delay_ms = int(self.config.get("query_quip_delay_ms", 1500) or 0)
            except (TypeError, ValueError):
                delay_ms = 1500
            if delay_ms > 0:
                await asyncio.sleep(min(delay_ms, 10000) / 1000)

        try:
            qqid = str(event.get_sender_id() or "").strip()
            users, err = await self._fetch_present_users(qqid)

            if users is None:
                logger.warning(f"[dcs_whosin] 查询失败：{err.get('error')} {err.get('message') or ''}")
                yield event.plain_result(f"查询失败：{public_api_error(err)}")
                return

            players = [u for u in users if u.get("role") == "player"]
            staff_line = [u for u in users if u.get("role") in ("staff", "admin")]
            staff_line.sort(key=lambda u: 0 if u.get("role") == "admin" else 1)

            if not players:
                yield event.plain_result(EMPTY_PLAYER_MESSAGE)
                return

            text_lines = [f"玩家：{','.join(u['name'] for u in players)}"]
            if staff_line:
                text_lines.append(f"管理/STAFF：{','.join(u['name'] for u in staff_line)}")
            text = "\n".join(text_lines)

            if not self.config.get("enable_image", True):
                yield event.plain_result(text)
                return

            urls, _fallback_ips, _timeout, _key = self._api_config()
            source_host = urlsplit(urls[0]).netloc if urls else ""
            image_path = await self._render_image(players, staff_line, source_host)
            if image_path:
                yield event.chain_result(
                    [
                        Plain(text),
                        Image.fromFileSystem(image_path),
                    ]
                )
            else:
                yield event.plain_result(text + "\n\n（注：图片渲染暂时失败，本次为文字版）")
        except Exception as exc:
            logger.error(f"[dcs_whosin] 查询在店人员时发生异常：{exc!r}")
            yield event.plain_result("查询失败，请稍后再试。")

    @filter.command("25h")
    async def nightcord(self, event: AstrMessageEvent):
        if not self._policy_allows(event):
            return
        yield event.plain_result("25時、ナイトコードで")

    @filter.command("jg")
    async def current_price(self, event: AstrMessageEvent):
        if not self._policy_allows(event):
            return
        try:
            pricing = await self._get_pricing("")
            prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
            text = format_current_rate_message(pricing, prefix)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 时段价格查询失败：{exc!r}")
            text = None
        if text:
            yield event.plain_result(text)
        else:
            yield event.plain_result("价格查询失败，请稍后再试。")

    @filter.command("mj")
    async def mj_sticker(self, event: AstrMessageEvent):
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

    @filter.command("mjlist")
    async def mj_list(self, event: AstrMessageEvent):
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

    @filter.command("离店")
    async def attend_leave(self, event: AstrMessageEvent):
        if not self._policy_allows(event):
            return
        if not self.config.get("attend_enable", True):
            return
        text = await self._attend_action(event, "leave")
        if text:
            yield event.plain_result(text)

    @filter.regex(r"(?i)^bd\s*\+?\s*(.*)$")
    async def bind_qq(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command:
            return
        if not self._policy_allows(event):
            return
        text = event.get_message_str().strip()
        match = re.match(r"(?i)^bd\s*\+?\s*(.*)$", text)
        code = normalize_bind_code(match.group(1)) if match else ""
        if not is_valid_bind_code(code):
            yield event.plain_result(
                "用法：/bd 验证码\n"
                "验证码在会馆网站→「个人信息」页获取（填写待绑QQ后页面上会显示）。"
            )
            return
        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            return
        now = time.monotonic()
        cooldown = int(self.config.get("bind_cooldown_seconds", 15) or 0)
        last = self._bind_last.get(qq, 0.0)
        if cooldown > 0 and now - last < cooldown:
            remain = int(cooldown - (now - last)) + 1
            yield event.plain_result(f"操作太频繁，请 {remain} 秒后再试")
            return
        self._bind_last[qq] = now
        if len(self._bind_last) > 500:
            cutoff = now - max(cooldown, 60) * 2
            self._bind_last = {k: v for k, v in self._bind_last.items() if v >= cutoff}

        urls, fallback_ips, timeout, key = self._api_config()
        if not key:
            yield event.plain_result("机器人还没配置 Bot 明钥，请联系维护者")
            return
        result = None
        try:
            result = await bot_action(urls, fallback_ips, timeout, key, qq, "bindQq", code)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] /bd 绑定请求异常：{exc!r}")
        if isinstance(result, dict) and result.get("ok"):
            data = result.get("data") or {}
            message = str(data.get("message") or "") if isinstance(data, dict) else ""
            logger.info(f"[dcs_whosin] /bd 绑定结果：QQ={qq} 站点消息={message}")
        else:
            err = result.get("error") if isinstance(result, dict) else "network"
            logger.info(f"[dcs_whosin] /bd 绑定未成功：QQ={qq} 原因={err}")
        yield event.plain_result(
            format_bind_reply(result if isinstance(result, dict) else {"ok": False, "error": "network"}, qq)
        )

    @filter.regex(r"^包(?:机|场)\s*\S")
    async def booking_add(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command:
            return
        if not self.config.get("booking_enable", True):
            return
        if not self._policy_allows(event):
            return
        if not await self._is_booking_admin(event):
            yield event.plain_result("只有群主/管理员可以登记包场哦")
            return
        booking, error = parse_booking(event.get_message_str().strip())
        if booking is None:
            yield event.plain_result(error or "格式不对")
            return
        booking["by"] = str(event.get_sender_id() or "")
        booking["created_ms"] = int(time.time() * 1000)
        self._bookings = prune_bookings(self._bookings)
        self._bookings.append(booking)
        self._save_bookings()
        logger.info(
            f"[dcs_whosin] 包场登记：{booking.get('machine')} "
            f"{booking.get('start_ms')}~{booking.get('end_ms')}"
        )
        yield event.plain_result(format_booking_confirm(booking))

    @filter.regex(r"^取消包(?:机|场)")
    async def booking_cancel(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command:
            return
        if not self.config.get("booking_enable", True):
            return
        if not self._policy_allows(event):
            return
        if not await self._is_booking_admin(event):
            yield event.plain_result("只有群主/管理员可以取消包场哦")
            return
        keyword = re.sub(r"^取消包(?:机|场)\s*", "", event.get_message_str().strip()).strip()
        now_ms = int(time.time() * 1000)
        future = [b for b in self._bookings if int(b.get("end_ms") or 0) > now_ms]
        if keyword:
            removed = [b for b in future if keyword in str(b.get("machine") or "")]
        else:
            removed = list(future)
        if not removed:
            suffix = f"（关键词：{keyword}）" if keyword else ""
            yield event.plain_result("没有找到可取消的包场" + suffix)
            return
        removed_ids = {id(b) for b in removed}
        self._bookings = prune_bookings([b for b in self._bookings if id(b) not in removed_ids])
        self._save_bookings()
        lines = [f"已取消 {len(removed)} 条包场："]
        for b in removed:
            try:
                window = format_window(int(b.get("start_ms") or 0), int(b.get("end_ms") or 0))
            except (TypeError, ValueError):
                window = ""
            lines.append(f"· {b.get('machine', '')} {window}")
        logger.info(f"[dcs_whosin] 包场取消：{len(removed)} 条（关键词={keyword or '全部'}）")
        yield event.plain_result("\n".join(lines))

    @llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str):
        '''联网搜索实时信息。

        当用户问到最新新闻、活动、价格、赛事、音乐游戏更新等你不知道或可能已经过时的信息时使用。

        Args:
            query(string): 搜索关键词，1 到 128 个字符
        '''
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        key = ""
        for item in cfg.get("provider", []) or []:
            if str(item.get("id") or "") == "deepseek":
                keys = item.get("key") or []
                if keys:
                    key = str(keys[0])
                break
        if not key:
            return "联网搜索不可用：未找到 DeepSeek 密钥。"
        query = str(query or "").strip()[:128]
        if not query:
            return "搜索关键词为空。"
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "https://api.deepseek.com/v1/tools/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"query": query, "count": 5},
                ) as resp:
                    if resp.status != 200:
                        return f"联网搜索失败（HTTP {resp.status}），请稍后再试。"
                    data = await resp.json()
        except Exception as exc:
            return f"联网搜索失败：{exc!r}"
        results = data.get("results") or []
        if not results:
            return "没有搜到相关结果。"
        lines = []
        for item in results[:5]:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or item.get("snippet") or "").strip()
            link = str(item.get("url") or "").strip()
            date = str(item.get("date") or "")[:10]
            suffix = f"（{date}）" if date else ""
            lines.append(f"- {title}{suffix}\n  {summary[:300]}\n  来源：{link}")
        return "搜索结果如下（请用自己的话简短转述，不要照抄）：\n" + "\n".join(lines)

    def _policy_allows(self, event: AstrMessageEvent) -> bool:
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
        if len(self._last_by_user) > 1000:
            for store in (self._last_by_user, self._last_notice):
                for key in [k for k, t in store.items() if now - t >= cooldown]:
                    store.pop(key, None)
        return True, None

    async def _wait_group_slot(self, event: AstrMessageEvent) -> None:
        group_id = str(event.get_group_id() or "")
        interval = float(self.config.get("group_interval_seconds", 5) or 0)
        if not group_id or interval <= 0:
            return
        now = time.monotonic()
        slot = max(now, self._group_next_slot.get(group_id, 0.0))
        self._group_next_slot[group_id] = slot + interval
        if slot > now:
            await asyncio.sleep(slot - now)

    def _booking_file(self) -> Path:
        return self.plugin_data_dir / "bookings.json"

    def _load_bookings(self) -> None:
        try:
            path = self._booking_file()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("bookings") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return
            cleaned = prune_bookings([b for b in items if isinstance(b, dict)])
            self._bookings = cleaned
            if len(cleaned) != len(items):
                self._save_bookings()
            if self._bookings:
                logger.info(f"[dcs_whosin] 已恢复 {len(self._bookings)} 条包场记录")
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 包场记录恢复失败：{exc!r}")

    def _save_bookings(self) -> None:
        try:
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            path = self._booking_file()
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"bookings": self._bookings}, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 包场记录保存失败：{exc!r}")

    async def _is_booking_admin(self, event: AstrMessageEvent) -> bool:
        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            return False
        if qq in _as_list(self.config.get("booking_admins")):
            return True
        bot = getattr(event, "bot", None)
        group_id = str(event.get_group_id() or "").strip()
        if bot is None or not group_id.isdigit():
            return False
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(qq),
                no_cache=False,
            )
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 查询群成员身份失败：{exc!r}")
            return False
        role = str((info or {}).get("role") or "")
        return role in ("owner", "admin")

    @staticmethod
    def _render_entry(user: dict) -> dict:
        entered_ms = user.get("entered_ms")
        entered_text = ""
        minutes = None
        if entered_ms:
            entered_text = datetime.fromtimestamp(int(entered_ms) / 1000, FLT).strftime("%H:%M")
            minutes = max(0, int((time.time() * 1000 - int(entered_ms)) // 60000))
        return {"name": user.get("name") or "", "entered": entered_text, "minutes": minutes}

    async def _render_image(self, players, staff_line, source_host: str) -> str | None:
        now = datetime.now(FLT)
        ttl = int(self.config.get("cache_ttl_seconds", 30) or 0)
        theme = str(self.config.get("image_theme", "cyberpunk-bw") or "cyberpunk-bw")

        payload = {
            "time_text": now.strftime("%Y-%m-%d %H:%M"),
            "weekday_text": "周" + "一二三四五六日"[now.weekday()],
            "players": [self._render_entry(u) for u in players],
            "staff_line": [self._render_entry(u) for u in staff_line],
            "player_count": len(players),
            "staff_count": len(staff_line),
            "total_count": len(players) + len(staff_line),
            "source_host": source_host,
        }
        return await self.renderer.render(
            payload,
            theme=theme,
            ttl=ttl,
            browser=str(self.config.get("render_browser", "auto") or "auto"),
            browser_path=str(self.config.get("render_browser_path", "") or ""),
            timeout=float(self.config.get("render_timeout_seconds", 20) or 20),
        )

    def _presence_state_file(self) -> Path:
        return self.plugin_data_dir / "presence_state.json"

    def _load_presence_state(self) -> None:
        try:
            path = self._presence_state_file()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(data.get("saved_at") or 0)
            seen = data.get("seen")
            if isinstance(seen, dict):
                self._records_state = {"seen": seen}
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
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 进离店状态恢复失败：{exc!r}")

    def _save_presence_state(self) -> None:
        try:
            self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
            path = self._presence_state_file()
            temp = path.with_suffix(".tmp")
            payload = {
                "saved_at": time.time(),
                "seen": self._ledger_seen(),
                "users": self._presence,
            }
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 进离店状态保存失败：{exc!r}")

    async def _presence_loop(self) -> None:
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
            except Exception as exc:
                logger.warning(f"[dcs_whosin] 进离店播报异常：{exc!r}")
            await asyncio.sleep(interval)

    async def _presence_tick(self, interval: int) -> None:
        users, err = await self._fetch_present_users()
        if users is None:
            logger.warning(f"[dcs_whosin] 进离店检查失败：{err.get('error')} {err.get('message') or ''}")
            return
        now_ms = int(time.time() * 1000)
        current = {item["key"]: dict(item, last_seen_ms=now_ms) for item in users}

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
            key = visit_key(user.get("id"), user.get("entered_ms")) or user.get("key")
            record = self._ledger_get(key)
            if record.get("e"):
                continue
            messages.append(f"{prefix}{user.get('name', '')}进店了")
            self._ledger_mark(key, e=1, at=user.get("entered_ms"))
        for user in exits:
            entered_ms = user.get("entered_ms")
            if not entered_ms:
                logger.warning(f"[dcs_whosin] {user.get('name', '')} 离店但缺少入店时间，跳过播报")
                continue
            key = visit_key(user.get("id"), entered_ms) or user.get("key")
            record = self._ledger_get(key)
            if record.get("x"):
                continue
            last_seen = int(user.get("last_seen_ms") or now_ms)
            gap = max(0, now_ms - last_seen)
            settle_ms = last_seen + min(gap, interval * 1000) // 2
            minutes = max(0, math.ceil((settle_ms - int(entered_ms)) / 60000))
            pricing = await self._get_pricing("")
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

    def _ledger_seen(self) -> dict:
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

    async def _broadcast_presence(self, messages: list[str]) -> None:
        if not messages:
            return
        targets = _as_list(self.config.get("presence_targets"))
        if not targets:
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
                except Exception as exc:
                    logger.warning(f"[dcs_whosin] 进离店播报失败（{target}）：{exc!r}")
            logger.info(f"[dcs_whosin] 进离店播报：{text}")
            if gap > 0:
                await asyncio.sleep(gap)

    async def _get_pricing(self, source_url: str) -> dict:
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
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 计费表抓取失败：{exc!r}")

        pricing = parse_pricing(html) if html else None
        if pricing:
            self._pricing = pricing
            self._pricing_at = now
            return pricing
        if self._pricing:
            self._pricing_at = now
            return self._pricing
        if not self._pricing_default_logged:
            self._pricing_default_logged = True
            logger.warning("[dcs_whosin] 未能获取站点计费表，暂用内置默认费率（自动重试中）")
        self._pricing = DEFAULT_PRICING
        self._pricing_at = now - ttl // 2
        return self._pricing

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
        except Exception as exc:
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
                except Exception:
                    bot = None
            if bot is None:
                logger.info("[dcs_whosin] /mj 预取：napcat 平台未就绪，将在首次使用时获取")
                return
            await self._fetch_and_cache_sticker(bot, index)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[dcs_whosin] /mj 预取失败：{exc!r}")

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

        prefix = str(self.config.get("presence_prefix", "【Kanade】") or "")
        result = None
        try:
            result = await self._attend_leave_via_snapshot(qq)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] @bot 自助离店异常：{exc!r}")

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
        detail = ""
        if result is None:
            detail = "站点请求失败"
        elif not result.get("success"):
            detail = f"错误={result.get('error')}"
        logger.info(f"[dcs_whosin] @bot 自助（{action}）QQ={qq}：{first_line}" + (f"（{detail}）" if detail else ""))
        return text

    def _site_targets(self):
        urls = self.config.get("target_urls", DEFAULT_TARGET_URLS)
        if isinstance(urls, str):
            urls = [urls]
        fallback_ips = _as_list(self.config.get("fallback_ips", DEFAULT_FALLBACK_IPS))
        timeout = int(self.config.get("timeout_seconds", 10) or 10)
        cookie = str(self.config.get("cookie", "") or "")
        username = str(self.config.get("login_username", "") or "")
        password = str(self.config.get("login_password", "") or "")
        return urls, fallback_ips, timeout, cookie, username, password

    @staticmethod
    def _find_snapshot_user(snapshot, qq: str):
        for user in snapshot or []:
            if str(user.get("qqid") or "").strip() == qq:
                return user
        return None

    async def _attend_leave_via_snapshot(self, qq: str) -> dict | None:
        snapshot = None
        now_ms = int(time.time() * 1000)
        if self._presence_ready and self._presence:
            newest = max((int(u.get("last_seen_ms") or 0) for u in self._presence.values()), default=0)
            if now_ms - newest <= 40000:
                snapshot = list(self._presence.values())
        match = self._find_snapshot_user(snapshot, qq)
        if match is None:
            users, _err = await self._fetch_present_users(qq)
            if users is None:
                return None
            match = self._find_snapshot_user(users, qq)
        if match is None or match.get("id") is None or not match.get("entered_ms"):
            return {"success": False, "error": "NOT_FOUND"}

        user_id = int(match["id"])
        entered_ms = int(match["entered_ms"])
        urls, fallback_ips, timeout, cookie, username, password = self._site_targets()
        resp = await call_force_leave(urls, fallback_ips, timeout, cookie, username, password, user_id)
        if resp is None:
            return None
        if not resp.get("success"):
            error = str(resp.get("error") or "")
            if error.startswith("HTTP_401") or error.startswith("HTTP_403"):
                logger.warning("[dcs_whosin] 代离店被站点拒绝（机器人账号权限异常）")
                return {"success": False, "error": "UNAUTHORIZED"}
            return {"success": False, "error": error or "FORCE_FAILED"}

        left_ms = int(time.time() * 1000)
        charge = await fetch_latest_charge_fen(urls, fallback_ips, timeout, cookie, username, password, user_id)
        if charge is None:
            pricing = await self._get_pricing("")
            try:
                multiplier = float(match.get("multiplier")) if match.get("multiplier") is not None else 1.0
            except (TypeError, ValueError):
                multiplier = 1.0
            charge = calculate_charge_fen(entered_ms, left_ms, pricing, multiplier)
        return {
            "success": True,
            "action": "leave",
            "userId": user_id,
            "nickname": match.get("name") or "",
            "enteredAt": entered_ms,
            "leftAt": left_ms,
            "charge": charge,
        }

    async def terminate(self):
        for task in (self._status_task, self._deps_task, self._presence_task, self._mj_task):
            if task and not task.done():
                task.cancel()
        await self.renderer.close()
        logger.info("[dcs_whosin] 插件已停用，本地浏览器与图片缓存已清理。")
