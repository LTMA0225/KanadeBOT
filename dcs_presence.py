
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

FLT = timezone(timedelta(hours=8))

_ROLE_MAP = {
    "CUSTOMER": "player",
    "STAFF": "staff",
    "ADMIN": "admin",
}

DEFAULT_PRICING: Dict[str, Any] = {
    "validFrom": "2026-11-15T00:00:00.000+08:00",
    "admissionBalance": 1000,
    "globalDiscount": 1.0,
    "capStartMinute": 600,
    "capEndMinute": 1680,
    "capFullMinutes": 240,
    "capHalfMinutes": 240,
    "weekendDays": [5, 6],
    "holidayDates": [],
    "circadyRates": [
        {"startMinute": 0, "endMinute": 240, "maxout": None, "rate": 25,
         "globalDiscountApply": True, "comment": "夜间"},
        {"startMinute": 240, "endMinute": 600, "maxout": None, "rate": 10,
         "globalDiscountApply": True, "comment": "早鸟 6 元/小时"},
        {"startMinute": 600, "endMinute": 1080, "maxout": None, "rate": 20,
         "globalDiscountApply": True, "comment": "工作日白天 12 元/小时"},
        {"startMinute": 1080, "endMinute": 1440, "maxout": None, "rate": 25,
         "globalDiscountApply": True, "comment": "夜间 15 元/小时"},
    ],
    "weekendRates": [
        {"startMinute": 0, "endMinute": 240, "maxout": None, "rate": 25,
         "globalDiscountApply": True, "comment": "夜间 15 元/小时"},
        {"startMinute": 240, "endMinute": 600, "maxout": None, "rate": 10,
         "globalDiscountApply": True, "comment": "早鸟 6 元/小时"},
        {"startMinute": 600, "endMinute": 1440, "maxout": None, "rate": 25,
         "globalDiscountApply": True, "comment": "周末/节假日 15 元/小时"},
    ],
}

_NEXT_PUSH = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')

_DAY_MS = 86400000
_FLT_OFFSET_MS = 8 * 3600 * 1000

def extract_next_data(html: str) -> str:
    if not html or "__next_f" not in html:
        return ""
    parts: List[str] = []
    for match in _NEXT_PUSH.finditer(html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except Exception:
            continue
    return "".join(parts)

def _scan_json_object(text: str, brace_pos: int) -> Optional[str]:
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

def parse_pricing(html: str) -> Optional[Dict[str, Any]]:
    text = extract_next_data(html)
    if not text:
        return None
    for match in re.finditer(r'"pricing"\s*:\s*\{', text):
        frag = _scan_json_object(text, match.end() - 1)
        if not frag:
            continue
        try:
            obj = json.loads(frag)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("circadyRates"):
            return obj
    return None

def _clock_minutes(ms: int) -> int:
    return ((ms + _FLT_OFFSET_MS) // 60000) % 1440

def _day_start_ms(ms: int) -> int:
    return ((ms + _FLT_OFFSET_MS) // _DAY_MS) * _DAY_MS - _FLT_OFFSET_MS

def _segments_for(pricing: Dict[str, Any], day_start_ms: int) -> List[Dict[str, Any]]:
    try:
        day = datetime.fromtimestamp(day_start_ms / 1000, FLT)
        date_str = day.date().isoformat()
    except Exception:
        date_str = ""
    try:
        weekend_days = {int(x) for x in (pricing.get("weekendDays") or [5, 6])}
    except (TypeError, ValueError):
        weekend_days = {5, 6}
    try:
        weekday = datetime.fromtimestamp(day_start_ms / 1000, FLT).weekday()
    except Exception:
        weekday = 0
    holidays = {str(x)[:10] for x in (pricing.get("holidayDates") or [])}
    if weekday in weekend_days or date_str in holidays:
        rates = pricing.get("weekendRates")
        if isinstance(rates, list) and rates:
            return [s for s in rates if isinstance(s, dict)]
    return [s for s in (pricing.get("circadyRates") or []) if isinstance(s, dict)]


def calculate_charge_fen(
    entered_ms: int,
    left_ms: int,
    pricing: Dict[str, Any],
    multiplier: float = 1.0,
) -> float:
    if left_ms <= entered_ms:
        return 0.0
    try:
        global_discount = float(pricing.get("globalDiscount") or 0)
    except (TypeError, ValueError):
        global_discount = 0.0
    try:
        cap_start = int(pricing.get("capStartMinute", 600) or 600)
        cap_end = int(pricing.get("capEndMinute", 1680) or 1680)
        cap_full = max(0, int(pricing.get("capFullMinutes", 240) or 240))
        cap_half = max(0, int(pricing.get("capHalfMinutes", 240) or 240))
    except (TypeError, ValueError):
        cap_start, cap_end, cap_full, cap_half = 600, 1680, 240, 240
    usage: Dict[int, int] = {}
    total = 0.0
    cursor = int(entered_ms)
    while cursor < left_ms:
        day_start = _day_start_ms(cursor)
        minute = _clock_minutes(cursor)
        segments = _segments_for(pricing, day_start)
        rate = None
        applied = True
        for segment in segments:
            start = int(segment.get("startMinute", 0))
            end = int(segment.get("endMinute", 0))
            if start <= minute < end:
                rate = float(segment.get("rate", 0) or 0)
                applied = bool(segment.get("globalDiscountApply"))
                break
        if rate is None:
            cursor += 60000
            continue
        discount = global_discount if (applied or global_discount == 0) else 1.0
        window = None
        if minute >= cap_start:
            window = day_start
        elif minute < (cap_end - 1440):
            window = day_start - _DAY_MS
        factor = 1.0
        if window is not None:
            used = usage.get(window, 0)
            if used < cap_full:
                factor = 1.0
            elif used < cap_full + cap_half:
                factor = 0.5
            else:
                factor = 0.0
            usage[window] = used + 1
        total += rate * discount * factor
        cursor += 60000
    try:
        mult = abs(float(multiplier)) if multiplier is not None else 1.0
    except (TypeError, ValueError):
        mult = 1.0
    if mult == 0:
        return 0.0
    return total * mult


def format_fee_yuan(fen: float) -> str:
    return f"{fen / 100:.2f}"

def _format_clock(minute: int) -> str:
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"

def format_discount_zhe(ratio: float) -> str:
    raw = int(round(float(ratio) * 100))
    if raw % 10 == 0:
        return f"{raw // 10}折"
    return f"{raw // 10}.{raw % 10}折"

def current_rate_info(pricing: Dict[str, Any], now_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    minute = _clock_minutes(now)
    segments = _segments_for(pricing, _day_start_ms(now))
    if not segments:
        return None
    try:
        global_discount = float(pricing.get("globalDiscount") or 0)
    except (TypeError, ValueError):
        global_discount = 0.0
    for segment in segments:
        start = int(segment.get("startMinute", 0))
        end = int(segment.get("endMinute", 0))
        if not (start <= minute < end):
            continue
        rate = float(segment.get("rate", 0) or 0)
        applied = bool(segment.get("globalDiscountApply"))
        multiplier = global_discount if (applied or global_discount == 0) else 1.0
        base_fen = rate * 60
        return {
            "start": _format_clock(start),
            "end": "24:00" if end >= 1440 else _format_clock(end),
            "comment": str(segment.get("comment") or ""),
            "maxout": segment.get("maxout"),
            "base_fen": base_fen,
            "final_fen": base_fen * multiplier,
            "discount": multiplier,
            "cap_note": (
                "10:00-28:00 期间：第 4-8 小时半价，8 小时后不计费（封顶）"
                if (600 <= minute < 1440 or minute < 240)
                else ""
            ),
        }
    return None

def format_current_rate_message(
    pricing: Dict[str, Any], prefix: str = "", now_ms: Optional[int] = None
) -> Optional[str]:
    info = current_rate_info(pricing, now_ms)
    if not info:
        return None
    head = f"时段：{info['start']}-{info['end']}"
    if info["comment"]:
        head += f"（{info['comment']}）"
    hourly = f"¥{info['final_fen'] / 100:.2f}"
    if info["final_fen"] != info["base_fen"]:
        if info["final_fen"] == 0:
            hourly += "（限时免费）"
        else:
            hourly += f"（原价 ¥{info['base_fen'] / 100:.2f}，{format_discount_zhe(info['discount'])}）"
    lines = [f"{prefix}当前时段价格", head, f"每小时：{hourly}"]
    if info.get("cap_note"):
        lines.append(info["cap_note"])
    lines.append("包机/包场事项请看群公告【包机包场】栏")
    return "\n".join(lines)

def map_role(raw: Any) -> str:
    return _ROLE_MAP.get(str(raw or "CUSTOMER"), "player")

def visit_key(user_id: Any, entered_ms: Any) -> Optional[str]:
    try:
        if user_id is None or entered_ms is None:
            return None
        return f"{int(user_id)}:{int(entered_ms)}"
    except (TypeError, ValueError):
        return None

async def _fetch_authed_text(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    cookie: str,
    username: str,
    password: str,
) -> Optional[str]:
    try:
        from . import dcs_api as api
    except ImportError:
        import dcs_api as api

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

async def fetch_pricing_html(
    urls: List[str],
    fallback_ips: List[str],
    timeout: int,
    cookie: str,
    username: str,
    password: str,
) -> Optional[str]:
    return await _fetch_authed_text(urls, fallback_ips, timeout, cookie, username, password)

