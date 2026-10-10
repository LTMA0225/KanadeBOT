"""音游窝 · /bd QQ 绑定支持模块（v3.0.0）。

绑定流程（复用站点现有逻辑）：
1. 玩家在网站「个人信息」页填写 QQ 发起绑定 → 页面显示"绑定验证码"；
2. 玩家在群里发送 "/bd 验证码"（或 "/bd验证码"）；
3. 机器人调用统一 Bot API 的 bindQq 动作（按发送者 QQ + 验证码校验）完成绑定。

安全：只使用发送者自己的 QQ 作为待绑 QQ —— 只有 QQ 本人能完成自己账号的绑定。
本模块不依赖 astrbot；纯函数便于离线测试。
"""

from __future__ import annotations

import re
from typing import Any, Dict

# 站点验证码 = base260（小写字母+数字交替，32 位），这里放宽到 8-64 位字母数字
_CODE_RE = re.compile(r"^[a-z0-9]{8,64}$")


def normalize_bind_code(raw: str) -> str:
    """去掉所有空白并转小写（站点验证码为小写字母+数字）。"""
    return "".join(str(raw or "").split()).lower()


def is_valid_bind_code(code: str) -> bool:
    return bool(_CODE_RE.match(code or ""))


def format_bind_reply(result: Dict[str, Any], qq: str = "") -> str:
    """把统一 Bot API 的返回整理成回复文本。

    result 为 bot_action 的统一结构：{"ok": True, "data": ...} 或
    {"ok": False, "error": ..., "message": ...}。
    注意：bindQq 的"验证码错误"以 data.message 形式返回（success 仍为 true），需按文案判定。
    """
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
