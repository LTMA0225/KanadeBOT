
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_TARGET_URLS = ["https://dcstream.top/whosin"]

DEFAULT_FALLBACK_IPS = ["47.116.47.191"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 DCStreamQueryBot/1.0"
)

EMPTY_PLAYER_MESSAGE = "店内无玩家，快来吧唧！"

LOGIN_PAGE_MARKERS = (
    "<h2>登录</h2>",
    '"c":["","login"]',
    '\\"c\\":[\\"\\",\\"login\\"]',
)

_LOGIN_COOKIE_CACHE: Dict[str, str] = {}

_MAX_REDIRECTS = 5

def is_login_page(body: str) -> bool:
    return any(marker in body for marker in LOGIN_PAGE_MARKERS)

def _looks_like_login(final_url: str, body: str) -> bool:
    return "/login" in urlsplit(final_url).path or is_login_page(body)

def _origin_of(url: str) -> str:
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
                except Exception:
                    return None, "http_status", f"{route.describe()} 登录接口返回的不是 JSON"
                if not isinstance(data, dict) or not data.get("success"):
                    return None, "login_failed", "登录失败：账号或密码错误（接口未返回 success）"

                morsel = resp.cookies.get("session")
                if morsel is not None:
                    return f"session={morsel.value}", None, None

                match = re.search(r"session=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
                if match:
                    return f"session={match.group(1)}", None, None
                return None, "login_failed", "登录接口未返回会话 Cookie"
    except Exception as exc:
        return None, "network", f"{route.describe()} 登录请求失败：{exc!r}"

async def _resolve_cookie(
    route: _Route,
    cookie: str,
    username: str,
    password: str,
    timeout: int,
) -> Tuple[str, Optional[str], Optional[str]]:
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
    except Exception as exc:
        return "", current, "network", f"{route.describe()} 访问失败：{exc!r}"
