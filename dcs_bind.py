
from __future__ import annotations

import re
from typing import Any, Dict

_CODE_RE = re.compile(r"^[a-z0-9]{8,64}$")

def normalize_bind_code(raw: str) -> str:
    return "".join(str(raw or "").split()).lower()

def is_valid_bind_code(code: str) -> bool:
    return bool(_CODE_RE.match(code or ""))

def format_bind_reply(result: Dict[str, Any], qq: str = "") -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        error = str((result or {}).get("error") or "network")
        if error == "NO_KEY":
            return "机器人还没配置 Bot 明钥，请联系维护者"
        if error == "UNAUTHORIZED":
            return "机器人密钥无效，请联系维护者"
        if error == "FORBIDDEN":
            return "机器人缺少绑定权限，请联系维护者"
        if error.startswith("http_5") or error == "SERVER_ERROR":
            return "绑定失败：网站暂时异常，请稍后再试"
        return "连接会馆网站失败，请稍后再试"

    data = result.get("data")
    message = str(data.get("message") or "") if isinstance(data, dict) else ""
    if "成功" in message:
        tail = f"（QQ {qq}）" if qq else ""
        return f"✅ 绑定成功{tail}！之后就可以用 @bot 离店 等功能啦～"
    return (
        f"绑定失败：{message or '验证码无效'}。请到会馆网站「个人信息」页重新获取验证码，"
        "并确认「待绑QQ」填的是你发消息的这个 QQ。"
    )
