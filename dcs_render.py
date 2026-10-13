
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from astrbot.api import logger

THEME_TEMPLATES = {
    "cyberpunk-bw": "whosin.html",
}

DEFAULT_THEME = "cyberpunk-bw"

LOCAL_ASSET_ORIGIN = "https://dcs-local.invalid"
LOCAL_ASSET_BASE = LOCAL_ASSET_ORIGIN + "/assets"

ASSET_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

VIEWPORT = {"width": 1080, "height": 633}

BROWSER_CHOICES = ("auto", "msedge", "chrome", "chromium")

BROWSER_IDLE_SECONDS = 300

class ImageRenderCache:

    def __init__(self, max_entries: int = 16):
        self.max_entries = max_entries
        self._store: Dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, ttl: int) -> Optional[str]:
        if ttl <= 0:
            return None
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            created_at, path = item
            if time.time() - created_at > ttl or not Path(path).exists():
                self._store.pop(key, None)
                return None
            return path

    async def set(self, key: str, path: str) -> None:
        async with self._lock:
            self._store[key] = (time.time(), path)
            if len(self._store) > self.max_entries:
                oldest_key = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest_key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

class LocalAssets:

    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir.resolve()
        self._cache: Dict[str, Optional[Tuple[bytes, str]]] = {}

    def lookup(self, url: str) -> Optional[Tuple[bytes, str]]:
        prefix = LOCAL_ASSET_BASE + "/"
        if not url.startswith(prefix):
            return None
        rel = unquote(url[len(prefix):].split("?", 1)[0].split("#", 1)[0])
        if rel in self._cache:
            return self._cache[rel]
        item: Optional[Tuple[bytes, str]] = None
        try:
            path = (self.assets_dir / rel).resolve()
            content_type = ASSET_CONTENT_TYPES.get(path.suffix.lower())
            if content_type and self.assets_dir in path.parents and path.is_file():
                item = (path.read_bytes(), content_type)
        except OSError:
            item = None
        self._cache[rel] = item
        return item

    def exists(self, rel: str) -> bool:
        return self.lookup(f"{LOCAL_ASSET_BASE}/{rel}") is not None

class _BrowserWorker:

    def __init__(self, idle_seconds: int = BROWSER_IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread_lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._browser_key: Optional[Tuple[str, str]] = None
        self._browser_label = ""
        self._render_lock: Optional[asyncio.Lock] = None
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._active = 0

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: Dict[str, asyncio.AbstractEventLoop] = {}

            def runner() -> None:
                if sys.platform == "win32":
                    loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder["loop"] = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(target=runner, name="dcs-whosin-render", daemon=True)
            thread.start()
            if not ready.wait(10):
                raise RuntimeError("渲染线程启动超时")
            self._thread, self._loop = thread, holder["loop"]
            return self._loop

    async def screenshot(
        self,
        html: str,
        assets: LocalAssets,
        browser: str,
        browser_path: str,
        timeout: float,
    ) -> bytes:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._render(html, assets, browser, browser_path, timeout), loop
        )
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    async def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            await asyncio.wait_for(asyncio.wrap_future(future), 15)
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 关闭本地浏览器时出错：{exc!r}")
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join, 5)
        self._loop = None
        self._thread = None

    @staticmethod
    def _launch_candidates(choice: str, browser_path: str) -> List[Tuple[str, Dict[str, Any]]]:
        if browser_path:
            return [(f"自定义浏览器 {browser_path}", {"executable_path": browser_path})]
        edge = ("Microsoft Edge", {"channel": "msedge"})
        chrome = ("Google Chrome", {"channel": "chrome"})
        bundled = ("Playwright Chromium", {})
        return {
            "msedge": [edge],
            "chrome": [chrome],
            "chromium": [bundled],
        }.get(choice, [edge, chrome, bundled])

    async def _get_context(self, choice: str, browser_path: str):
        key = (choice, browser_path)
        if (
            self._context is not None
            and self._browser is not None
            and self._browser.is_connected()
            and self._browser_key == key
        ):
            return self._context
        await self._close_browser()

        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        errors = []
        for label, options in self._launch_candidates(choice, browser_path):
            try:
                self._browser = await self._playwright.chromium.launch(headless=True, **options)
            except Exception as exc:
                errors.append(f"{label}：{str(exc).strip().splitlines()[0]}")
                continue
            self._browser_key = key
            self._browser_label = label
            logger.info(f"[dcs_whosin] 本地渲染浏览器已启动：{label}")
            break
        else:
            raise RuntimeError(
                "未找到可用的浏览器内核（"
                + "；".join(errors)
                + "）。Windows 自带 Edge 即可；也可在插件配置中指定浏览器路径，"
                "或执行 python -m playwright install chromium"
            )

        self._context = await self._browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            java_script_enabled=False,
            accept_downloads=False,
            service_workers="block",
        )
        return self._context

    async def _render(
        self,
        html: str,
        assets: LocalAssets,
        choice: str,
        browser_path: str,
        timeout: float,
    ) -> bytes:
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        timeout_ms = max(1000, int(timeout * 1000))
        self._active += 1
        try:
            async with self._render_lock:
                for attempt in (1, 2):
                    context = await self._get_context(choice, browser_path)
                    try:
                        return await self._screenshot_once(context, html, assets, timeout_ms)
                    except Exception:
                        if attempt == 2 or (self._browser is not None and self._browser.is_connected()):
                            raise
                        logger.warning("[dcs_whosin] 本地浏览器已断开，正在重启后重试")
                        await self._close_browser()
        finally:
            self._active -= 1
            self._schedule_idle_close()
        raise RuntimeError("unreachable")

    @staticmethod
    async def _screenshot_once(context, html: str, assets: LocalAssets, timeout_ms: int) -> bytes:
        async def handle_route(route) -> None:
            item = assets.lookup(route.request.url)
            if item is None:
                await route.abort("blockedbyclient")
                return
            body, content_type = item
            await route.fulfill(
                status=200,
                body=body,
                content_type=content_type,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=86400"},
            )

        page = await context.new_page()
        try:
            await page.route("**/*", handle_route)
            await page.set_content(html, wait_until="load", timeout=timeout_ms)
            return await page.screenshot(
                type="png", full_page=True, animations="disabled", timeout=timeout_ms
            )
        finally:
            await page.close()

    def _schedule_idle_close(self) -> None:
        loop = self._loop
        if loop is None:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()

        def on_idle() -> None:
            if self._active > 0:
                self._schedule_idle_close()
                return
            loop.create_task(self._shutdown(reason="空闲"))

        self._idle_handle = loop.call_later(self.idle_seconds, on_idle)

    async def _close_browser(self, reason: str = "") -> None:
        context, browser = self._context, self._browser
        self._context = None
        self._browser = None
        self._browser_key = None
        for closer in (context, browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:
                pass
        if browser is not None and reason:
            logger.info(f"[dcs_whosin] 本地渲染浏览器已关闭（{reason}）")

    async def _shutdown(self, reason: str = "") -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        async with self._render_lock:
            if reason and self._active > 0:
                return
            await self._close_browser(reason=reason)
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

class WhosinRenderer:

    def __init__(self, plugin_dir: Path, cache_dir: Path):
        self.plugin_dir = plugin_dir
        self.template_dir = plugin_dir / "templates"
        self.assets = LocalAssets(plugin_dir / "assets")
        self.cache_dir = cache_dir
        self.cache = ImageRenderCache()
        self._worker = _BrowserWorker()
        self._env = None
        self._missing_dependency_logged = False

    def _get_env(self):
        if self._env is None:
            from jinja2 import Environment, FileSystemLoader

            self._env = Environment(
                loader=FileSystemLoader(str(self.template_dir)),
                autoescape=True,
                auto_reload=True,
            )
        return self._env

    def render_html(self, payload: Dict[str, Any], theme: str = DEFAULT_THEME) -> str:
        template_name = THEME_TEMPLATES.get(theme, THEME_TEMPLATES[DEFAULT_THEME])
        context = dict(payload)
        context["asset_base"] = LOCAL_ASSET_BASE
        context["logo_available"] = self.assets.exists("logo.svg")
        return self._get_env().get_template(template_name).render(**context)

    @staticmethod
    def build_cache_key(payload: Dict[str, Any], theme: str = DEFAULT_THEME) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        minute = time.strftime("%Y%m%d%H%M")
        return hashlib.md5(f"{raw}|{theme}|{minute}".encode("utf-8")).hexdigest()

    async def render(
        self,
        payload: Dict[str, Any],
        theme: str = DEFAULT_THEME,
        ttl: int = 30,
        browser: str = "auto",
        browser_path: str = "",
        timeout: float = 20.0,
    ) -> Optional[str]:
        cache_key = self.build_cache_key(payload, theme)
        cached = await self.cache.get(cache_key, ttl)
        if cached:
            return cached

        try:
            html = self.render_html(payload, theme)
        except ImportError:
            self._log_missing_dependency("jinja2")
            return None
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 模板渲染失败：{exc!r}")
            return None

        choice = browser if browser in BROWSER_CHOICES else "auto"
        timeout = max(5.0, float(timeout))
        try:
            png = await self._worker.screenshot(html, self.assets, choice, browser_path.strip(), timeout)
        except ImportError:
            self._log_missing_dependency("playwright")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"[dcs_whosin] 本地截图超时（>{timeout:.0f} 秒），本次改发文字版")
            return None
        except Exception as exc:
            logger.warning(f"[dcs_whosin] 本地截图失败：{exc}")
            return None

        try:
            path = self._save_png(png, cache_key)
        except OSError as exc:
            logger.warning(f"[dcs_whosin] 图片写入失败：{exc!r}")
            return None
        await self.cache.set(cache_key, path)
        return path

    def _log_missing_dependency(self, name: str) -> None:
        if not self._missing_dependency_logged:
            self._missing_dependency_logged = True
            logger.warning(
                f"[dcs_whosin] 缺少依赖 {name}，图片功能暂不可用（只发文字）。"
                "请在 AstrBot 插件页点击本插件的「安装依赖」后重载插件。"
            )

    def _save_png(self, png: bytes, cache_key: str) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"whosin_{cache_key}.png"
        temp = target.with_suffix(".tmp")
        temp.write_bytes(png)
        temp.replace(target)
        self._prune_cache_dir(self.cache_dir)
        return str(target)

    @staticmethod
    def _prune_cache_dir(directory: Path, max_age_seconds: int = 900) -> None:
        try:
            now = time.time()
            for item in directory.iterdir():
                if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                    item.unlink(missing_ok=True)
        except Exception:
            pass

    async def close(self) -> None:
        await self._worker.close()
        await self.cache.clear()
