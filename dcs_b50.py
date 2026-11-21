from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

B50_API_URL = "https://maimai.diving-fish.com/api/maimaidxprober/query/player"
B50_COVER_URL = "https://www.diving-fish.com/covers/{song_id}.png"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) dcs-whosin-b50",
    "Accept": "application/json",
}

_OLD_SLOTS = 35
_NEW_SLOTS = 15

_RATE_LABELS = {
    "sssp": "SSS+",
    "sss": "SSS",
    "ssp": "SS+",
    "ss": "SS",
    "sp": "S+",
    "s": "S",
    "aaa": "AAA",
    "aa": "AA",
    "a": "A",
    "bbb": "BBB",
    "bb": "BB",
    "b": "B",
    "c": "C",
    "d": "D",
    "e": "E",
    "f": "F",
}

_FC_LABELS = {
    "fc": "FC",
    "fcp": "FC+",
    "ap": "AP",
    "app": "AP+",
}

_FS_LABELS = {
    "sync": "SYNC",
    "fs": "FS",
    "fsp": "FS+",
    "fsd": "FSD",
    "fsdp": "FSD+",
}

_LEVEL_COLORS = {0: "basic", 1: "advanced", 2: "expert", 3: "master", 4: "remaster"}

FLT = timezone(timedelta(hours=8))

_DX_CERT_HINT = (
    "查询失败：查分器那边没有找到这个玩家。\n"
    "用法：/b50 昵称 或 /b50 QQ号\n"
    "（想查自己的话直接 /b50 就行，会先按你的 QQ 查）\n"
    "还没绑定查分网的话，先去查分网把 QQ 填上，然后 /bd 验证码 绑定；"
    "或者直接在指令后面带上你的查分网昵称。"
)


class B50Error(Exception):
    pass


def _fmt_time(value: Optional[float]) -> str:
    return datetime.fromtimestamp(value or time.time(), FLT).strftime("%Y-%m-%d %H:%M")


def _rate_class(achievements: float) -> str:
    for border, name in (
        (100.5, "apex"),
        (100.0, "sssp"),
        (99.5, "sss"),
        (99.0, "ssp"),
        (98.0, "ss"),
        (97.0, "sp"),
        (94.0, "s"),
        (90.0, "aaa"),
        (80.0, "a"),
    ):
        if achievements >= border:
            return name
    return "other"


def _badge(text: str) -> str:
    return _FC_LABELS.get(text) or _FS_LABELS.get(text) or ""


def _chart_entry(chart: Dict[str, Any], cover: Optional[str]) -> Dict[str, Any]:
    try:
        achievements = float(chart.get("achievements") or 0)
    except (TypeError, ValueError):
        achievements = 0.0
    index = chart.get("level_index")
    return {
        "title": str(chart.get("title") or ""),
        "level": str(chart.get("level") or ""),
        "label": str(chart.get("level_label") or ""),
        "type": "DX" if str(chart.get("type") or "").upper() == "DX" else "SD",
        "ds": chart.get("ds"),
        "rate_label": _RATE_LABELS.get(str(chart.get("rate") or ""), ""),
        "rate_class": _rate_class(achievements),
        "achievements": f"{achievements:.4f}",
        "ra": int(chart.get("ra") or 0),
        "badge": _badge(str(chart.get("fc") or "")) or _badge(str(chart.get("fs") or "")),
        "level_class": _LEVEL_COLORS.get(index if isinstance(index, int) else -1, "master"),
        "cover": cover,
    }


_LXNS_SONG_LIST = "https://maimai.lxns.net/api/v0/maimai/song/list"
_LXNS_JACKET_URL = "https://assets.lxns.net/maimai/jacket/{cover_id}.png"


class _CoverLoader:
    """封面：先拿落雪歌曲表把 (曲名, 谱型, 难度) 换成封面 ID 再取图。

    内存 + 磁盘缓存；查分器封面挂了还有一层按 song_id 的兜底。
    慢请求直接放弃，缺图交给模板显示等级占位。
    """

    MAX_PARALLEL = 8
    PER_TIMEOUT = 4.0
    TOTAL_TIMEOUT = 12.0
    RETRY_TIMEOUT = 6.0
    PRIME_COUNT = 4
    MAP_TTL = 12 * 3600
    _HEADERS = _HEADERS

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._cache: Dict[str, Optional[str]] = {}
        self._map: Optional[Dict[tuple, int]] = None
        self._map_at = 0.0
        self._warm_task: Optional[asyncio.Task] = None

    def _disk_path(self, key: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.jpg"

    def _read_disk(self, key: str) -> Optional[str]:
        path = self._disk_path(key)
        if path is None or not path.exists():
            return None
        blob = path.read_bytes()
        if blob == b"-":
            return None
        return "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")

    def _write_disk(self, key: str, data: Optional[bytes]) -> None:
        path = self._disk_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if data else b"-")
        except OSError:
            pass

    def _map_file(self) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / "_song_index.json"

    async def _ensure_map(self, session: Any) -> Dict[tuple, int]:
        now = time.time()
        if self._map is not None and now - self._map_at < self.MAP_TTL:
            return self._map

        path = self._map_file()
        if self._map is None and path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                stamp = float(raw.get("at") or 0)
                if now - stamp < self.MAP_TTL:
                    loaded: Dict[tuple, int] = {}
                    for k, v in (raw.get("map") or {}).items():
                        parts = k.split("\\u0001")
                        if len(parts) == 3:
                            loaded[(parts[0], parts[1], int(parts[2]))] = int(v)
                    self._map = loaded
                    self._map_at = stamp
                    return self._map
            except (OSError, ValueError):
                pass

        mapping: Dict[tuple, int] = dict(self._map or {})
        try:
            async with session.get(_LXNS_SONG_LIST) as resp:
                if resp.status != 200:
                    raise ValueError(resp.status)
                data = await resp.json(content_type=None)
            for song in (data or {}).get("songs") or []:
                sid = int(song.get("id") or 0)
                title = str(song.get("title") or "").lower()
                if not sid or not title:
                    continue
                for kind, entries in (song.get("difficulties") or {}).items():
                    for entry in entries or []:
                        diff = entry.get("difficulty")
                        if not isinstance(diff, int):
                            continue
                        mapping[(title, "dx" if str(kind).lower() == "dx" else "sd", diff)] = sid
        except Exception:
            pass

        if mapping:
            self._map = mapping
            self._map_at = now
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(
                            {
                                "at": now,
                                "map": {
                            "\\u0001".join((k[0], k[1], str(k[2]))): v
                            for k, v in mapping.items()
                        },
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
        return mapping

    @staticmethod
    def _chart_key(chart: Dict[str, Any]) -> str:
        kind = "dx" if str(chart.get("type") or "").upper() == "DX" else "sd"
        diff = chart.get("level_index")
        raw = f"{kind}{diff if isinstance(diff, int) else -1}:{str(chart.get('title') or '').lower()}"
        return "s" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

    async def load_many(self, charts: List[Dict[str, Any]]) -> Dict[int, Optional[str]]:
        """charts 每项需要 title / type / level_index / song_id；返回按 song_id 索引的封面。"""
        import aiohttp

        if not charts:
            return {}

        pairs: List[tuple] = []
        seen: set = set()
        todo: List[tuple] = []
        for chart in charts:
            sid = int(chart.get("song_id") or 0)
            key = self._chart_key(chart)
            pairs.append((key, chart, sid))
            if key in seen or key in self._cache:
                continue
            seen.add(key)
            disk = self._read_disk(key)
            if disk is not None:
                self._cache[key] = disk
                continue
            todo.append((key, chart, sid))

        if todo:
            sem = asyncio.Semaphore(self.MAX_PARALLEL)

            async def one(
                session: Any, key: str, chart: Dict[str, Any], sid: int, tries: int = 1
            ) -> bool:
                for attempt in range(tries):
                    async with sem:
                        got = await self._fetch(session, chart, key, sid)
                    if got:
                        self._cache[key] = got
                        return True
                    if attempt + 1 < tries:
                        await asyncio.sleep(0.25)
                self._cache[key] = None
                return False

            connector = aiohttp.TCPConnector(
                limit=self.MAX_PARALLEL + 6, ttl_dns_cache=1800, keepalive_timeout=180
            )
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=self.PER_TIMEOUT, connect=self.PER_TIMEOUT
                    ),
                    headers={
                        "User-Agent": _HEADERS["User-Agent"],
                        "Referer": "https://maimai.lxns.net/",
                    },
                    connector=connector,
                ) as session:
                    await self._ensure_map(session)
                    # 先抓几张把 DNS/TLS 连接热起来，后面并发才快
                    head = todo[: self.PRIME_COUNT]
                    rest = todo[self.PRIME_COUNT :]
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(one(session, k, c, s_) for k, c, s_ in head),
                            return_exceptions=True,
                        ),
                        timeout=self.TOTAL_TIMEOUT,
                    )
                    failed: List[tuple] = []
                    if rest:
                        results = await asyncio.wait_for(
                            asyncio.gather(
                                *(one(session, k, c, s_) for k, c, s_ in rest),
                                return_exceptions=True,
                            ),
                            timeout=self.TOTAL_TIMEOUT,
                        )
                        for item, ok in zip(rest, results):
                            if ok is not True:
                                failed.append(item)
                    # 超时没拿到的再补一轮，尽量别留空
                    if failed:
                        await asyncio.wait_for(
                            asyncio.gather(
                                *(one(session, k, c, s_, tries=2) for k, c, s_ in failed),
                                return_exceptions=True,
                            ),
                            timeout=self.RETRY_TIMEOUT,
                        )
            except (asyncio.TimeoutError, Exception):
                pass

        missing = [(k, c, s_) for k, c, s_ in todo if self._cache.get(k) is None]
        if missing and (self._warm_task is None or self._warm_task.done()):
            self._warm_task = asyncio.create_task(self._warm(missing))

        return {sid: self._cache.get(key) for key, _, sid in pairs}

    async def _warm(self, items: List[tuple]) -> None:
        """冷门/超时的封面放到后台慢慢补，下次就命中了。"""
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                headers=self._HEADERS,
            ) as session:
                await self._ensure_map(session)
                for key, chart, sid in items:
                    if self._cache.get(key):
                        continue
                    got = await self._fetch(session, chart, key, sid)
                    if got:
                        self._cache[key] = got
        except Exception:
            pass

    async def _fetch(
        self, session: Any, chart: Dict[str, Any], key: str, song_id: int
    ) -> Optional[str]:
        kind = "dx" if str(chart.get("type") or "").upper() == "DX" else "sd"
        cover_id = (self._map or {}).get(
            (str(chart.get("title") or "").lower(), kind, chart.get("level_index"))
        )

        blob = None
        if cover_id:
            blob = await self._get(session, _LXNS_JACKET_URL.format(cover_id=cover_id))
        if blob is None and song_id:
            blob = await self._get(session, B50_COVER_URL.format(song_id=song_id))
        if blob is None:
            if not cover_id:
                self._write_disk(key, None)
            return None

        shrunk = _shrink_cover(blob)
        if shrunk:
            self._write_disk(key, base64.b64decode(shrunk.split(",", 1)[1]))
        return shrunk

    @staticmethod
    async def _get(session: Any, url: str) -> Optional[bytes]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                blob = await resp.read()
        except Exception:
            return None
        return blob or None


def _shrink_cover(blob: bytes) -> Optional[str]:
    try:
        from PIL import Image
    except ImportError:
        return "data:image/png;base64," + base64.b64encode(blob).decode("ascii")
    try:
        with Image.open(io.BytesIO(blob)) as img:
            img = img.convert("RGB")
            img.thumbnail((132, 132))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=78, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


async def fetch_player_data(username: str = "", qq: str = "") -> Dict[str, Any]:
    import aiohttp

    body: Dict[str, Any] = {"b50": True}
    if username:
        body["username"] = username
    if qq:
        body["qq"] = qq
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers=_HEADERS
        ) as session:
            async with session.post(B50_API_URL, json=body) as resp:
                if resp.status == 403:
                    raise B50Error("查分器把这次查询拒掉了，可能是太频繁，等一下再试吧。")
                if resp.status == 404:
                    raise B50Error(_DX_CERT_HINT)
                if resp.status >= 500:
                    raise B50Error("查分器暂时挂了，等一下再试吧。")
                data = await resp.json(content_type=None)
                if resp.status != 200 or not isinstance(data, dict):
                    raise B50Error(_DX_CERT_HINT)
    except B50Error:
        raise
    except asyncio.TimeoutError:
        raise B50Error("查分器超时了，等一下再试吧。")
    except Exception as exc:
        raise B50Error(f"连不上查分器（{type(exc).__name__}），等一下再试吧。")

    charts = data.get("charts")
    if not isinstance(charts, dict):
        raise B50Error(_DX_CERT_HINT)
    return data


async def build_b50_payload(
    username: str = "",
    qq: str = "",
    display_name: str = "",
    loader: Optional[_CoverLoader] = None,
) -> Dict[str, Any]:
    data = await fetch_player_data(username=username, qq=qq)

    old_charts = [c for c in (data.get("charts") or {}).get("sd") or [] if isinstance(c, dict)]
    new_charts = [c for c in (data.get("charts") or {}).get("dx") or [] if isinstance(c, dict)]
    old_charts.sort(key=lambda c: int(c.get("ra") or 0), reverse=True)
    new_charts.sort(key=lambda c: int(c.get("ra") or 0), reverse=True)
    old_charts = old_charts[:_OLD_SLOTS]
    new_charts = new_charts[:_NEW_SLOTS]

    loader = loader or _CoverLoader()
    covers = await loader.load_many(old_charts + new_charts)

    def entries(charts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [_chart_entry(c, covers.get(int(c.get("song_id") or 0))) for c in charts]

    old_entries = entries(old_charts)
    new_entries = entries(new_charts)
    old_sum = sum(item["ra"] for item in old_entries)
    new_sum = sum(item["ra"] for item in new_entries)

    nickname = str(data.get("nickname") or data.get("username") or display_name or "").strip()
    return {
        "name": nickname,
        "username": str(data.get("username") or ""),
        "display_name": display_name,
        "rating": int(data.get("rating") or 0),
        "additional_rating": int(data.get("additional_rating") or 0),
        "plate": str(data.get("plate") or ""),
        "time_text": _fmt_time(None),
        "old": old_entries,
        "new": new_entries,
        "old_sum": old_sum,
        "new_sum": new_sum,
        "old_count": len(old_entries),
        "new_count": len(new_entries),
        "total_ra": old_sum + new_sum,
    }


def format_b50_text(payload: Dict[str, Any]) -> str:
    name = payload.get("name") or payload.get("display_name") or ""
    return (
        f"Best50：{name}\n"
        f"rating {payload.get('rating')} ｜ 旧谱 {payload.get('old_sum')} + 新谱 {payload.get('new_sum')}"
        f" = {payload.get('total_ra')}"
    )
