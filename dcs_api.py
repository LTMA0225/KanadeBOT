"""直流会馆在店数据抓取模块。

负责从直流会馆官网抓取"实时在店人员"页面，并解析出昵称、角色、
入店时间与游玩时长（分钟）。

目标地址优先使用域名，失败时自动切换到备用 IP。

认证说明：
- 站点 /whosin 需要登录，本模块支持两种认证方式：
  1. 直接配置 Cookie（cookie 配置项）；
  2. 配置账号密码（login_username / login_password），
     自动调用站点登录接口获取会话 Cookie，并在失效时自动重新登录。

解析约定（与站点前端组件 UserOneline / PresentUser 对应）：
- 昵称元素为带有 ``nickname`` class 的 ``<span>``，角色体现为附加 class：
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
import re
import time as _time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# 默认目标地址：优先域名，失败时自动切换备用 IP
DEFAULT_TARGET_URLS = [
    "https://dcstream.top/whosin",
    "http://47.116.47.191/whosin",
]

# 模拟浏览器的 User-Agent，附带插件标识，便于站点统计
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 DCStreamQueryBot/1.0"
)

# 匹配带有 nickname class 的元素：第一个分组为 class，第二个分组为昵称文本。
# 参考站点结构：<span class="nickname staff">昵称</span>
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

# 需要登录但未配置认证时给出的引导文案
LOGIN_REQUIRED_HINT = (
    "目标页面要求登录。请在插件配置中填写站点账号密码（login_username / login_password），"
    "或填写浏览器中的 Cookie（cookie 项）。"
)

# 已登录会话缓存：origin -> Cookie 字符串（进程内缓存，失效时自动重新登录）
_LOGIN_COOKIE_CACHE: Dict[str, str] = {}

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
    """抓取结果封装。"""

    users: List[VenueUser] = field(default_factory=list)
    source_url: str = ""
    error: Optional[str] = None


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


def _business_midnight(ms: int) -> int:
    """返回 ms 所处"业务日"对应的 UTC 零点毫秒。

    业务日规则（站点 FLT）：UTC+8 凌晨 4 点换日，
    因此业务日 = UTC 日期(ms + 4h)，其零点是 UTC 当日 00:00。
    """
    return ((ms + _FLT_BOUNDARY_MS) // _DAY_MS) * _DAY_MS


def _midnight_for_date(year: int, month: int, day: int) -> int:
    """给定日历日期，返回其 UTC 零点毫秒。"""
    from datetime import datetime, timezone

    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _entry_from_clock(midnight: int, hh: int, mm: int) -> int:
    """业务日零点 + 站点显示时钟 → 毫秒时间戳。

    站点 toFLT 显示：hour = (T + 8h - 业务日零点) / 1h，
    即显示时钟范围 04:00 ~ 27:59（UTC+8 挂钟：24 点后表示次日凌晨）。
    因此 T = 业务日零点 + (hh - 8) * 1h + mm。
    """
    return midnight + (hh - 8) * 3600000 + mm * 60000


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

    # 今天：HH:MM（业务日内，显示范围 04:00 - 27:59）
    m = _ENTRY_TODAY.match(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        entry = _entry_from_clock(_business_midnight(now), hh, mm)
        if entry > now + 60000:  # 容错：跨业务日边界
            entry -= _DAY_MS
        return _minutes_between(entry)

    # 昨天 / 前天
    for pattern, back in ((_ENTRY_YESTERDAY, 1), (_ENTRY_DAYBEFORE, 2)):
        m = pattern.match(text)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            entry = _entry_from_clock(_business_midnight(now) - back * _DAY_MS, hh, mm)
            return _minutes_between(entry)

    # 周X HH:MM（最近一次该星期几对应的业务日）
    m = _ENTRY_WEEKDAY.match(text)
    if m:
        target = _WEEKDAY_INDEX[m.group(1)]
        hh, mm = int(m.group(2)), int(m.group(3))
        now_midnight = _business_midnight(now)
        now_dnum = now_midnight // _DAY_MS
        now_weekday = (now_dnum + 4) % 7  # 1970-01-01 为周四；0 = 周日
        days_back = (now_weekday - target) % 7
        entry = _entry_from_clock(now_midnight - days_back * _DAY_MS, hh, mm)
        if entry > now + 60000:
            entry -= 7 * _DAY_MS
        return _minutes_between(entry)

    # M月D日 HH:MM（默认当年；若显著晚于当前则视为去年）
    m = _ENTRY_MONTHDAY.match(text)
    if m:
        month, day, hh, mm = (int(v) for v in m.groups())
        year = _flt_calendar_year(now)
        entry = _entry_from_clock(_midnight_for_date(year, month, day), hh, mm)
        if entry > now + 7 * _DAY_MS:
            entry = _entry_from_clock(_midnight_for_date(year - 1, month, day), hh, mm)
        return _minutes_between(entry)

    # YYYY.MM.DD HH:MM
    m = _ENTRY_FULLDATE.match(text)
    if m:
        year, month, day, hh, mm = (int(v) for v in m.groups())
        entry = _entry_from_clock(_midnight_for_date(year, month, day), hh, mm)
        return _minutes_between(entry)

    return None


def _flt_calendar_year(now_ms: int) -> int:
    """当前 FLT（UTC+8）历法年份。"""
    from datetime import datetime, timedelta, timezone

    return datetime.fromtimestamp(now_ms / 1000, timezone(timedelta(hours=8))).year


def _detect_role(class_attr: str) -> str:
    lowered = class_attr.lower()
    if "admin" in lowered:
        return "admin"
    if "staff" in lowered:
        return "staff"
    if "sponsor" in lowered:
        return "sponsor"
    return "player"


def parse_whosin_html(html: str, now_ms: Optional[int] = None) -> List[VenueUser]:
    """从页面 HTML 中解析出在店用户列表（含入店时间与游玩时长）。

    同一昵称可能在页面中出现多次（例如"在线时长"区块会再列一遍昵称），
    本函数按昵称去重：角色取优先级最高者（管理员 > 士大夫 > 赞助者 > 玩家），
    入店时间取首个可解析到的值。
    """
    role_priority = {"admin": 3, "staff": 2, "sponsor": 1, "player": 0}
    matches = list(NICKNAME_PATTERN.finditer(html))
    order: List[str] = []
    best: Dict[str, dict] = {}

    for idx, match in enumerate(matches):
        class_attr, raw_name = match.group(1), match.group(2)
        name = html_lib.unescape(raw_name).strip()
        if not name:
            continue

        role = _detect_role(class_attr)

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
    return f"{parts.scheme}://{parts.netloc}"


async def site_login(
    origin: str,
    username: str,
    password: str,
    timeout: int = 10,
) -> Optional[str]:
    """调用站点登录接口，成功时返回可用于请求的 Cookie 字符串。

    站点接口约定（见 dcs-web 源码 app/api/login/route.ts）：
    - POST {origin}/api/login，JSON 参数 {"username": ..., "password": ...}
    - 成功：200 且响应体 {"success": true}，并通过 Set-Cookie 下发 session
    """
    import aiohttp

    login_url = origin.rstrip("/") + "/api/login"
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(
                login_url,
                json={"username": username, "password": password},
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                if resp.status != 200:
                    return None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return None
                if not isinstance(data, dict) or not data.get("success"):
                    return None

                morsel = resp.cookies.get("session")
                if morsel is not None:
                    return f"session={morsel.value}"

                # 兼容通过响应头下发 Cookie 的情况
                match = re.search(r"session=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
                if match:
                    return f"session={match.group(1)}"
    except Exception:  # noqa: BLE001 - 登录失败由调用方统一处理
        return None
    return None


async def _resolve_cookie(
    origin: str,
    cookie: str,
    username: str,
    password: str,
    timeout: int,
) -> str:
    """按优先级获取请求用 Cookie：显式配置 > 缓存登录 > 现场登录。"""
    if cookie.strip():
        return cookie.strip()
    if not (username and password):
        return ""
    cached = _LOGIN_COOKIE_CACHE.get(origin)
    if cached:
        return cached
    fresh = await site_login(origin, username, password, timeout)
    if fresh:
        _LOGIN_COOKIE_CACHE[origin] = fresh
    return fresh or ""


async def _fetch_html(
    url: str,
    timeout: int,
    cookie: str,
) -> Tuple[str, str, Optional[str]]:
    """请求页面 HTML，返回 (页面内容, 最终地址, 错误信息)。"""
    import aiohttp

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie.strip():
        headers["Cookie"] = cookie.strip()

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=max(3, int(timeout)))
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return "", str(resp.url), f"{url} 返回状态码 {resp.status}"
                body = await resp.text()
                return body, str(resp.url), None
    except Exception as exc:  # noqa: BLE001 - 汇总所有异常供调用方切换地址
        return "", url, f"{url} 访问失败：{exc}"


async def fetch_whosin_users(
    urls: Optional[List[str]] = None,
    timeout: int = 10,
    cookie: str = "",
    username: str = "",
    password: str = "",
) -> FetchResult:
    """按顺序尝试目标地址，返回抓取结果。

    :param urls: 目标地址列表（默认使用 DEFAULT_TARGET_URLS）
    :param timeout: 单次请求超时时间（秒）
    :param cookie: 可选的站点 Cookie（优先使用）
    :param username: 可选的站点账号（用于自动登录）
    :param password: 可选的站点密码（用于自动登录）
    """
    target_urls = [u for u in (urls or DEFAULT_TARGET_URLS) if u]
    if not target_urls:
        return FetchResult(error="没有配置可用的目标地址")

    last_error = "没有可用的目标地址"
    for url in target_urls:
        origin = _origin_of(url)
        auth_cookie = await _resolve_cookie(origin, cookie, username, password, timeout)

        body, final_url, err = await _fetch_html(url, timeout, auth_cookie)
        if err is not None:
            last_error = err
            continue

        if "/login" in final_url or is_login_page(body):
            # 登录态失效：若配置了账号密码，则清除缓存重新登录并重试一次
            if username and password and not cookie.strip():
                _LOGIN_COOKIE_CACHE.pop(origin, None)
                auth_cookie = await _resolve_cookie(origin, cookie, username, password, timeout)
                if auth_cookie:
                    body, final_url, err = await _fetch_html(url, timeout, auth_cookie)
                    if err is None and "/login" not in final_url and not is_login_page(body):
                        users = parse_whosin_html(body)
                        return FetchResult(users=users, source_url=url)
                last_error = "自动登录失败：请检查插件配置中的站点账号 / 密码是否正确"
            else:
                last_error = LOGIN_REQUIRED_HINT
            continue

        users = parse_whosin_html(body)
        return FetchResult(users=users, source_url=url)

    return FetchResult(error=last_error)
