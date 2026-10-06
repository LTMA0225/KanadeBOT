"""直流会馆 · /bd QQ 绑定支持模块（v2.7.0）。

绑定流程（复用站点现有逻辑，无需站点更新）：
1. 玩家在会馆网站「个人信息」页填写 QQ 发起绑定 → 页面显示"绑定验证码"
   （服务端把 pendingQqid + qqBindToken 存到该账号上）；
2. 玩家在会馆群里发送 "/bd 验证码"（或 "/bd验证码"）；
3. 机器人读取发送者 QQ 与验证码，调用站点 /api/qqBindVerify
   （该接口按 pendingQqid+token 校验，无需登录），完成绑定。

安全：只使用发送者自己的 QQ 作为 pendingQqid —— 只有 QQ 本人能完成自己账号的绑定。
本模块不依赖 astrbot；纯函数便于离线测试。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# 站点验证码 = base260（小写字母+数字交替，32 位），这里放宽到 8-64 位字母数字
_CODE_RE = re.compile(r"^[a-z0-9]{8,64}$")


def normalize_bind_code(raw: str) -> str:
    """去掉所有空白并转小写（站点验证码为小写字母+数字）。"""
    return "".join(str(raw or "").split()).lower()


def is_valid_bind_code(code: str) -> bool:
    return bool(_CODE_RE.match(code or ""))


async def call_qq_bind_verify(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    qq: str,
    code: str,
) -> Optional[Dict[str, Any]]:
    """调用站点 /api/qqBindVerify 完成绑定；网络失败返回 None。"""
    import aiohttp

    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        if not route.secure:
            continue
        url = route.origin + "/api/qqBindVerify"
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with api._request(
                    session,
                    "POST",
                    url,
                    route.connect_ip,
                    json={"pendingQqid": qq, "token": code},
                    headers={"User-Agent": api.USER_AGENT},
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


def format_bind_reply(result: Optional[Dict[str, Any]], qq: str = "") -> str:
    """把站点返回整理成回复文本。"""
    if result is None:
        return "连接会馆网站失败，请稍后再试"
    if result.get("success"):
        tail = f"（QQ {qq}）" if qq else ""
        return f"✅ 绑定成功{tail}！之后就可以用 @bot 离店 等功能啦～"
    error = str(result.get("error") or "")
    if error.startswith("HTTP_5"):
        return "绑定失败：网站暂时异常，请稍后再试"
    return (
        "绑定失败：验证码无效或已过期。请到会馆网站「个人信息」页重新获取验证码，"
        "并确认「待绑QQ」填的是你发消息的这个 QQ。"
    )
