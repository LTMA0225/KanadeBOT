
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

def calculate_charge_fen(
    entered_ms: int,
    left_ms: int,
    pricing: Dict[str, Any],
    multiplier: float = 1.0,
) -> float:
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

    for _ in range(64):
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
    segments = [s for s in (pricing.get("circadyRates") or []) if isinstance(s, dict)]
    if not segments:
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    minute = _clock_minutes(now)
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
    return "\n".join([f"{prefix}当前时段价格", head, f"每小时：{hourly}"])

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

