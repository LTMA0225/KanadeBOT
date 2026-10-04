"""HTML 模板渲染与缓存模块。

图片渲染使用 AstrBot 内置的 ``html_render`` 接口：
- 该接口底层使用 Playwright 无头浏览器在本地渲染 HTML（非后端截图服务）；
- 模板使用 Jinja2 语法，渲染数据通过第二个参数传入；
- 渲染结果（图片地址）可直接交给 OneBot 发送。

同时提供轻量内存缓存，避免短时间内重复查询时反复渲染图片。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from astrbot.api import logger

# 主题名 -> 模板文件名的映射，便于后续扩展多套模板
THEME_TEMPLATES = {
    "cyberpunk-bw": "whosin.html",
}

DEFAULT_THEME = "cyberpunk-bw"


class ImageRenderCache:
    """简单的内存图片缓存（带 TTL 与容量上限）。"""

    def __init__(self, max_entries: int = 16):
        self.max_entries = max_entries
        self._store: Dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, ttl: int) -> Optional[str]:
        """读取缓存；ttl <= 0 时视为不缓存。"""
        if ttl <= 0:
            return None
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            created_at, url = item
            if time.time() - created_at > ttl:
                self._store.pop(key, None)
                return None
            return url

    async def set(self, key: str, url: str) -> None:
        """写入缓存并驱逐最旧的条目。"""
        async with self._lock:
            self._store[key] = (time.time(), url)
            if len(self._store) > self.max_entries:
                oldest_key = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest_key, None)

    async def clear(self) -> None:
        """清空缓存。"""
        async with self._lock:
            self._store.clear()


class WhosinRenderer:
    """在店一览图片渲染器。"""

    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.template_dir = plugin_dir / "templates"
        self.logo_path = plugin_dir / "assets" / "logo.svg"
        self.cache = ImageRenderCache()
        self._templates: Dict[str, str] = {}
        self._logo_uri: Optional[str] = None

    # ------------------------------------------------------------------
    # 资源加载
    # ------------------------------------------------------------------
    def load_template(self, theme: str = DEFAULT_THEME) -> str:
        """按主题加载 HTML 模板（带内存缓存）。"""
        template_name = THEME_TEMPLATES.get(theme, THEME_TEMPLATES[DEFAULT_THEME])
        if template_name not in self._templates:
            template_path = self.template_dir / template_name
            self._templates[template_name] = template_path.read_text(encoding="utf-8")
        return self._templates[template_name]

    def load_logo_uri(self) -> str:
        """将 Logo 转为 base64 Data URI，便于无头浏览器直接内联引用。"""
        if self._logo_uri is None:
            if self.logo_path.exists():
                data = base64.b64encode(self.logo_path.read_bytes()).decode("ascii")
                self._logo_uri = f"data:image/svg+xml;base64,{data}"
            else:
                logger.warning(f"[dcs_whosin] 未找到 Logo 素材：{self.logo_path}")
                self._logo_uri = ""
        return self._logo_uri

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    @staticmethod
    def build_cache_key(payload: Dict[str, Any]) -> str:
        """以（渲染数据 + 当前分钟）生成缓存键，保证图片时间戳每分钟刷新。"""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        minute = time.strftime("%Y%m%d%H%M")
        return hashlib.md5(f"{raw}|{minute}".encode("utf-8")).hexdigest()

    async def render(
        self,
        star: Any,
        payload: Dict[str, Any],
        theme: str = DEFAULT_THEME,
        ttl: int = 30,
    ) -> Optional[str]:
        """渲染图片，返回可发送的图片地址；失败时返回 None。

        :param star: 插件实例（Star），用于调用 self.html_render
        :param payload: 传给 Jinja2 模板的渲染数据
        :param theme: 图片模板主题
        :param ttl: 图片缓存时间（秒），0 表示不缓存
        """
        cache_key = self.build_cache_key(payload)
        cached = await self.cache.get(cache_key, ttl)
        if cached:
            return cached

        try:
            template = self.load_template(theme)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 模板加载失败：{exc}")
            return None
        context = dict(payload)
        context["logo_uri"] = self.load_logo_uri()

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                image_url = await star.html_render(
                    template,
                    context,
                    options={
                        "type": "png",
                        "full_page": True,
                        "animations": "disabled",
                        "viewport_width": 1080,
                        "viewport_height": 633,
                    },
                )
                image_ref = await self._localize_image(image_url, cache_key)
                if image_ref:
                    await self.cache.set(cache_key, image_ref)
                    return image_ref
                last_error = RuntimeError("图片本地化失败")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt < 3:
                await asyncio.sleep(2.5)
        logger.warning(f"[dcs_whosin] 图片渲染失败（已重试 3 次）：{last_error}")
        return None

    async def _localize_image(self, image_url: str, cache_key: str) -> Optional[str]:
        """将远程渲染结果下载到本地，避免发送阶段依赖外部图片服务。"""
        if not image_url:
            return None
        if not image_url.startswith(("http://", "https://")):
            return image_url

        target_dir = self.plugin_dir / "image_cache"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"whosin_{cache_key}.png"

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        image_url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")
                        data = await response.read()
                if not data:
                    raise RuntimeError("空响应")
                target_path.write_bytes(data)
                self._prune_cache_dir(target_dir)
                return target_path.as_uri()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(1.5 * attempt)
        logger.warning(f"[dcs_whosin] 图片本地化失败（已重试 3 次）：{last_error}")
        return None

    @staticmethod
    def _prune_cache_dir(directory: Path, max_age_seconds: int = 900) -> None:
        """清理过期的本地图片缓存文件。"""
        try:
            now = time.time()
            for item in directory.iterdir():
                if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                    item.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
