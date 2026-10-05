"""直流会馆 进/离店播报支持模块（v2.1.0）。

数据来源与方案：
- 复用 dcs_api 的抓取链路轮询 /whosin 页面，从页面内嵌的 Next.js RSC 数据
  （self.__next_f.push）中解析精确用户快照：id / 昵称 / 角色 / 入店时间（ISO）/
  计费倍率；按 id 差分判定进店与离店。
- 计费表从站点 /chargecalc 页面 RSC 中解析（时段费率 / 全局折扣 / 封顶规则），
  本地按站点源码 lib/pricing.ts 的 calculateCharge 逻辑 1:1 复刻费用计算，
  离店时再乘玩家个人倍率（chargeMultiplier）。

本模块除抓取辅助外不依赖 astrbot，便于独立测试；抓取辅助延迟导入 dcs_api。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

FLT = timezone(timedelta(hours=8))

# 站点角色（Prisma Role 枚举）-> 插件内部角色
_ROLE_MAP = {
    "CUSTOMER": "player",
    "STAFF": "staff",
    "ADMIN": "admin",
}

# 内置兜底计费表（2026-09-17 生效版本；正常运行时自动从站点抓取，站点调价自动跟随）
DEFAULT_PRICING: Dict[str, Any] = {
    "validFrom": "2026-09-17T00:00:00.000+08:00",
    "admissionBalance": 1000,
    "globalDiscount": 0.7,
    "circadyRates": [
        {"startMinute": 0, "endMinute": 240, "maxout": None, "rate": 25,
         "globalDiscountApply": True, "comment": "平价"},
        {"startMinute": 240, "endMinute": 600, "maxout": None, "rate": 10,
         "globalDiscountApply": False, "comment": "舞萌维护期+早鸟激励"},
        {"startMinute": 600, "endMinute": 1320, "maxout": 240, "rate": 25,
         "globalDiscountApply": True, "comment": "白天4小时封顶"},
        {"startMinute": 1320, "endMinute": 1440, "maxout": None, "rate": 30,
         "globalDiscountApply": True, "comment": "黄金时段"},
    ],
}

# Next.js RSC 数据块：self.__next_f.push([1,"<JSON 字符串转义的内容>"])
_NEXT_PUSH = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')

_DAY_MS = 86400000
_FLT_OFFSET_MS = 8 * 3600 * 1000


def extract_next_data(html: str) -> str:
    """把所有 RSC push 块解转义并拼接为一段可检索文本。"""
    if not html or "__next_f" not in html:
        return ""
    parts: List[str] = []
    for match in _NEXT_PUSH.finditer(html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except Exception:  # noqa: BLE001 - 单块损坏不影响其它块
            continue
    return "".join(parts)


def _scan_json_object(text: str, brace_pos: int) -> Optional[str]:
    """从 { 位置起做花括号配对，返回完整 JSON 对象文本（考虑字符串与转义）。"""
    if brace_pos < 0 or brace_pos >= len(text) or text[brace_pos] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(brace_pos, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_pos:i + 1]
    return None


def _parse_iso_ms(value: Any) -> Optional[int]:
    """解析 RSC 中的时间（可能带 Next.js 的 $D 前缀）；失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    text = value[2:] if value.startswith("$D") else value
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FLT)
    return int(dt.timestamp() * 1000)


def parse_whosin_snapshot(html: str) -> Optional[List[Dict[str, Any]]]:
    """从 /whosin 页面解析在店用户快照。

    返回 [{"key","id","name","role","entered_ms","multiplier","balance"}, ...]；
    页面里没有可用的 RSC 用户数据时返回 None（调用方降级为文本解析）。
    """
    text = extract_next_data(html)
    if not text:
        return None
    users: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for match in re.finditer(r'"user"\s*:\s*\{', text):
        frag = _scan_json_object(text, match.end() - 1)
        if not frag:
            continue
        try:
            obj = json.loads(frag)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(obj, dict) or "nickname" not in obj:
            continue
        name = str(obj.get("nickname") or "").strip()
        entered = _parse_iso_ms(obj.get("enteredAt"))
        if not name or entered is None:
            # 只有带 enteredAt 的对象才是在店用户；其余（如普通用户对象）忽略
            continue
        uid = obj.get("id")
        key = f"id:{uid}" if uid is not None else f"name:{name}"
        if key not in users:
            order.append(key)
        users[key] = {
            "key": key,
            "id": uid,
            "name": name,
            "role": _ROLE_MAP.get(str(obj.get("role") or "CUSTOMER"), "player"),
            "entered_ms": entered,
            "multiplier": obj.get("chargeMultiplier"),
            "balance": obj.get("balance"),
        }
    if not users:
        return None
    return [users[key] for key in order]


def snapshot_from_users(users: Any) -> List[Dict[str, Any]]:
    """降级方案：用 dcs_api 已解析的 VenueUser（HTML 文本）生成快照。"""
    now_ms = int(time.time() * 1000)
    result: List[Dict[str, Any]] = []
    for user in users or []:
        minutes = getattr(user, "minutes", None)
        entered = now_ms - int(minutes) * 60000 if minutes is not None else None
        role = getattr(user, "role", "player")
        result.append({
            "key": f"name:{user.name}",
            "id": None,
            "name": user.name,
            "role": role,
            "entered_ms": entered,
            "multiplier": 1.0,
            "balance": None,
        })
    return result


def parse_pricing(html: str) -> Optional[Dict[str, Any]]:
    """从页面（/chargecalc 或 /manage）RSC 中解析计费表。"""
    text = extract_next_data(html)
    if not text:
        return None
    for match in re.finditer(r'"pricing"\s*:\s*\{', text):
        frag = _scan_json_object(text, match.end() - 1)
        if not frag:
            continue
        try:
            obj = json.loads(frag)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and obj.get("circadyRates"):
            return obj
    return None


def _clock_minutes(ms: int) -> int:
    """FLT（UTC+8）挂钟分钟数（0-1439）。"""
    return ((ms + _FLT_OFFSET_MS) // 60000) % 1440


def _day_start_ms(ms: int) -> int:
    """ms 所在 FLT 日历日的零点毫秒。"""
    return ((ms + _FLT_OFFSET_MS) // _DAY_MS) * _DAY_MS - _FLT_OFFSET_MS


def calculate_charge_fen(
    entered_ms: int,
    left_ms: int,
    pricing: Dict[str, Any],
    multiplier: float = 1.0,
) -> float:
    """按站点 lib/pricing.ts 的 calculateCharge 复刻计算费用（单位：分）。

    - 分段计时：每段时间按所在时段的 rate（分/分钟）× 折扣累计；
    - globalDiscountApply 为假的时段不打折；globalDiscount 为 0 时全局免费；
    - maxout 为该时段单次封顶分钟数（如白天 4 小时封顶）；
    - 跨零点自动进入次日的时段循环。
    返回值乘以玩家个人倍率（负数按绝对值，0 = 免费）。
    """
    segments = [s for s in (pricing.get("circadyRates") or []) if isinstance(s, dict)]
    try:
        global_discount = float(pricing.get("globalDiscount") or 0)
    except (TypeError, ValueError):
        global_discount = 0.0
    if not segments or left_ms <= entered_ms:
        return 0.0

    leave_min = _clock_minutes(left_ms)
    leave_day = _day_start_ms(left_ms)
    cursor_min = _clock_minutes(entered_ms)
    cursor_day = _day_start_ms(entered_ms)
    total = 0.0

    for _ in range(64):  # 防御：最多遍历 64 天
        segment = None
        for s in segments:
            start = int(s.get("startMinute", 0))
            end = int(s.get("endMinute", 0))
            if start <= cursor_min < end:
                segment = s
                break
        if segment is None:
            break
        rate = float(segment.get("rate", 0) or 0)
        apply_discount = bool(segment.get("globalDiscountApply"))
        discount = global_discount if (apply_discount or global_discount == 0) else 1.0

        if cursor_day == leave_day and leave_min <= int(segment.get("endMinute", 0)):
            duration = leave_min - cursor_min
            if duration > 0:
                maxout = segment.get("maxout")
                capped = min(duration, int(maxout)) if maxout else duration
                total += capped * rate * discount
            break

        next_min = int(segment.get("endMinute", 0))
        duration = next_min - cursor_min
        if duration > 0:
            maxout = segment.get("maxout")
            capped = min(duration, int(maxout)) if maxout else duration
            total += capped * rate * discount
        cursor_min = next_min
        if cursor_min >= 1440:
            cursor_min = 0
            cursor_day += _DAY_MS

    try:
        mult = abs(float(multiplier)) if multiplier is not None else 1.0
    except (TypeError, ValueError):
        mult = 1.0
    if mult == 0:
        return 0.0
    return total * mult


def format_fee_yuan(fen: float) -> str:
    """分 -> 元（保留两位小数）。"""
    return f"{fen / 100:.2f}"


async def fetch_pricing_html(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    cookie: str,
    username: str,
    password: str,
) -> Optional[str]:
    """按 dcs_api 的线路与登录链路抓取计费表页面 HTML；失败返回 None。"""
    try:
        from . import dcs_api as api  # type: ignore
    except ImportError:  # pragma: no cover - 目录导入方式
        import dcs_api as api  # type: ignore

    for route in api._build_routes(list(urls), list(fallback_ips or [])):
        auth, kind, _err = await api._resolve_cookie(route, cookie, username, password, timeout)
        if kind == "login_failed":
            return None
        if kind:
            continue
        body, final_url, kind, _err = await api._fetch_html(route, timeout, auth)
        if kind or not body:
            continue
        if api._looks_like_login(final_url, body):
            continue
        return body
    return None
