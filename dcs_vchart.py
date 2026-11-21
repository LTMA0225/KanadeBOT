"""v.wmc.pub 谱面平台只读客户端（给 chart_lookup 补充评分/物量/标签/评论）。

接口：https://v.wmc.pub/api/v1/  鉴权：Authorization: Bearer <mk_live_...>
平台难度从 2 起：2=BASIC 3=ADVANCED 4=EXPERT 5=MASTER 6=Re:MASTER；kind 为 standard / dx。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://v.wmc.pub/api/v1"
# 公共只读键，配置里留空时兜底用
DEFAULT_KEY = "mk_live_y6uXzBNFR7mlUlxb9Y65-c9FG-oCNrIB"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TIMEOUT = 20
RETRIES = 2

DIFF_ALIAS = {
    "b": 2, "basic": 2, "2": 2,
    "a": 3, "advanced": 3, "3": 3,
    "e": 4, "expert": 4, "4": 4,
    "m": 5, "master": 5, "5": 5,
    "r": 6, "remaster": 6, "re:master": 6, "6": 6,
}
DIFF_NAME = {2: "BASIC", 3: "ADVANCED", 4: "EXPERT", 5: "MASTER", 6: "Re:MASTER"}

_cache: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL = 600
_last_call = 0.0
_MIN_GAP = 0.0


class VChartError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def difficulty_from_level_label(label: str) -> Optional[int]:
    """从水鱼档位里猜难度：BASIC/ADVANCED/EXPERT/MASTER/Re:MASTER。"""
    text = str(label or "").upper()
    if "RE:MASTER" in text or "REMASTER" in text:
        return 6
    for name, diff in (("BASIC", 2), ("ADVANCED", 3), ("EXPERT", 4), ("MASTER", 5)):
        if name in text:
            return diff
    return None


def parse_diff(token: str) -> int:
    return DIFF_ALIAS.get(str(token or "").strip().lower(), 0)


async def _get(path: str, params: Optional[Dict[str, Any]] = None, key: str = "") -> Any:
    global _last_call
    import aiohttp

    key = str(key or "").strip() or DEFAULT_KEY

    url = BASE + "/" + path.lstrip("/")
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            from urllib.parse import urlencode

            url += "?" + urlencode(clean, doseq=True)
    cache_key = ("k" if key else "anon") + "|" + url
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    headers = {"Accept": "application/json", "User-Agent": UA}
    if key:
        headers["Authorization"] = "Bearer " + key
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    last: Optional[VChartError] = None
    for attempt in range(RETRIES + 1):
        wait = _MIN_GAP - (time.time() - _last_call)
        if wait > 0:
            await asyncio.sleep(min(wait, 3))
        _last_call = time.time()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status == 200:
                        data = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
                        _cache[cache_key] = (time.time(), data)
                        return data
                    try:
                        msg = json.loads(raw).get("message") or raw[:200]
                    except Exception:
                        msg = raw[:200] or str(resp.status)
                    last = VChartError(resp.status, str(msg))
                    if resp.status in (401, 404):
                        raise last
                    if resp.status == 429:
                        await asyncio.sleep(2 + attempt * 2)
                        continue
                    if resp.status >= 500 and attempt < RETRIES:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise last
        except VChartError:
            raise
        except asyncio.TimeoutError:
            last = VChartError(0, "超时")
        except Exception as exc:  # 网络错误
            last = VChartError(0, f"网络错误：{exc}")
        if attempt < RETRIES:
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last or VChartError(0, "未知错误")


async def search_charts(keyword: str, key: str = "", kind: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    data = await _get("charts", {"q": keyword, "kind": kind, "limit": limit}, key)
    items = data.get("items", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict)]


async def chart_by_song_id(song_id: int, diff: int, key: str = "", kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    kinds = [kind] if kind in ("standard", "dx") else ["dx", "standard"]
    for k in kinds:
        chart_key = f"{song_id}:{k}:{diff}"
        try:
            data = await _get("charts/" + chart_key, None, key)
        except VChartError as exc:
            if exc.status == 404:
                continue
            raise
        if isinstance(data, dict) and data.get("chartKey"):
            return data
    return None


def _chart_key_of(chart: Dict[str, Any]) -> str:
    return str(chart.get("chartKey") or "")


def normalize_title(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


async def match_chart(title: str, diff: int, key: str = "", kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按曲名 + 难度找唯一谱面。"""
    want = normalize_title(title)
    if not want:
        return None
    items = await search_charts(str(title or "").strip()[:60], key, kind, 20)
    exact = [i for i in items if i.get("difficulty") == diff and normalize_title(i.get("title", "")) == want]
    loose = [i for i in items if i.get("difficulty") == diff and want in normalize_title(i.get("title", ""))]
    pick = (exact or loose or [None])[0]
    if not pick:
        return None
    chart_key = _chart_key_of(pick)
    if not chart_key:
        return None
    try:
        data = await _get("charts/" + chart_key, None, key)
    except VChartError:
        return pick
    return data if isinstance(data, dict) else pick


async def fetch_tags(chart_key: str, key: str = "", radar: int = 50, feature: float = 0.5, pattern: int = 1) -> Dict[str, Any]:
    data = await _get(
        f"charts/{chart_key}/tags",
        {"radar_threshold": radar, "feature_threshold": feature, "pattern_threshold": pattern},
        key,
    )
    return data if isinstance(data, dict) else {}


async def fetch_comments(chart_key: str, key: str = "", limit: int = 3) -> List[Dict[str, Any]]:
    data = await _get(f"charts/{chart_key}/comments", {"limit": limit, "page": 1}, key)
    items = data.get("items", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict)][:limit]


async def fetch_scores(chart_key: str, key: str = "", limit: int = 3) -> Dict[str, Any]:
    data = await _get(f"charts/{chart_key}/scores", {"limit": limit}, key)
    return data if isinstance(data, dict) else {}
