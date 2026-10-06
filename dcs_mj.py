"""直流会馆 · /mj 表情包支持模块（v2.4.0）。

- 通过 NapCat 的 fetch_custom_face 拉取 bot 账号"收藏表情"（自定义表情）列表；
- 按配置取用其中一个，下载到插件数据目录做本地缓存
  （QQ 的下载链接带 rkey 会过期，缓存一次后长期稳定）；
- /mj 指令发送该表情（图片形式）；/mjlist 可列出编号方便选择。

本模块不依赖 astrbot；网络下载使用 aiohttp（与插件依赖一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional


def normalize_custom_faces(data: Any) -> List[str]:
    """把 fetch_custom_face 的返回整理成 URL 列表（兼容多种返回格式）。"""
    result: List[str] = []
    if isinstance(data, dict):
        for key in ("data", "list", "urls", "faces", "emojiList"):
            if key in data:
                return normalize_custom_faces(data[key])
        for key in ("url", "file", "path", "emojiUrl", "emoji_url"):
            value = data.get(key)
            if isinstance(value, str) and value:
                result.append(value)
                break
        return result
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                if item:
                    result.append(item)
            elif isinstance(item, dict):
                for key in ("url", "file", "path", "emojiUrl", "emoji_url"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        result.append(value)
                        break
    return result


def pick_sticker(urls: List[str], index: int) -> Optional[str]:
    """按索引取表情 URL（索引越界时回退到第 0 个）。"""
    if not urls:
        return None
    if index < 0 or index >= len(urls):
        index = 0
    return urls[index]


def sticker_base(data_dir: Path, index: int) -> Path:
    return data_dir / f"mj_sticker_{index}"


def find_cached_sticker(data_dir: Path, index: int) -> Optional[Path]:
    """查找已缓存的表情文件。"""
    for ext in (".png", ".jpg", ".gif", ".webp"):
        path = sticker_base(data_dir, index).with_suffix(ext)
        try:
            if path.exists() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


async def download_sticker(url: str, data_dir: Path, index: int) -> Optional[Path]:
    """下载表情到本地缓存；成功返回文件路径，失败返回 None。"""
    import aiohttp

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                ext = ".jpg"
                if "png" in ctype:
                    ext = ".png"
                elif "gif" in ctype:
                    ext = ".gif"
                elif "webp" in ctype:
                    ext = ".webp"
                payload = await resp.read()
                if not payload or len(payload) > 8 * 1024 * 1024:
                    return None
                target = sticker_base(data_dir, index).with_suffix(ext)
                temp = target.with_suffix(ext + ".tmp")
                temp.write_bytes(payload)
                temp.replace(target)
                return target
    except Exception:  # noqa: BLE001
        return None
