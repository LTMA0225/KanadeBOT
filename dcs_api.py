"""音游窝在店数据抓取模块。

负责从音游窝官网抓取"实时在店人员"页面，并解析出昵称、角色、
入店时间与游玩时长（分钟）。

网络与认证（安全设计）：
- 账号密码与会话 Cookie 只会通过 HTTPS 发送；明文 HTTP 地址只做匿名访问，绝不携带凭据。
- 域名访问失败时，可直连备用 IP（fallback_ips）。直连时 SNI、Host 与证书校验
  仍使用原域名，因此不会降级为明文，也不会信任与域名不符的证书。
- 重定向一律手动跟随，且只跟随同站点（同协议 + 域名 + 端口）的跳转，
  Cookie 不会被带到其他站点。

认证方式（/whosin 需要登录）：
  1. 直接配置 Cookie（cookie 配置项）；
  2. 配置账号密码（login_username / login_password），
     自动调用站点登录接口获取会话 Cookie，并在失效时自动重新登录。

解析约定（与站点前端组件 UserOneline / PresentUser 对应）：
- 昵称元素的 class 中含有独立的 ``nickname`` 词元，角色体现为附加词元：
  - ``nickname staff``    -> 士大夫（工作人员）
  - ``nickname admin``    -> 管理员
  - ``nickname sponsor``  -> 赞助者（本插件计入玩家）
  - ``nickname``          -> 普通玩家
- 入店时间位于昵称之后的"自 <span>时间</span>"或"从 <span>时间</span>"元素中，
  时间文案可能是：HH:MM / 昨天 HH:MM / 前天 HH:MM / 周X HH:MM /
  M月D日 HH:MM / YYYY.MM.DD HH:MM（按站点 FLT 时区，UTC+8，4 点换日）。
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
import time as _time
from dataclasses import dataclass, field
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

# 匹配 class 中含 nickname 的元素：第一个分组为 class，第二个分组为昵称文本。
# 参考站点结构：<span class="nickname staff">昵称</span>
# 注意：\b 也会匹配 nickname-label 之类的类名，解析时会再按独立词元校验一次。
NICKNAME_PATTERN = re.compile(
    r"""<[^>]*class=["']([^"']*\bnickname\b[^"']*)["'][^>]*>([^<]*)<""",
    re.IGNORECASE,
)

# 匹配昵称之后的入店时间："自/从 <span ...>时间</span>"
ENTRY_TIME_PATTERN = re.compile(r"[自从]\s*<[^>]*>([^<]+)<")
# 兜底：单独的 "自/从 HH:MM" 明文
ENTRY_TIME_FALLBACK = re.compile(r"[自从]\s*(\d{1,2}:\d{2})")

# 玩家数量为 0 时发送的提示文案
EMPTY_PLAYER_MESSAGE = "店内无玩家，快来吧唧！"

# 登录页特征：当目标页面要求登录时，站点会重定向到 /login
LOGIN_PAGE_MARKERS = (
    "<h2>登录</h2>",
    '"c":["","login"]',
    '\\"c\\":[\\"\\",\\"login\\"]',
)

# 需要登录但未配置认证时给出的引导文案（写入日志 / 状态推送）
LOGIN_REQUIRED_HINT = (
    "目标页面要求登录。请在插件配置中填写站点账号密码（login_username / login_password），"
    "或填写浏览器中的 Cookie（cookie 项）。"
)

# 错误类别 -> 群内可见的提示（不含地址、IP、异常详情等内部信息）
PUBLIC_ERROR_TEXT = {
    "network": "站点暂时无法访问，请稍后再试。",
    "http_status": "站点返回异常，请稍后再试。",
    "redirect": "站点返回了异常跳转，请稍后再试。",
    "login_required": "站点需要登录，请联系维护者配置账号。",
    "login_failed": "站点登录失败，请联系维护者检查账号配置。",
    "config": "插件配置有误，请联系维护者。",
}

# 已登录会话缓存：站点源（scheme://host[:port]）-> Cookie 字符串（进程内缓存，失效时自动重新登录）
_LOGIN_COOKIE_CACHE: Dict[str, str] = {}

# 同站点重定向最多跟随的次数
_MAX_REDIRECTS = 5

# 站点时区与换日规则（FLT：UTC+8，凌晨 4 点换日）
_FLT_OFFSET_MS = 8 * 3600 * 1000
_FLT_BOUNDARY_MS = 4 * 3600 * 1000
_DAY_MS = 86400000

_ENTRY_TODAY = re.compile(r"^(\d{1,2}):(\d{2})$")
_ENTRY_YESTERDAY = re.compile(r"^昨天\s*(\d{1,2}):(\d{2})$")
_ENTRY_DAYBEFORE = re.compile(r"^前天\s*(\d{1,2}):(\d{2})$")
_ENTRY_WEEKDAY = re.compile(r"^周([日一二三四五六])\s*(\d{1,2}):(\d{2})$")
_ENTRY_MONTHDAY = re.compile(r"^(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}):(\d{2})$")
_ENTRY_FULLDATE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})\s*(\d{1,2}):(\d{2})$")

_WEEKDAY_INDEX = {"日": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


@dataclass
class VenueUser:
    """一位在店用户。"""

    name: str
    role: str  # player / staff / admin / sponsor
    entered_text: str = ""  # 页面显示的入店时间文案（原样）
    minutes: Optional[int] = None  # 由入店时间计算出的游玩时长（分钟）


@dataclass
class FetchResult:
    """抓取结果封装。

    error 为详细原因（含地址与异常信息，只写日志或推送给维护者）；
    error_kind 为错误类别，群内只展示 PUBLIC_ERROR_TEXT 中对应的通用文案。
    """

    users: List[VenueUser] = field(default_factory=list)
    source_url: str = ""
    raw_html: str = ""  # 原始页面（供进/离店播报解析内嵌数据；不写日志）
    error: Optional[str] = None
    error_kind: Optional[str] = None


@dataclass
class VenueGroups:
    """按角色分组后的用户列表（元素为包含 name/entered/minutes 的字典）。"""

    players: List[dict] = field(default_factory=list)
    staffs: List[dict] = field(default_factory=list)
    admins: List[dict] = field(default_factory=list)

    @property
    def staff_line(self) -> List[dict]:
        """管理 / STAFF 合并成一行时的顺序：管理员在前，士大夫在后。"""
        return self.admins + self.staffs


def public_error_text(kind: Optional[str]) -> str:
    """错误类别 -> 群内可见的通用提示。"""
    return PUBLIC_ERROR_TEXT.get(kind or "", "查询失败，请稍后再试。")


def _business_midnight(ms: int) -> int:
    """返回 ms 所处"业务日"对应的 UTC 零点毫秒。

    业务日规则（站点 FLT）：UTC+8 凌晨 4 点换日，
    因此业务日 = UTC 日期(ms + 4h)，其零点是 UTC 当日 00:00。
    """
    return ((ms + _FLT_BOUNDARY_MS) // _DAY_MS) * _DAY_MS


def _midnight_for_date(year: int, month: int, day: int) -> int:
    """给定日历日期，返回其 UTC 零点毫秒；日期非法时抛出 ValueError。"""
    from datetime import datetime, timezone

    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _entry_from_clock(midnight: int, hh: int, mm: int) -> int:
    """业务日零点 + 站点显示时钟 → 毫秒时间戳。

    站点 toFLT 显示：hour = (T + 8h - 业务日零点) / 1h，
    即显示时钟范围 04:00 ~ 27:59（UTC+8 挂钟：24 点后表示次日凌晨）。
    因此 T = 业务日零点 + (hh - 8) * 1h + mm。
    """
    return midnight + (hh - 8) * 3600000 + mm * 60000


def _business_clock_hour(hh: int) -> int:
    """相对业务日的时钟统一换算到 04~27 点。

    站点按 4 点换日，凌晨时段通常显示为 24~27 点；若显示的是 00~03 点，
    它同样属于该业务日的凌晨，换算为 24~27 点（对 24 点制写法无影响）。
    """
    return hh + 24 if hh < 4 else hh


def _valid_clock(hh: int, mm: int) -> bool:
    return 0 <= hh <= 27 and 0 <= mm <= 59


def parse_entry_minutes(entered_text: str, now_ms: Optional[int] = None) -> Optional[int]:
    """将入店时间文案解析为"已游玩分钟数"；无法解析时返回 None。"""
    if not entered_text:
        return None
    text = (
        entered_text.replace("\u2007", " ")
        .replace("\u3000", " ")
        .replace(" ", "")
        .strip()
    )
    now = int(now_ms if now_ms is not None else _time.time() * 1000)

    def _minutes_between(entry_ms: int) -> int:
        return max(0, (now - entry_ms) // 60000)

    try:
        # 今天：HH:MM（业务日内，显示范围 04:00 - 27:59）
        m = _ENTRY_TODAY.match(text)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            if not _valid_clock(hh, mm):
                return None
            entry = _entry_from_clock(_business_midnight(now), _business_clock_hour(hh), mm)
            if entry > now + 60000:  # 容错：跨业务日边界
                entry -= _DAY_MS
            return _minutes_between(entry)

        # 昨天 / 前天
        for pattern, back in ((_ENTRY_YESTERDAY, 1), (_ENTRY_DAYBEFORE, 2)):
            m = pattern.match(text)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2))
                if not _valid_clock(hh, mm):
                    return None
                entry = _entry_from_clock(
                    _business_midnight(now) - back * _DAY_MS, _business_clock_hour(hh), mm
                )
                return _minutes_between(entry)

        # 周X HH:MM（最近一次该星期几对应的业务日）
        m = _ENTRY_WEEKDAY.match(text)
        if m:
            target = _WEEKDAY_INDEX[m.group(1)]
            hh, mm = int(m.group(2)), int(m.group(3))
            if not _valid_clock(hh, mm):
                return None
            now_midnight = _business_midnight(now)
            now_dnum = now_midnight // _DAY_MS
            now_weekday = (now_dnum + 4) % 7  # 1970-01-01 为周四；0 = 周日
            days_back = (now_weekday - target) % 7
            entry = _entry_from_clock(
                now_midnight - days_back * _DAY_MS, _business_clock_hour(hh), mm
            )
            if entry > now + 60000:
                entry -= 7 * _DAY_MS
            return _minutes_between(entry)

        # M月D日 HH:MM（默认当年；若显著晚于当前则视为去年）
        # 带日期的写法里"日期 + 时钟"本身无歧义，不做 24 点换算
        m = _ENTRY_MONTHDAY.match(text)
        if m:
            month, day, hh, mm = (int(v) for v in m.groups())
            if not _valid_clock(hh, mm):
                return None
            year = _flt_calendar_year(now)
            entry = _entry_from_clock(_midnight_for_date(year, month, day), hh, mm)
            if entry > now + 7 * _DAY_MS:
                entry = _entry_from_clock(_midnight_for_date(year - 1, month, day), hh, mm)
            return _minutes_between(entry)

        # YYYY.MM.DD HH:MM
        m = _ENTRY_FULLDATE.match(text)
        if m:
            year, month, day, hh, mm = (int(v) for v in m.groups())
            if not _valid_clock(hh, mm):
                return None
            entry = _entry_from_clock(_midnight_for_date(year, month, day), hh, mm)
            return _minutes_between(entry)
    except ValueError:  # 非法日期（如 2月30日）：只影响这一位用户的时长显示
        return None

    return None


def _flt_calendar_year(now_ms: int) -> int:
    """当前 FLT（UTC+8）历法年份。"""
    from datetime import datetime, timedelta, timezone

    return datetime.fromtimestamp(now_ms / 1000, timezone(timedelta(hours=8))).year


def _class_tokens(class_attr: str) -> set:
    return {token.lower() for token in class_attr.split()}


def _detect_role(tokens: set) -> str:
    if "admin" in tokens:
        return "admin"
    if "staff" in tokens:
        return "staff"
    if "sponsor" in tokens:
        return "sponsor"
    return "player"


def parse_whosin_html(html: str, now_ms: Optional[int] = None) -> List[VenueUser]:
    """从页面 HTML 中解析出在店用户列表（含入店时间与游玩时长）。

    同一昵称可能在页面中出现多次（例如"在线时长"区块会再列一遍昵称），
    本函数按昵称去重：角色取优先级最高者（管理员 > 士大夫 > 赞助者 > 玩家），
    入店时间取首个可解析到的值。因此昵称完全相同的两个人会被合并为一人。
    """
    role_priority = {"admin": 3, "staff": 2, "sponsor": 1, "player": 0}
    matches = [
        match
        for match in NICKNAME_PATTERN.finditer(html)
        if "nickname" in _class_tokens(match.group(1))
    ]
    order: List[str] = []
    best: Dict[str, dict] = {}

    for idx, match in enumerate(matches):
        class_attr, raw_name = match.group(1), match.group(2)
        # 这里得到的是纯文本；后续渲染时由模板引擎统一转义
        name = html_lib.unescape(raw_name).strip()
        if not name:
            continue

        role = _detect_role(_class_tokens(class_attr))

        # 取本昵称之后到下一个昵称之间的 HTML 片段，在其中找入店时间
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(html), match.end() + 800)
        segment = html[match.end():seg_end]
        entered = ""
        em = ENTRY_TIME_PATTERN.search(segment)
        if em:
            entered = html_lib.unescape(em.group(1)).strip()
        else:
            fm = ENTRY_TIME_FALLBACK.search(segment)
            if fm:
                entered = fm.group(1)

        if name not in best:
            best[name] = {"name": name, "role": role, "entered_text": entered}
            order.append(name)
        else:
            if role_priority[role] > role_priority[best[name]["role"]]:
                best[name]["role"] = role
            if not best[name]["entered_text"] and entered:
                best[name]["entered_text"] = entered

    now = int(now_ms if now_ms is not None else _time.time() * 1000)
    users: List[VenueUser] = []
    for name in order:
        info = best[name]
        minutes = parse_entry_minutes(info["entered_text"], now)
        users.append(
            VenueUser(
                name=name,
                role=info["role"],
                entered_text=info["entered_text"],
                minutes=minutes,
            )
        )
    return users


def is_login_page(body: str) -> bool:
    """判断返回内容是否为登录页（页面需要登录时会被重定向）。"""
    return any(marker in body for marker in LOGIN_PAGE_MARKERS)


def _looks_like_login(final_url: str, body: str) -> bool:
    return "/login" in urlsplit(final_url).path or is_login_page(body)


def _to_entry(user: VenueUser) -> dict:
    return {
        "name": user.name,
        "entered": user.entered_text,
        "minutes": user.minutes,
    }


def classify_users(users: List[VenueUser]) -> VenueGroups:
    """将用户按"玩家 / 士大夫 / 管理员"分组；赞助者计入玩家。"""
    players = [_to_entry(u) for u in users if u.role in ("player", "sponsor")]
    staffs = [_to_entry(u) for u in users if u.role == "staff"]
    admins = [_to_entry(u) for u in users if u.role == "admin"]
    return VenueGroups(players=players, staffs=staffs, admins=admins)


def format_text_message(groups: VenueGroups) -> str:
    """生成纯文本消息。

    格式要求：
    - 玩家之间用英文逗号分隔；
    - 管理 / STAFF 单独一行（无则省略）。
    """
    lines = [f"玩家：{','.join(item['name'] for item in groups.players)}"]
    if groups.staff_line:
        lines.append(f"管理/STAFF：{','.join(item['name'] for item in groups.staff_line)}")
    return "\n".join(lines)


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


async def fetch_whosin_users(
    urls: Optional[List[str]] = None,
    timeout: int = 10,
    cookie: str = "",
    username: str = "",
    password: str = "",
    fallback_ips: Optional[List[str]] = None,
) -> FetchResult:
    """按顺序尝试各条访问线路，返回抓取结果。

    :param urls: 目标地址列表（默认使用 DEFAULT_TARGET_URLS）
    :param timeout: 单次请求超时时间（秒）
    :param cookie: 可选的站点 Cookie（优先使用，仅经 HTTPS 发送）
    :param username: 可选的站点账号（用于自动登录）
    :param password: 可选的站点密码（用于自动登录，仅经 HTTPS 发送）
    :param fallback_ips: 备用 IP 列表（None 时使用 DEFAULT_FALLBACK_IPS）
    """
    target_urls = [str(u).strip() for u in (urls or DEFAULT_TARGET_URLS) if str(u).strip()]
    if not target_urls:
        return FetchResult(error="没有配置可用的目标地址", error_kind="config")
    ips = DEFAULT_FALLBACK_IPS if fallback_ips is None else [str(i).strip() for i in fallback_ips if str(i).strip()]

    last_kind, last_error = "network", "没有可用的目标地址"
    for route in _build_routes(target_urls, ips):
        auth_cookie, kind, err = await _resolve_cookie(route, cookie, username, password, timeout)
        if kind == "login_failed":  # 凭据错误：换线路也没用
            return FetchResult(error=err, error_kind=kind)
        if kind:
            last_kind, last_error = kind, err
            continue

        body, final_url, kind, err = await _fetch_html(route, timeout, auth_cookie)
        if kind:
            last_kind, last_error = kind, err
            continue

        if not _looks_like_login(final_url, body):
            return FetchResult(users=parse_whosin_html(body), source_url=route.url, raw_html=body)

        # 需要登录
        if not route.secure:
            last_kind = "login_required"
            last_error = f"{route.url} 是明文 HTTP 地址，出于安全考虑不会发送登录凭据；请改用 HTTPS 地址"
            continue
        if cookie.strip():
            return FetchResult(error="配置的 Cookie 已失效，请重新填写或改用账号密码", error_kind="login_failed")
        if not (username and password):
            return FetchResult(error=LOGIN_REQUIRED_HINT, error_kind="login_required")

        # 缓存的会话已失效：清除缓存，重新登录并重试一次
        _LOGIN_COOKIE_CACHE.pop(route.origin, None)
        auth_cookie, kind, err = await _resolve_cookie(route, cookie, username, password, timeout)
        if kind == "login_failed":
            return FetchResult(error=err, error_kind=kind)
        if kind:
            last_kind, last_error = kind, err
            continue
        body, final_url, kind, err = await _fetch_html(route, timeout, auth_cookie)
        if kind:
            last_kind, last_error = kind, err
            continue
        if _looks_like_login(final_url, body):
            return FetchResult(error="登录成功但页面仍要求登录，请检查账号权限", error_kind="login_failed")
        return FetchResult(users=parse_whosin_html(body), source_url=route.url, raw_html=body)

    return FetchResult(error=last_error, error_kind=last_kind)
