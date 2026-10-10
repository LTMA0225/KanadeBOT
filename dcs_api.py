"""音游窝 · 站点会话与线路辅助模块（v3.0.0）。

负责：
- 访问线路（HTTPS 域名 + 备用 IP 直连，证书仍按域名校验）；
- 站点登录与会话 Cookie 缓存（供计费表抓取、工作人员代离店、来店记录页读取使用）；
- 页面文本抓取（手动跟随同站重定向，Cookie 不外泄）。

说明：实时在店数据已改由统一 Bot API（dcs_botapi）提供，本模块不再解析在店页面。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

# 默认目标地址（只用 HTTPS）
DEFAULT_TARGET_URLS = ["https://dcstream.top/whosin"]

# 默认备用 IP：域名访问失败时直连，证书仍按域名校验
DEFAULT_FALLBACK_IPS = ["47.116.47.191"]

# 模拟浏览器的 User-Agent，附带插件标识，便于站点统计
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 DCStreamQueryBot/1.0"
)

# 玩家数量为 0 时发送的提示文案
EMPTY_PLAYER_MESSAGE = "店内无玩家，快来吧唧！"

# 登录页特征：当目标页面要求登录时，站点会重定向到 /login
LOGIN_PAGE_MARKERS = (
    "<h2>登录</h2>",
    '"c":["","login"]',
    '\\"c\\":[\\"\\",\\"login\\"]',
)

# 已登录会话缓存：站点源（scheme://host[:port]）-> Cookie 字符串（进程内缓存，失效时自动重新登录）
_LOGIN_COOKIE_CACHE: Dict[str, str] = {}

# 同站点重定向最多跟随的次数
_MAX_REDIRECTS = 5


def is_login_page(body: str) -> bool:
    """判断返回内容是否为登录页（页面需要登录时会被重定向）。"""
    return any(marker in body for marker in LOGIN_PAGE_MARKERS)


def _looks_like_login(final_url: str, body: str) -> bool:
    return "/login" in urlsplit(final_url).path or is_login_page(body)


def _origin_of(url: str) -> str:
    """提取 URL 的站点源（scheme://host[:port]）。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _Route:
    """一条访问线路：逻辑地址始终是域名形式；connect_ip 非空时直连该 IP。"""

    url: str
    connect_ip: str = ""

    @property
    def origin(self) -> str:
        return _origin_of(self.url)

    @property
    def secure(self) -> bool:
        return urlsplit(self.url).scheme.lower() == "https"

    def describe(self) -> str:
        return f"{self.url}（直连 {self.connect_ip}）" if self.connect_ip else self.url


def _build_routes(urls: List[str], fallback_ips: List[str]) -> List[_Route]:
    """按顺序展开访问线路：每个 HTTPS 域名地址后面追加其备用 IP 线路。"""
    routes: List[_Route] = []
    for url in urls:
        routes.append(_Route(url))
        parts = urlsplit(url)
        if parts.scheme.lower() == "https" and parts.hostname and not _is_ip(parts.hostname):
            routes.extend(_Route(url, ip) for ip in fallback_ips)
    unique: List[_Route] = []
    for route in routes:
        if route not in unique:
            unique.append(route)
    return unique


def _request(session, method: str, url: str, connect_ip: str = "", **kwargs):
    """发起请求（不自动跟随重定向）。

    connect_ip 非空时把 TCP 连接打到该 IP，但 Host 头、TLS SNI 与证书校验
    仍使用 URL 中的域名，因此证书必须对域名有效。
    """
    kwargs.setdefault("allow_redirects", False)
    if not connect_ip:
        return session.request(method, url, **kwargs)
    parts = urlsplit(url)
    ip_host = f"[{connect_ip}]" if ":" in connect_ip else connect_ip
    netloc = ip_host if parts.port is None else f"{ip_host}:{parts.port}"
    direct_url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    headers = dict(kwargs.pop("headers", None) or {})
    headers["Host"] = parts.netloc
    if parts.scheme.lower() == "https":
        kwargs["server_hostname"] = parts.hostname
    return session.request(method, direct_url, headers=headers, **kwargs)


async def site_login(
    route: _Route,
    username: str,
    password: str,
    timeout: int = 10,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """调用站点登录接口，返回 (Cookie, 错误类别, 错误详情)。

    站点接口约定（见音游窝网页端 github.com/MioAoi/dcs-web 的源码 app/api/login/route.ts）：
    - POST {origin}/api/login，JSON 参数 {"username": ..., "password": ...}
    - 成功：200 且响应体 {"success": true}，并通过 Set-Cookie 下发 session

    出于安全考虑，只允许通过 HTTPS 登录。
    """
    import aiohttp

    if not route.secure:
        return None, "login_required", f"{route.url} 不是 HTTPS 地址，出于安全考虑不发送账号密码"

    login_url = route.origin + "/api/login"
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with _request(
                session,
                "POST",
                login_url,
                route.connect_ip,
                json={"username": username, "password": password},
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status in (401, 403):
                    return None, "login_failed", f"登录接口拒绝（状态码 {resp.status}），请检查账号密码"
                if resp.status != 200:
                    return None, "http_status", f"{route.describe()} 登录接口返回状态码 {resp.status}"
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    return None, "http_status", f"{route.describe()} 登录接口返回的不是 JSON"
                if not isinstance(data, dict) or not data.get("success"):
                    return None, "login_failed", "登录失败：账号或密码错误（接口未返回 success）"

                morsel = resp.cookies.get("session")
                if morsel is not None:
                    return f"session={morsel.value}", None, None

                # 兼容通过响应头下发 Cookie 的情况
                match = re.search(r"session=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
                if match:
                    return f"session={match.group(1)}", None, None
                return None, "login_failed", "登录接口未返回会话 Cookie"
    except Exception as exc:  # noqa: BLE001 - 网络问题交给调用方切换线路
        return None, "network", f"{route.describe()} 登录请求失败：{exc!r}"


async def _resolve_cookie(
    route: _Route,
    cookie: str,
    username: str,
    password: str,
    timeout: int,
) -> Tuple[str, Optional[str], Optional[str]]:
    """按优先级获取请求用 Cookie：显式配置 > 缓存登录 > 现场登录。

    返回 (Cookie, 错误类别, 错误详情)；明文 HTTP 线路永远不使用凭据。
    """
    if not route.secure:
        return "", None, None
    if cookie.strip():
        return cookie.strip(), None, None
    if not (username and password):
        return "", None, None
    cached = _LOGIN_COOKIE_CACHE.get(route.origin)
    if cached:
        return cached, None, None
    fresh, kind, err = await site_login(route, username, password, timeout)
    if fresh:
        _LOGIN_COOKIE_CACHE[route.origin] = fresh
        return fresh, None, None
    return "", kind, err


async def _fetch_html(
    route: _Route,
    timeout: int,
    cookie: str,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """请求页面 HTML，返回 (页面内容, 最终地址, 错误类别, 错误详情)。

    只跟随同站点的重定向；跳到其他站点时立即停止，Cookie 不会外泄。
    """
    import aiohttp

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie.strip() and route.secure:
        headers["Cookie"] = cookie.strip()

    current = route.url
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            for _ in range(_MAX_REDIRECTS + 1):
                async with _request(session, "GET", current, route.connect_ip, headers=headers) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        target = urljoin(current, resp.headers.get("Location", ""))
                        if _origin_of(target) != route.origin:
                            return "", target, "redirect", (
                                f"{route.describe()} 跳转到其他站点 {_origin_of(target)}，已停止"
                            )
                        current = target
                        continue
                    if resp.status != 200:
                        return "", current, "http_status", f"{route.describe()} 返回状态码 {resp.status}"
                    return await resp.text(), current, None, None
            return "", current, "redirect", f"{route.describe()} 重定向次数过多"
    except Exception as exc:  # noqa: BLE001 - 汇总所有异常供调用方切换线路
        return "", current, "network", f"{route.describe()} 访问失败：{exc!r}"
