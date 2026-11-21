
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

FLT = timezone(timedelta(hours=8))

_RSC_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')

_CONSOLE_FALLBACK = {
    "mm_1": "舞萌6号",
    "cn_nya_1": "猫台1号",
}

def _origin(base_url: str) -> str:
    parts = urlsplit(str(base_url or ""))
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"

def _console_desc(name: str, desc: Any = "") -> str:
    text = str(desc or "").strip()
    if text:
        return text
    key = str(name or "").strip()
    return _CONSOLE_FALLBACK.get(key, key)

def _parse_iso_ms(value: Any) -> Optional[int]:
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

def _flatten_rsc(html: str) -> str:
    parts: List[str] = []
    for match in _RSC_PUSH_RE.finditer(html or ""):
        raw = match.group(1)
        try:
            parts.append(json.loads('"' + raw + '"'))
        except Exception:
            parts.append(raw)
    if parts:
        return "".join(parts)
    return html or ""

_MARKER = '"consoleDesc":"'

def _scan_object(text: str, start: int, end: int) -> Optional[Dict[str, Any]]:
    """从 consoleDesc 位置往回找对象边界，向前找同名 prop，拼出时间与机台。"""
    head = text.rfind('{"booking":{', max(0, start - 1200), start)
    if head < 0:
        return None
    if text.find('"users":', head, end + 200) < 0:
        return None
    window = text[head:start].rstrip().rstrip(",")
    start_ms = None
    end_ms = None
    try:
        obj = json.loads(window + "}")
        if not isinstance(obj, dict):
            obj = {}
    except Exception:
        obj = {}
        for field in ("startTime", "endTime", "console"):
            m = re.search(r'"%s":"([^"]*)"' % field, window)
            if m:
                obj[field] = m.group(1)
    booking = obj.get("booking") if isinstance(obj, dict) else None
    if isinstance(booking, dict):
        start_ms = _parse_iso_ms(booking.get("startTime"))
        end_ms = _parse_iso_ms(booking.get("endTime"))
        console = str(booking.get("console") or "").strip()
    else:
        console = ""
    desc_match = re.search(r'"consoleDesc":"((?:[^"\\]|\\.)*)"', text[start:end + 200])
    desc = ""
    if desc_match:
        try:
            desc = json.loads('"' + desc_match.group(1) + '"')
        except Exception:
            desc = desc_match.group(1)
    result: Dict[str, Any] = {"machine": _console_desc(console, desc)}
    if start_ms is not None:
        result["start_ms"] = start_ms
    if end_ms is not None:
        result["end_ms"] = end_ms
    return result

def parse_booking_page(html: str) -> List[Dict[str, Any]]:
    text = _flatten_rsc(html)
    found: List[Dict[str, Any]] = []
    seen = set()
    for match in re.finditer(re.escape(_MARKER), text):
        block = _scan_object(text, match.start(), match.start())
        if not block:
            continue
        start_ms = block.get("start_ms")
        end_ms = block.get("end_ms")
        if start_ms is None or end_ms is None:
            continue
        machine = block.get("machine") or ""
        token = (machine, start_ms, end_ms)
        if token in seen:
            continue
        seen.add(token)
        found.append({"machine": machine, "start_ms": int(start_ms), "end_ms": int(end_ms)})
    found.sort(key=lambda item: item["start_ms"])
    return found

def format_window_short(start_ms: int, end_ms: int, now_ms: Optional[int] = None) -> str:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    start = datetime.fromtimestamp(int(start_ms) / 1000, FLT)
    end = datetime.fromtimestamp(int(end_ms) / 1000, FLT)
    now = datetime.fromtimestamp(now_ms / 1000, FLT)
    today = now.date()
    if now.date() == start.date() and now.date() == end.date():
        day_text = "今天"
    else:
        day_text = f"{start.month}/{start.day}"
    if start.date() == end.date():
        return f"{day_text} {start:%H:%M}~{end:%H:%M}"
    return f"{day_text} {start:%H:%M}~{end.month}/{end.day} {end:%H:%M}"

def pick_display(
    bookings: List[Dict[str, Any]],
    now_ms: Optional[int] = None,
    lookahead_ms: int = 2 * 3600 * 1000,
    limit: int = 3,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回（正在进行/临近的包场, 今天余下时段的其他包场）。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    today = datetime.fromtimestamp(now_ms / 1000, FLT).date()
    now_list: List[Dict[str, Any]] = []
    later_list: List[Dict[str, Any]] = []
    for item in bookings or []:
        try:
            start = int(item.get("start_ms") or 0)
            end = int(item.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if end <= now_ms:
            continue
        row = dict(item)
        row["running"] = start <= now_ms < end
        row["window"] = format_window_short(start, end, now_ms)
        if row["running"]:
            now_list.append(row)
        elif start - now_ms <= lookahead_ms:
            now_list.append(row)
        elif datetime.fromtimestamp(start / 1000, FLT).date() == today:
            later_list.append(row)
    now_list.sort(key=lambda i: (0 if i.get("running") else 1, i.get("start_ms", 0)))
    return now_list[:limit], later_list[:max(0, limit - len(now_list))]

async def fetch_booking_html(
    target_urls: List[str],
    timeout: int = 10,
    cookie: str = "",
    username: str = "",
    password: str = "",
) -> Optional[str]:
    try:
        from . import dcs_api as api
    except ImportError:
        import dcs_api as api

    for base_url in list(target_urls or []):
        origin = _origin(base_url)
        if not origin:
            continue
        url = origin + "/userbooking"
        for route in api._build_routes([url], []):
            if not route.secure:
                continue
            try:
                auth, _kind, _err = await api._resolve_cookie(route, cookie, username, password, timeout)
                body, _final, kind, _err = await api._fetch_html(route, timeout, auth)
            except Exception:
                continue
            if kind or not body:
                continue
            return body
    return None
