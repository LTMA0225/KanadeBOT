"""在店一览图片：本地渲染与缓存模块。

渲染全程在本机完成，不调用任何第三方 / 社区渲染服务：
1. Jinja2 渲染 HTML 模板（开启自动转义，昵称等数据一律按纯文本处理）；
2. Playwright 驱动本机浏览器内核无头截图
   （auto 模式依次尝试 Edge → Chrome → Playwright 自带 Chromium）；
3. 页面禁用 JavaScript，并拦截一切网络请求，只放行插件 assets/ 目录下的本地素材
   （通过虚拟地址 https://dcs-local.invalid/assets/ 提供），不会加载任何外部资源。

Playwright 运行在独立线程的独立事件循环中（Windows 下为 Proactor 循环），
与 AstrBot 主事件循环隔离；浏览器空闲一段时间后自动关闭以释放内存。
"""

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

# 主题名 -> 模板文件名的映射，便于后续扩展多套模板
THEME_TEMPLATES = {
    "cyberpunk-bw": "whosin.html",
}

DEFAULT_THEME = "cyberpunk-bw"

# 页面引用本地素材用的虚拟站点：.invalid 是保留顶级域名，不会被真实解析，
# 对它的请求全部由渲染器拦截并用本地文件应答。
LOCAL_ASSET_ORIGIN = "https://dcs-local.invalid"
LOCAL_ASSET_BASE = LOCAL_ASSET_ORIGIN + "/assets"

# 允许经虚拟站点提供的素材类型
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

# 截图视口：宽 1080，高度随内容自动延展（full_page）
VIEWPORT = {"width": 1080, "height": 633}

# 浏览器内核选择
BROWSER_CHOICES = ("auto", "msedge", "chrome", "chromium")

# 浏览器空闲多久后自动关闭（秒）
BROWSER_IDLE_SECONDS = 300


class ImageRenderCache:
    """简单的内存图片缓存（带 TTL 与容量上限），值为本地图片路径。"""

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
            created_at, path = item
            if time.time() - created_at > ttl or not Path(path).exists():
                self._store.pop(key, None)
                return None
            return path

    async def set(self, key: str, path: str) -> None:
        """写入缓存并驱逐最旧的条目。"""
        async with self._lock:
            self._store[key] = (time.time(), path)
            if len(self._store) > self.max_entries:
                oldest_key = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(oldest_key, None)

    async def clear(self) -> None:
        """清空缓存。"""
        async with self._lock:
            self._store.clear()


class LocalAssets:
    """插件 assets/ 目录下的素材，按需读入内存，经虚拟地址提供给页面。"""

    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir.resolve()
        self._cache: Dict[str, Optional[Tuple[bytes, str]]] = {}

    def lookup(self, url: str) -> Optional[Tuple[bytes, str]]:
        """虚拟地址 -> (文件内容, Content-Type)；不是本地素材时返回 None。"""
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
            # 只提供 assets 目录内的已知类型文件，防止 ../ 越界读取
            if content_type and self.assets_dir in path.parents and path.is_file():
                item = (path.read_bytes(), content_type)
        except OSError:
            item = None
        self._cache[rel] = item
        return item

    def exists(self, rel: str) -> bool:
        return self.lookup(f"{LOCAL_ASSET_BASE}/{rel}") is not None


class _BrowserWorker:
    """在独立线程 + 独立事件循环中运行 Playwright，与 AstrBot 主循环隔离。

    除 screenshot() / close() 外，其余方法只在工作线程的事件循环中执行。
    """

    def __init__(self, idle_seconds: int = BROWSER_IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread_lock = threading.Lock()
        # 以下属性只在工作线程中访问
        self._playwright = None
        self._browser = None
        self._context = None
        self._browser_key: Optional[Tuple[str, str]] = None
        self._browser_label = ""
        self._render_lock: Optional[asyncio.Lock] = None
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._active = 0

    # ---------------- 主线程侧 ----------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: Dict[str, asyncio.AbstractEventLoop] = {}

            def runner() -> None:
                # Windows 下 Playwright 需要支持子进程的 Proactor 循环，
                # 这里显式创建，不受 AstrBot 全局事件循环策略影响。
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
        """在工作线程中截图；超时会取消工作线程中的任务。"""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._render(html, assets, browser, browser_path, timeout), loop
        )
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    async def close(self) -> None:
        """关闭浏览器与 Playwright，并停止工作线程。"""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            await asyncio.wait_for(asyncio.wrap_future(future), 15)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 关闭本地浏览器时出错：{exc!r}")
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join, 5)
        self._loop = None
        self._thread = None

    # ---------------- 工作线程侧 ----------------
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
            except Exception as exc:  # noqa: BLE001
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

        # 页面禁用 JavaScript；所有请求经 route 审核，只有本地素材会被应答
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
                        # 浏览器意外退出时重启一次；其他错误直接抛出
                        if attempt == 2 or (self._browser is not None and self._browser.is_connected()):
                            raise
                        logger.warning("[dcs_whosin] 本地浏览器已断开，正在重启后重试")
                        await self._close_browser()
        finally:
            self._active -= 1
            self._schedule_idle_close()
        raise RuntimeError("unreachable")  # pragma: no cover

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
            except Exception:  # noqa: BLE001
                pass
        if browser is not None and reason:
            logger.info(f"[dcs_whosin] 本地渲染浏览器已关闭（{reason}）")

    async def _shutdown(self, reason: str = "") -> None:
        """关闭浏览器并停止 Playwright 驱动进程（空闲超时或插件停用时调用）。"""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        # 与渲染互斥，避免在截图进行中关闭浏览器
        async with self._render_lock:
            if reason and self._active > 0:
                return
            await self._close_browser(reason=reason)
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None


class WhosinRenderer:
    """在店一览图片渲染器（本地 Jinja2 + Playwright）。"""

    def __init__(self, plugin_dir: Path, cache_dir: Path):
        self.plugin_dir = plugin_dir
        self.template_dir = plugin_dir / "templates"
        self.assets = LocalAssets(plugin_dir / "assets")
        self.cache_dir = cache_dir
        self.cache = ImageRenderCache()
        self._worker = _BrowserWorker()
        self._env = None
        self._missing_dependency_logged = False

    # ------------------------------------------------------------------
    # HTML 生成
    # ------------------------------------------------------------------
    def _get_env(self):
        if self._env is None:
            from jinja2 import Environment, FileSystemLoader

            # autoescape=True：模板中的所有变量都会被 HTML 转义，昵称无法注入标签
            self._env = Environment(
                loader=FileSystemLoader(str(self.template_dir)),
                autoescape=True,
                auto_reload=True,
            )
        return self._env

    def render_html(self, payload: Dict[str, Any], theme: str = DEFAULT_THEME) -> str:
        """按主题把渲染数据填入模板，返回完整 HTML。"""
        template_name = THEME_TEMPLATES.get(theme, THEME_TEMPLATES[DEFAULT_THEME])
        context = dict(payload)
        context["asset_base"] = LOCAL_ASSET_BASE
        context["logo_available"] = self.assets.exists("logo.svg")
        return self._get_env().get_template(template_name).render(**context)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    @staticmethod
    def build_cache_key(payload: Dict[str, Any], theme: str = DEFAULT_THEME) -> str:
        """以（渲染数据 + 主题 + 当前分钟）生成缓存键，保证图片时间戳每分钟刷新。"""
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
        """渲染图片，返回本地 PNG 路径；失败或超时时返回 None（调用方降级为文字）。

        :param payload: 传给 Jinja2 模板的渲染数据
        :param theme: 图片模板主题
        :param ttl: 图片缓存时间（秒），0 表示不缓存
        :param browser: 浏览器内核：auto / msedge / chrome / chromium
        :param browser_path: 自定义浏览器可执行文件路径（优先于 browser）
        :param timeout: 单次渲染总超时（秒，含首次启动浏览器）
        """
        cache_key = self.build_cache_key(payload, theme)
        cached = await self.cache.get(cache_key, ttl)
        if cached:
            return cached

        try:
            html = self.render_html(payload, theme)
        except ImportError:
            self._log_missing_dependency("jinja2")
            return None
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
        """清理过期的本地图片缓存文件。"""
        try:
            now = time.time()
            for item in directory.iterdir():
                if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                    item.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        """关闭本地浏览器并清空缓存（插件停用时调用）。"""
        await self._worker.close()
        await self.cache.clear()
