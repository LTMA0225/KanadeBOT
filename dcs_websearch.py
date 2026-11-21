
"""联网检索与网页取文（不依赖 DeepSeek 搜索接口，带多引擎回退）。"""

from __future__ import annotations

import base64
import html as ihtml
import re
import time
from urllib.parse import quote, unquote

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.6",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)

# ---- 限流冷却：402/429/403/5xx 统一记账，引擎与代理共用 ----
_COOL_UNTIL: dict = {}
_COOL_SECONDS = {402: 240, 429: 240, 403: 120, 503: 90, 502: 90, 500: 60}
_LAST_ALL_FAILED = False


def _cooling(name: str) -> bool:
    return time.time() < _COOL_UNTIL.get(name, 0.0)


def _resp_bad(name: str, status: int) -> bool:
    """非 200 一律算失败；限流/封禁类状态记冷却，别再原地连撞。"""
    if status == 200:
        return False
    if status in _COOL_SECONDS:
        _COOL_UNTIL[name] = time.time() + _COOL_SECONDS[status]
    return True


def search_sources_exhausted() -> bool:
    """上一次 search_web 是否「一个源都没给出结果」（用于提示稍等再搜）。"""
    return _LAST_ALL_FAILED


_BAD_HOSTS = (
    "bing.com/ck/a",
    "www.bing.com/ck/a",
    "so.com/link",
    "www.so.com/link",
    "sogou.com/link",
    "duckduckgo.com/y.js",
)
_DATE_RE = re.compile(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})")


def _clean(text: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", ihtml.unescape(text)).strip()


def _unwrap_bing(link: str) -> str:
    link = ihtml.unescape(link or "")
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", link)
    if not m:
        return link
    raw = m.group(1)
    raw += "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return link
    if decoded.startswith("http"):
        return decoded
    inner = re.search(r"[?&]url=([^&]+)", decoded)
    return unquote(inner.group(1)) if inner else link


def _link_ok(url: str) -> bool:
    if not url.startswith("http"):
        return False
    lowered = url.lower()
    if any(bad in lowered for bad in _BAD_HOSTS):
        return False
    # 图片/导航/纯首页类结果对取文没帮助
    if re.search(r"image\.(so|baidu|bing|google)\.com|/i\?src=imageonebox", lowered):
        return False
    return True


def _pack(title, url, summary, date="") -> dict:
    date = _clean(date)
    m = _DATE_RE.search(date) or _DATE_RE.search(_clean(summary))
    return {
        "title": _clean(title)[:120],
        "url": ihtml.unescape(url or "").strip(),
        "summary": _clean(summary)[:400],
        "date": m.group(1) if m else "",
    }


def _store(items, raw, count):
    for item in raw:
        if not item["title"] or not _link_ok(item["url"]):
            continue
        if any(item["url"] == seen["url"] for seen in items):
            continue
        items.append(item)
        if len(items) >= count:
            break


async def _so(session, query: str, count: int):
    """360 搜索：中文结果质量最好。"""
    if _cooling("so"):
        return []
    async with session.get("https://www.so.com/s", params={"q": query}) as resp:
        if _resp_bad("so", resp.status):
            return []
        body = await resp.text(errors="ignore")
    items = []
    raw = []
    for block in re.findall(r"(?s)<li class=\"res-list\".*?</li>", body)[: count * 2]:
        head = re.search(r"(?s)<h3[^>]*>(.*?)</h3>", block)
        link = re.search(r'data-mdurl="([^"]+)"', block) or re.search(
            r'(?s)<h3[^>]*>.*?<a[^>]+href="([^"]+)"', block
        )
        snip = re.search(r'(?s)class="res-list-summary"[^>]*>(.*?)</span>', block)
        if not head or not link:
            continue
        raw.append(_pack(head.group(1), link.group(1), snip.group(1) if snip else ""))
    _store(items, raw, count)
    return items


async def _bing(session, query: str, count: int):
    url = "https://www.bing.com/search"
    params = {"q": query, "format": "rss", "setlang": "zh-CN", "count": str(count)}
    if _cooling("bing"):
        return []
    async with session.get(url, params=params) as resp:
        if _resp_bad("bing", resp.status):
            return []
        body = await resp.text(errors="ignore")
    items = []
    raw = []
    for block in re.findall(r"(?s)<item>(.*?)</item>", body)[:count]:
        def field(tag):
            m = re.search(rf"(?s)<{tag}>(.*?)</{tag}>", block)
            return m.group(1) if m else ""

        raw.append(
            _pack(
                field("title"),
                _unwrap_bing(_clean(field("link"))) or field("link").strip(),
                field("description"),
                field("pubDate"),
            )
        )
    _store(items, raw, count)
    return items


async def _bing_html(session, query: str, count: int):
    url = "https://www.bing.com/search"
    params = {"q": query, "setlang": "zh-CN", "count": str(count)}
    if _cooling("bing_html"):
        return []
    async with session.get(url, params=params) as resp:
        if _resp_bad("bing_html", resp.status):
            return []
        body = await resp.text(errors="ignore")
    items = []
    raw = []
    for block in re.findall(r'(?s)<li class="b_algo".*?</li>', body)[: count * 2]:
        head = re.search(r'(?s)<h2>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not head:
            continue
        snip = re.search(r"(?s)<p[^>]*>(.*?)</p>", block)
        raw.append(
            _pack(head.group(2), _unwrap_bing(head.group(1)), snip.group(1) if snip else "")
        )
    _store(items, raw, count)
    return items


async def _ddg(session, query: str, count: int):
    if _cooling("ddg"):
        return []
    async with session.post(
        "https://html.duckduckgo.com/html/", data={"q": query, "kl": "cn-zh"}
    ) as resp:
        if _resp_bad("ddg", resp.status):
            return []
        body = await resp.text(errors="ignore")
    items = []
    raw = []
    links = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
    snips = re.findall(r"class=['\"]result__snippet['\"]>(.*?)</a>", body, re.S)
    for idx, (link, title) in enumerate(links[:count]):
        target = link
        m = re.search(r"uddg=([^&]+)", link)
        if m:
            target = unquote(m.group(1))
        raw.append(_pack(title, target, snips[idx] if idx < len(snips) else ""))
    _store(items, raw, count)
    return items


async def _yahoo_jp(session, query: str, count: int):
    """Yahoo! 搜索（日本）：日中混合时命中率最好，证书链在本机有问题，走 ssl=False。"""
    if _cooling("yahoo"):
        return []
    async with session.get(
        "https://search.yahoo.co.jp/search", params={"p": query}, ssl=False
    ) as resp:
        if _resp_bad("yahoo", resp.status):
            return []
        body = await resp.text(errors="ignore")
    items = []
    raw = []
    blocks = re.findall(
        r'(?s)<div class="sw-Card Algo(?![^"]*(?:imageSnippet|Visual|Gallery))[^"]*">.*?</section>',
        body,
    )[: count * 3]
    for block in blocks:
        head = re.search(
            r'(?s)<a[^>]+href="([^"]+)"[^>]*class="sw-Card__title(?:Inner|Main)?"', block
        ) or re.search(r'(?s)class="sw-Card__titleInner"[^>]*href="([^"]+)"', block)
        title = re.search(r"(?s)<h3[^>]*>(.*?)</h3>", block)
        snip = re.search(r'(?s)class="sw-Card__summary"[^>]*>(.*?)</p>', block)
        if not head or not title:
            continue
        raw.append(_pack(title.group(1), head.group(1), snip.group(1) if snip else ""))
    _store(items, raw, count)
    return items


# 顺序：中文引擎优先，其余兜底
ENGINES = (_so, _bing, _yahoo_jp, _bing_html, _ddg)


_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
# 中文短词查询常被搜索引擎误判，交给中文引擎；含假名时先走日文引擎
_EN_ZH = (_so, _bing, _bing_html, _yahoo_jp, _ddg)
_EN_JA = (_yahoo_jp, _so, _bing, _bing_html, _ddg)


def _order_engines(query: str):
    kana = len(_KANA_RE.findall(query))
    zh = len(_ZH_RE.findall(query))
    return _EN_JA if kana and kana >= zh / 2 else _EN_ZH


async def search_web(query: str, count: int = 5) -> list:
    query = str(query or "").strip()
    if not query:
        return []
    global _LAST_ALL_FAILED
    items: list = []
    # 按「中文 / 日中」自动排序：含假名、含中文占比高的查询优先不同引擎
    ordered = _order_engines(query)
    tried = 0
    async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
        for engine in ordered:
            try:
                part = await engine(session, query, count)
            except Exception:
                part = []
            tried += 1
            if part:
                _store(items, part, count)
            # 先到的引擎有货就够用了，别把所有引擎都打一遍
            if len(items) >= min(3, count):
                break
    _LAST_ALL_FAILED = not items and tried > 0
    return items[:count]


async def search_site(keyword: str, sites: str, hint: str = "", count: int = 4) -> list:
    """站点限定检索。多个 site: 用 OR 拼起来常被引擎忽略，所以逐个站点单独查，
    站点命中优先；全都查不到时才用自然语言结果兜底。
    """
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    hosts = []
    for h in (sites or "").split(" OR "):
        h = h.strip().replace("site:", "").split("/")[0]
        if h and h not in hosts:
            hosts.append(h)

    seen: set = set()
    hits: list = []

    def _key(item: dict) -> str:
        return re.sub(r"[?#].*$", "", item["url"]).rstrip("/")

    def _take(urls_items):
        out = []
        for p in urls_items:
            k = _key(p)
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    for host in hosts:
        if len(hits) >= count:
            break
        try:
            part = await search_web(f"{keyword} site:{host}", count + 2)
        except Exception:
            part = []
        for p in _take(part):
            if host in p["url"]:
                hits.append(p)
    if len(hits) >= count:
        return hits[:count]

    for query in (hint, f"{keyword} wiki", keyword):
        if not query or len(hits) >= count:
            break
        try:
            part = await search_web(query, count + 2)
        except Exception:
            part = []
        hits.extend(_take(part))
    return hits[:count]


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html or "")
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)</(p|div|h[1-6]|li|tr|br)>", "\n", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    text = ihtml.unescape(html)
    text = re.sub(r"[ \t\u3000]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _meta_desc(html: str) -> str:
    m = re.search(
        r'(?is)<meta[^>]+name=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)',
        html,
    )
    return _clean(m.group(1)) if m else ""


async def _direct(session, url: str) -> str:
    if _cooling("direct:" + (url.split("/")[2].lower() if "//" in url else "")):
        return ""
    async with session.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True) as resp:
        if _resp_bad("direct:" + (url.split("/")[2].lower() if "//" in url else ""), resp.status):
            return ""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "text" not in ctype and "xml" not in ctype:
            return ""
        body = await resp.text(errors="ignore")
    desc = _meta_desc(body)
    text = html_to_text(body)
    if desc and desc not in text[:800]:
        text = desc + "\n" + text
    return text


_JINA_BLOCK_UNTIL = 0.0  # jina 免费额度被限流（402）后短暂冷却
_JINA_COOLDOWN = 180


async def _jina(session, url: str) -> str:
    if _cooling("jina"):
        return ""
    async with session.get(
        "https://r.jina.ai/" + url, timeout=aiohttp.ClientTimeout(total=12)
    ) as resp:
        if resp.status == 402:  # 额度用完/限流，冷却几分钟别再连撞
            _COOL_UNTIL["jina"] = time.time() + max(_JINA_COOLDOWN, 240)
            return ""
        if _resp_bad("jina", resp.status):
            return ""
        text = await resp.text(errors="ignore")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


async def _allorigins(session, url: str) -> str:
    """备用取文代理：jina 偶发 402 时兜底。"""
    from urllib.parse import quote as _quote

    api = "https://api.allorigins.win/raw?url=" + _quote(url, safe="")
    if _cooling("allorigins"):
        return ""
    async with session.get(api, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        if _resp_bad("allorigins", resp.status):
            return ""
        body = await resp.text(errors="ignore")
    if not body or body.lstrip().startswith("{"):
        return ""
    desc = _meta_desc(body)
    text = html_to_text(body)
    if desc and desc not in text[:800]:
        text = desc + "\n" + text
    return text


# 萌百：直连页面基本超时或 403，改走 MediaWiki 解析接口拿正文，快且稳定
_MOEGIRL_API_HOSTS = ("moegirl.uk", "moegirl.icu", "zh.moegirl.org.cn")


def _moegirl_title(url: str) -> str:
    m = re.match(r"https?://[^/]+/([^?#]+)", url)
    if not m:
        return ""
    title = unquote(m.group(1)).strip("/")
    if not title or title.startswith("index.php") or ":" in title:
        return ""
    return title


async def _moegirl_api(session, url: str) -> str:
    title = _moegirl_title(url)
    if not title:
        return ""
    q = quote(title, safe="")
    for host in _MOEGIRL_API_HOSTS:
        api = (
            f"https://{host}/api.php?action=parse&page={q}"
            "&prop=text&format=json&formatversion=2"
        )
        try:
            async with session.get(api, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
        except Exception:
            continue
        body = ((data or {}).get("parse") or {}).get("text") or ""
        text = html_to_text(body) if body else ""
        if len(text) >= 400:
            return text
    return ""


async def _codetabs(session, url: str) -> str:
    """又一个备用取文代理，allorigins 抽风时顶上。"""
    from urllib.parse import quote as _quote

    api = "https://api.codetabs.com/v1/proxy?quest=" + _quote(url, safe="")
    if _cooling("codetabs"):
        return ""
    async with session.get(api, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if _resp_bad("codetabs", resp.status):
            return ""
        body = await resp.text(errors="ignore")
    if not body or body.lstrip().startswith("{"):
        return ""
    text = html_to_text(body)
    return text


# 这些站点直连基本必被挡（403/超时），直接用阅读代理取，省掉白等的二十秒
_PREFER_PROXY_HOSTS = ("moegirl.org.cn", "moegirl.uk", "moegirl.icu", "zhihu.com", "weibo.com")


async def fetch_text(url: str, limit: int = 6000, skip_direct: bool = False) -> str:
    """取正文：直连优先；被拦（403/空内容/超时）时才用代理兜底，代理失败直接放弃。"""
    url = str(url or "").strip()
    if not url.startswith("http"):
        return ""
    host = url.split("/")[2].lower() if "//" in url else ""
    is_moegirl = "moegirl" in host
    prefer_proxy = any(h in host for h in _PREFER_PROXY_HOSTS)
    text = ""
    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=aiohttp.ClientTimeout(total=25)
    ) as session:
        if is_moegirl and not skip_direct:
            try:
                text = await _moegirl_api(session, url)
            except Exception:
                text = ""
        if not text and not skip_direct and not prefer_proxy:
            try:
                text = await _direct(session, url)
            except Exception:
                text = ""
        if len(text) < 400:
            for alt_get in (_jina, _allorigins, _codetabs):
                if len(text) >= 400:
                    break
                attempts = 2 if alt_get is _jina else 1
                for _ in range(attempts):
                    try:
                        alt = await alt_get(session, url)
                    except Exception:
                        alt = ""
                    if len(alt) > len(text):
                        text = alt
                    if len(text) >= 400 or not alt:
                        break
        if len(text) < 400 and prefer_proxy and not is_moegirl:
            try:
                alt = await _direct(session, url)
                if len(alt) > len(text):
                    text = alt
            except Exception:
                pass
    return text[:limit]
