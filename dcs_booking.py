
from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

FLT = timezone(timedelta(hours=8))

_BOOKING_RE = re.compile(
    r"^包(?P<kind>机|场)\s*(?P<machine>.+?)\s*"
    r"(?:(?P<month>\d{1,2})月)?(?:(?P<day>\d{1,2})日|(?P<rel>今天|明天|后天))\s*"
    r"(?P<start_h>\d{1,2})(?:[:：](?P<start_m>\d{2}))?\s*[时点]?\s*"
    r"[~～\-—－到至]\s*"
    r"(?P<end_h>\d{1,2})(?:[:：](?P<end_m>\d{2}))?\s*[时点]?\s*$"
)

_REL_DAYS = {"今天": 0, "明天": 1, "后天": 2}

_MAX_WINDOW_HOURS = 24

_USAGE = (
    "格式示例：包场 舞萌 10日14时~16时\n"
    "（支持 包机/包场、X月X日 或 今天/明天/后天、X时 或 X:XX、可跨零点如 22时~2时）"
)

def _midnight_flt(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=FLT).timestamp() * 1000)

def parse_booking(
    text: str, now_ms: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw = str(text or "").strip()
    match = _BOOKING_RE.match(raw)
    if not match:
        return None, _USAGE

    machine = str(match.group("machine") or "").strip()
    if not machine:
        return None, "没有识别到机台名称。" + _USAGE

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    now = datetime.fromtimestamp(now_ms / 1000, FLT)

    rel = match.group("rel")
    month_text = match.group("month")
    day_text = match.group("day")
    year = now.year

    if rel:
        target = now + timedelta(days=_REL_DAYS[rel])
        year, month, day = target.year, target.month, target.day
    else:
        day = int(day_text)
        month = int(month_text) if month_text else now.month
        if not 1 <= month <= 12:
            return None, "月份不对（1-12）"
        if not 1 <= day <= 31:
            return None, "日期不对（1-31）"
        days_in_month = calendar.monthrange(year, month)[1]
        if day > days_in_month:
            return None, f"{month}月没有{day}日"
        base = _midnight_flt(year, month, day)
        if base + 24 * 3600 * 1000 < now_ms:
            if month_text:
                year += 1
            else:
                month += 1
                if month > 12:
                    month, year = 1, year + 1
            days_in_month = calendar.monthrange(year, month)[1]
            if day > days_in_month:
                return None, f"{month}月没有{day}日"

    start_h = int(match.group("start_h"))
    start_m = int(match.group("start_m") or 0)
    end_h = int(match.group("end_h"))
    end_m = int(match.group("end_m") or 0)
    for label, hh, mm in (("开始", start_h, start_m), ("结束", end_h, end_m)):
        if not 0 <= hh <= 30:
            return None, f"{label}时间不对（0-30 时）"
        if not 0 <= mm <= 59:
            return None, f"{label}的分钟不对"

    midnight = _midnight_flt(year, month, day)
    start_ms = midnight + start_h * 3600000 + start_m * 60000
    end_ms = midnight + end_h * 3600000 + end_m * 60000
    if end_ms <= start_ms:
        end_ms += 24 * 3600000
    if end_ms - start_ms > _MAX_WINDOW_HOURS * 3600000:
        return None, "单次包场不能超过 24 小时"

    return {
        "kind": match.group("kind"),
        "machine": machine,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }, None

def active_bookings(
    bookings: List[Dict[str, Any]], now_ms: Optional[int] = None
) -> List[Dict[str, Any]]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    result: List[Dict[str, Any]] = []
    for item in bookings or []:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start_ms") or 0)
            end = int(item.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if start <= now_ms < end:
            result.append(item)
    return result

def prune_bookings(
    bookings: List[Dict[str, Any]], now_ms: Optional[int] = None
) -> List[Dict[str, Any]]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    grace = 24 * 3600000
    return [
        item
        for item in (bookings or [])
        if isinstance(item, dict) and int(item.get("end_ms") or 0) >= now_ms - grace
    ]

def format_window(start_ms: int, end_ms: int) -> str:
    start = datetime.fromtimestamp(int(start_ms) / 1000, FLT)
    end = datetime.fromtimestamp(int(end_ms) / 1000, FLT)
    if start.date() == end.date():
        return f"{start.month}月{start.day}日 {start:%H:%M}~{end:%H:%M}"
    return f"{start.month}月{start.day}日 {start:%H:%M}~{end.month}月{end.day}日 {end:%H:%M}"

def format_booking_notice(bookings: List[Dict[str, Any]], prefix: str = "") -> str:
    lines = [f"{prefix}本时段正有包场（包机）"]
    for item in bookings or []:
        try:
            window = format_window(int(item.get("start_ms") or 0), int(item.get("end_ms") or 0))
        except (TypeError, ValueError):
            window = ""
        lines.append(f"· {item.get('machine', '')} {window}")
    lines.append("（期间不提供在店人数查询）")
    return "\n".join(lines)

def format_booking_confirm(booking: Dict[str, Any]) -> str:
    kind_text = "包场" if str(booking.get("kind")) == "场" else "包机"
    window = format_window(int(booking.get("start_ms") or 0), int(booking.get("end_ms") or 0))
    return (
        f"✅ 已登记{kind_text}\n"
        f"机台：{booking.get('machine', '')}\n"
        f"时间：{window}\n"
        "（期间群内 /j 将提示包场，不放出在店名单；误登记可发「取消包场」）"
    )
