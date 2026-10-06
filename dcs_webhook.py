"""音游窝 · Webhook 接收服务（v2.3.0）。

插件内置一个轻量 HTTP 服务（aiohttp），接收站点在"进店/离店"时推送的实时事件：

    POST /webhook   接收事件（建议配置 webhook_secret，校验请求头 X-DCS-Token）
    GET  /health    健康检查

音游窝网页端（github.com/MioAoi/dcs-web）在 .env 配置示例：
    DCS_WEBHOOK_URLS=http://<本机地址>:8765/webhook
    DCS_WEBHOOK_TOKEN=<与插件 webhook_secret 相同的令牌>

说明：
- 站点服务器需要能访问到本机端口（局域网 / 端口映射 / frp / SSH 反向隧道等）。
- 即使 Webhook 不可达，插件也会自动回退到 /api/visits/recent 记录流
  或在店快照模式，进离店播报不会因此漏报。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from astrbot.api import logger
from aiohttp import web


class WebhookReceiver:
    """基于 aiohttp 的 Webhook 接收器（随插件生命周期启停）。"""

    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._host = host or "0.0.0.0"
        self._port = int(port)
        self._secret = secret or ""
        self._handler = handler
        self._runner: Optional[web.AppRunner] = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> bool:
        if self._started:
            return True
        app = web.Application()
        app.router.add_get("/health", self._on_health)
        app.router.add_post("/webhook", self._on_webhook)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._host, self._port)
            await site.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[dcs_whosin] Webhook 服务启动失败（{self._host}:{self._port}）：{exc!r}"
            )
            try:
                await runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            return False
        self._runner = runner
        self._started = True
        logger.info(
            f"[dcs_whosin] Webhook 接收服务已启动：http://{self._host}:{self._port}/webhook"
        )
        return True

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._started = False
        if runner is not None:
            try:
                await runner.cleanup()
                logger.info("[dcs_whosin] Webhook 接收服务已停止。")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    async def _on_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "dcs-whosin-webhook"})

    async def _on_webhook(self, request: web.Request) -> web.Response:
        if self._secret:
            token = request.headers.get("X-DCS-Token", "")
            if token != self._secret:
                return web.json_response({"success": False, "error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"success": False, "error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"success": False, "error": "invalid payload"}, status=400)
        # 先立即应答，后台异步处理，避免拖慢站点请求
        asyncio.create_task(self._safe_handle(payload))
        return web.json_response({"success": True})

    async def _safe_handle(self, payload: dict) -> None:
        try:
            await self._handler(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dcs_whosin] 处理 Webhook 事件失败：{exc!r}")
