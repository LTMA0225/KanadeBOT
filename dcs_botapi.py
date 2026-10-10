"""音游窝 · 统一 Bot API 客户端（v3.0.0）。

站点开发者提供的统一机器人接口（网站「高级管理 → Bot 管理」按 bot 分配明钥与权限）：

POST /api/botAction
  Content-Type: application/json   （站内是严格字符串匹配，不能带 charset）
  body: { "qqid": "<操作者QQ>", "key": "<Bot明钥>", "action": "<动作>", "payload": "<字符串>" }

响应：
  成功: { "success": true,  "data": <结果> }
  失败: { "success": false, "message": "<原因>" }   （HTTP 400/401/403/415/500）

动作（随站点更新扩展）：getPresentUsers / purchase / getBalance / bindQq。
本模块不依赖 astrbot；解析函数为纯函数，便于离线测试。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from .dcs_presence import map_role
except ImportError:  # pragma: no cover - 目录导入方式
    from dcs_presence import map_role

FLT = timezone(timedelta(hours=8))


def _parse_iso_ms(value: Any) -> Optional[int]:
    """解析站点返回的时间（ISO 字符串，兼容 $D 前缀与无时区写法）。"""
    if not isinstance(value, str) or not value:
        return None
    text = value[2:] if value.startswith("$D") else value
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FLT)
    return int(dt.timestamp() * 1000)


def parse_present_users(data: Any) -> Optional[List[Dict[str, Any]]]:
    """把 getPresentUsers 的返回整理成快照用户列表。

    返回 [{"key","id","name","role","entered_ms","multiplier","balance","qqid"}, ...]；
    格式不符返回 None（调用方按数据异常处理）。
    注意：只提取需要的字段，站点返回中的 password 等敏感字段一律不保留。
    """
    if not isinstance(data, list):
        return None
    users: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("nickname") or "").strip()
        uid = item.get("id")
        entered = _parse_iso_ms(item.get("enteredAt"))
        if not name or uid is None or entered is None:
            continue
        users.append({
            "key": f"id:{uid}",
            "id": int(uid),
            "name": name,
            "role": map_role(item.get("role")),
            "entered_ms": entered,
            "multiplier": item.get("chargeMultiplier"),
            "balance": item.get("balance"),
            "qqid": str(item.get("qqid") or "").strip(),
        })
    return users


async def bot_action(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    key: str,
    qqid: str,
    action: str,
    payload: str = "",
) -> Dict[str, Any]:
    """调用统一 Bot API。

    统一返回结构：
      成功:     {"ok": True,  "data": <结果>}
      失败:     {"ok": False, "error": "<代码>", "message": "<站点文案>"}
    错误代码: UNAUTHORIZED / FORBIDDEN / BAD_REQUEST / SERVER_ERROR / http_<状态码> / network
    """
    import aiohttp

    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    body = {"qqid": str(qqid), "key": key, "action": action, "payload": str(payload or "")}
    last_err: Dict[str, Any] = {"ok": False, "error": "network", "message": "连接会馆网站失败，请稍后再试"}

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        if not route.secure:
            continue
        url = route.origin + "/api/botAction"
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with api._request(
                    session,
                    "POST",
                    url,
                    route.connect_ip,
                    json=body,
                    headers={"User-Agent": api.USER_AGENT},
                ) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else None
                    except Exception:  # noqa: BLE001 - 非 JSON
                        data = None
                    if not isinstance(data, dict):
                        last_err = {"ok": False, "error": f"http_{resp.status}", "message": "网站返回异常"}
                        continue
                    if resp.status == 200 and data.get("success"):
                        return {"ok": True, "data": data.get("data")}
                    error = {
                        400: "BAD_REQUEST",
                        401: "UNAUTHORIZED",
                        403: "FORBIDDEN",
                        415: "BAD_REQUEST",
                        500: "SERVER_ERROR",
                    }.get(resp.status, f"http_{resp.status}")
                    last_err = {"ok": False, "error": error, "message": str(data.get("message") or "")}
                    # 鉴权 / 权限 / 参数问题换线路也没用，直接返回
                    if resp.status in (400, 401, 403, 415):
                        return last_err
        except Exception:  # noqa: BLE001 - 换下一条线路
            continue
    return last_err


def public_api_error(result: Dict[str, Any]) -> str:
    """把失败结果整理成群内可见的通用提示（不含地址、密钥等内部信息）。"""
    code = str((result or {}).get("error") or "")
    if code == "NO_KEY":
        return "机器人未配置 Bot 明钥，请联系维护者。"
    if code == "UNAUTHORIZED":
        return "机器人密钥无效，请联系维护者。"
    if code == "FORBIDDEN":
        return "机器人缺少该功能权限，请联系维护者。"
    if code == "network":
        return "站点暂时无法访问，请稍后再试。"
    if code == "BAD_DATA":
        return "站点数据异常，请稍后再试。"
    return "站点返回异常，请稍后再试。"
