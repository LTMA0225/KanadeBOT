"""音游窝 · @bot 自助离店支持模块（v3.0.0）。

玩家在群里发 "@机器人 离店" 时，机器人用发送者的 QQ 号完成自助离店：

- 在店名单来自统一 Bot API（getPresentUsers），按 QQ 绑定（qqid）找到账号；
- 调用站点工作人员接口 /api/forceleave 代为离店结算（机器人账号有工作人员权限）；
- 离店后从来店记录页读取**真实扣费金额**（读取失败时按价目表本地估算兜底）。

出于风险控制，只支持离店，不支持机器人进店。
本模块不依赖 astrbot；格式化与解析函数为纯函数，便于离线测试。
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from .dcs_presence import format_fee_yuan, visit_key
except ImportError:  # pragma: no cover - 目录导入方式
    from dcs_presence import format_fee_yuan, visit_key


_VISIT_ROW = re.compile(
    r'<div class="visitListing">\s*<p>([^<]*)</p>\s*<p>([^<]*)</p>\s*<p>([^<]*)</p>',
    re.S,
)


def money_text_to_fen(text: str) -> Optional[int]:
    """把站点金额文案（¥15.75 / ¥1.5万 / −¥3.00）转成分；无金额返回 None。"""
    raw = str(text or "").strip()
    if not raw or raw in ("-", "—"):
        return None
    negative = raw.startswith("−") or raw.startswith("-")
    cleaned = raw.replace("¥", "").replace("￥", "").replace("−", "").replace("-", "").strip()
    try:
        if cleaned.endswith("万"):
            value = int(round(float(cleaned[:-1]) * 1_000_000))
        else:
            value = int(round(float(cleaned) * 100))
    except (TypeError, ValueError):
        return None
    return -value if negative else value


def parse_latest_charge_fen(html: str) -> Optional[int]:
    """从来店记录页解析最新一条（第一行）的扣费金额（分）。"""
    if not html:
        return None
    match = _VISIT_ROW.search(html)
    if not match:
        return None
    return money_text_to_fen(match.group(3))


async def call_force_leave(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    cookie: str,
    username: str,
    password: str,
    user_id: int,
) -> Optional[Dict[str, Any]]:
    """用机器人（工作人员）会话调用站点 /api/forceleave 代为离店；网络失败返回 None。"""
    import aiohttp

    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        if not route.secure:
            continue
        auth, kind, _err = await api._resolve_cookie(route, cookie, username, password, timeout)
        if kind:
            continue
        url = route.origin + "/api/forceleave"
        headers = {"User-Agent": api.USER_AGENT, "Cookie": auth}
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with api._request(
                    session,
                    "POST",
                    url,
                    route.connect_ip,
                    json={"userId": int(user_id)},
                    headers=headers,
                ) as resp:
                    body_text = await resp.text()
                    try:
                        data = json.loads(body_text) if body_text else None
                    except Exception:  # noqa: BLE001 - 非 JSON（如 500 页面）
                        data = None
                    if isinstance(data, dict):
                        return data
                    return {"success": False, "error": f"HTTP_{resp.status}"}
        except Exception:  # noqa: BLE001 - 换下一条线路
            continue
    return None


async def fetch_latest_charge_fen(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    cookie: str,
    username: str,
    password: str,
    user_id: int,
) -> Optional[int]:
    """抓取用户来店记录页，返回最新一条的扣费（分）；失败返回 None。"""
    import aiohttp

    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        if not route.secure:
            continue
        auth, kind, _err = await api._resolve_cookie(route, cookie, username, password, timeout)
        if kind:
            continue
        url = route.origin + f"/manage/users/{int(user_id)}/visits"
        headers = {"User-Agent": api.USER_AGENT, "Cookie": auth}
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with api._request(session, "GET", url, route.connect_ip, headers=headers) as resp:
                    if resp.status != 200:
                        continue
                    return parse_latest_charge_fen(await resp.text())
        except Exception:  # noqa: BLE001 - 换下一条线路
            continue
    return None


def format_attend_reply(
    action: str, result: Optional[Dict[str, Any]], prefix: str = ""
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """把站点返回整理成 (回复文本, 记账信息)。

    仅支持离店（leave）。记账信息 {"key","e","x","at"} 用于把该次离店写入统一去重表，
    避免播报通道（Webhook/记录流/快照）再重复播报一次。
    """
    if result is None:
        return "连接会馆网站失败，请稍后再试", None

    if result.get("success") and str(result.get("action") or action) == "leave":
        name = str(result.get("nickname") or "")
        entered = result.get("enteredAt")
        left = result.get("leftAt")
        key = visit_key(result.get("userId"), entered)
        try:
            left_ms = int(left) if left is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            left_ms = int(time.time() * 1000)
        try:
            minutes = max(0, math.ceil((left_ms - int(entered)) / 60000))
        except (TypeError, ValueError):
            minutes = 0
        charge = result.get("charge")
        meta = {"key": key, "e": 1, "x": 1, "at": entered}
        text = (
            f"{prefix}{name}离店了\n"
            f"游玩{minutes}分钟\n"
            f"扣费{format_fee_yuan(charge or 0)}元（线上余额）"
        )
        return text, meta

    if result.get("success"):
        return "操作失败，请稍后再试", None

    error = str(result.get("error") or "")
    if error == "NOT_BOUND":
        return "这个 QQ 还没绑定会馆账号：打开会馆网站 →「管理页」→「QQ绑定」完成绑定后再试", None
    if error == "NOT_IN":
        return "你现在不在店里哦", None
    if error == "NOT_FOUND":
        return "没有查到你的在店记录：你可能不在店，或这个 QQ 还没绑定会馆账号（可到网站绑定后再试）", None
    if error == "UNAUTHORIZED":
        return "机器人令牌配置有误，请让维护者检查", None
    if error == "NO_TOKEN":
        return "机器人还没配置站点令牌，请联系维护者", None
    if error == "BAD_QQ":
        return "无法识别你的 QQ 号", None
    if error.startswith("HTTP_404") or error.startswith("HTTP_405"):
        return "会馆网站还没有更新到这个功能（缺少自助离店接口），请联系维护者", None
    if error.startswith("HTTP_5"):
        return "离店失败（可能已经不在店了），请稍后再试", None
    if error.startswith("HTTP_"):
        return "会馆网站暂时返回异常，请稍后再试", None
    return "操作失败，请稍后再试", None
