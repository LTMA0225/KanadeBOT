"""直流会馆 · @bot 自助进店/离店支持模块（v2.5.0）。

玩家在群里 @ 机器人（或发"进店"/"离店"）时，机器人用发送者的 QQ 号调用
站点 /api/bot/action 接口：站点按 QQ 绑定（User.qqid）找到账号，代为进店/离店。
令牌与 webhook 共用（请求头 X-DCS-Token = 站点 .env 的 DCS_WEBHOOK_TOKEN）。

本模块不依赖 astrbot；format_attend_reply 为纯函数，便于离线测试。
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from .dcs_presence import format_fee_yuan, visit_key
except ImportError:  # pragma: no cover - 目录导入方式
    from dcs_presence import format_fee_yuan, visit_key


async def call_bot_action(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    token: str,
    action: str,
    qq: str,
) -> Optional[Dict[str, Any]]:
    """调用站点 /api/bot/action；网络失败返回 None（调用方给通用提示）。"""
    import aiohttp

    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        if not route.secure:
            continue
        url = route.origin + "/api/bot/action"
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with api._request(
                    session,
                    "POST",
                    url,
                    route.connect_ip,
                    json={"action": action, "qq": qq},
                    headers={"User-Agent": api.USER_AGENT, "X-DCS-Token": token},
                ) as resp:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        return data
                    return {"success": False, "error": f"HTTP_{resp.status}"}
        except Exception:  # noqa: BLE001 - 换下一条线路
            continue
    return None


def format_attend_reply(
    action: str, result: Optional[Dict[str, Any]], prefix: str = ""
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """把站点返回整理成 (回复文本, 记账信息)。

    记账信息 {"key","e","x","at"} 用于把该次进/离店写入统一去重表，
    避免播报通道（Webhook/记录流/快照）再重复播报一次。
    """
    if result is None:
        return "连接会馆网站失败，请稍后再试", None

    if result.get("success"):
        done = str(result.get("action") or action or "")
        name = str(result.get("nickname") or "")
        entered = result.get("enteredAt")
        key = visit_key(result.get("userId"), entered)
        if done == "leave":
            left = result.get("leftAt")
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
        meta = {"key": key, "e": 1, "x": 0, "at": entered}
        return f"{prefix}{name}进店了", meta

    error = str(result.get("error") or "")
    if error == "NOT_BOUND":
        return "这个 QQ 还没绑定会馆账号：打开会馆网站 →「管理页」→「QQ绑定」完成绑定后再试", None
    if error == "ALREADY_IN":
        return "你已经在店里啦～", None
    if error == "NOT_IN":
        return "你现在不在店里哦", None
    if error == "LOW_BALANCE":
        try:
            admission = int(result.get("admissionBalance") or 1000) / 100
        except (TypeError, ValueError):
            admission = 10.0
        return f"余额不足：进店至少需要 ¥{admission:.2f}，请先到网站充值", None
    if error == "UNAUTHORIZED":
        return "机器人令牌配置有误，请让维护者检查", None
    if error == "BAD_QQ":
        return "无法识别你的 QQ 号", None
    return "操作失败，请稍后再试", None
