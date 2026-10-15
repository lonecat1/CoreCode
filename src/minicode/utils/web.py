"""轻量 Web 能力：带重试的抓取、正文抽取、双后端搜索。

对应原版 `MiniCode/src/utils/web.ts`（506 行）。

只用标准库的 `urllib` 与 `html.parser`，不引入 requests / bs4。

## 三个后端相关的能力（此前完全缺失）

**① 请求重试。** 429 与 5xx 是**暂时性**失败，网络抖动也是。直接把它们变成
工具错误，会让「再试一次就好了」的情况在用户看来是「搜索坏了」。退避是
`300ms × 2^n`，上限 2 次重试——够覆盖抖动，又不至于让一次请求卡很久。

**② 域名过滤。** `allowed_domains` / `blocked_domains` 让模型能说「只在
docs.python.org 里找」。匹配含**子域**（`host == domain` 或
`host.endswith("." + domain)`），否则 `python.org` 会漏掉 `docs.python.org`。
两者**不能同时给**——语义会变得含糊（既限定范围又排除其中一部分）。

**③ 双搜索后端。** DuckDuckGo Lite 优先，失败时退到搜狗。单一后端被限流或
改版时，搜索就整个不可用；两个后端让「换个源」成为自动行为。

## 一处有意差异（D36）

原版的正文抽取把**所有空白折叠成单个空格**，整页输出成一行。本实现用
`HTMLParser` 保留段落结构（多行）。

理由：一整页压成一行后，无论用户在终端里读还是在会话记录里回看都是灾难；
而段落结构对模型理解页面也更有用。见 `docs/divergences.md` D36。
"""

from __future__ import annotations

import html as html_module
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable

#: 浏览器式 UA。原版如此。
#:
#: 诚实声明自己是脚本会被不少站点直接拒绝（403），这不是礼貌问题而是可用性
#: 问题——搜索与抓取都会整个失效。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 MiniCode/0.1"
)

#: 单次请求的超时（毫秒）。原版 `DEFAULT_TIMEOUT_MS`。
DEFAULT_TIMEOUT_MS = 12_000
#: 默认最大重试次数（**不含首次尝试**）。
DEFAULT_MAX_RETRIES = 2
#: 退避基数（毫秒）。实际等待 `BACKOFF_BASE_MS * 2^attempt`。
BACKOFF_BASE_MS = 300

#: 抓取正文的默认上限。
DEFAULT_MAX_CHARS = 12_000
#: 搜索结果的默认条数。原版是 5（不是 8）。
DEFAULT_MAX_RESULTS = 5

#: 搜索后端，按优先级排列。前者失败或没有结果时退到后者。
SEARCH_PROVIDERS: tuple[str, ...] = ("duckduckgo-lite", "sogou")

_DDG_LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
_SOGOU_ENDPOINT = "https://www.sogou.com/web"

#: 可重试的网络错误码。原版逐项列举，这里对齐。
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "ETIMEDOUT",
        "ECONNRESET",
        "EAI_AGAIN",
        "ENOTFOUND",
        "ECONNREFUSED",
        "UND_ERR_CONNECT_TIMEOUT",
        "UND_ERR_HEADERS_TIMEOUT",
        "UND_ERR_BODY_TIMEOUT",
    }
)

#: 这些标签的内容整体丢弃（不是正文）。
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe"}
)
#: 这些标签视为块级，前后换行。
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "pre", "blockquote",
        "table", "ul", "ol", "dl", "dt", "dd", "figure", "figcaption", "hr",
    }
)


class WebError(Exception):
    """网络或解析失败。"""


@dataclass(slots=True)
class HttpResponse:
    """一次 HTTP 响应的快照。

    把状态码、头、正文放在一个对象里，是为了让**重试逻辑与解析逻辑分开**：
    `HTTPError`（4xx/5xx）在原版里是**响应**而不是异常（fetch 的语义），
    urllib 却把它抛成异常。这层封装抹平了差异。
    """

    status: int
    status_text: str
    content_type: str
    final_url: str
    body: bytes

    def text(self) -> str:
        return decode_body(self.body, self.content_type)


@dataclass(slots=True)
class WebPage:
    url: str
    final_url: str
    status: int
    status_text: str
    content_type: str
    content: str
    title: str | None = None


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


# ---------------------------------------------------------------------------
# HTML 小工具
# ---------------------------------------------------------------------------

_ENTITY_MAP = (
    ("&amp;", "&"),
    ("&quot;", '"'),
    ("&#x27;", "'"),
    ("&#39;", "'"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&#x2F;", "/"),
    ("&#47;", "/"),
    ("&nbsp;", " "),
)


def decode_html(value: str) -> str:
    """解开常见的 HTML 实体。**顺序敏感**：`&amp;` 必须最先解。"""
    result = value
    for entity, replacement in _ENTITY_MAP:
        result = result.replace(entity, replacement)
    return result


def strip_tags(value: str) -> str:
    """去掉标签并把连续空白折成一个空格。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def first_match(
    pattern: str, text: str, group: int = 1, flags: int = re.IGNORECASE | re.DOTALL
) -> str | None:
    """取第一个匹配的某个分组。没有匹配（或该分组不存在）返回 `None`。"""
    match = re.search(pattern, text, flags)
    if not match:
        return None
    try:
        return match.group(group)
    except IndexError:
        return None


def extract_title(html_text: str) -> str | None:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_text, re.IGNORECASE)
    if not match:
        return None
    return decode_html(strip_tags(match.group(1))).strip() or None


class _TextExtractor(HTMLParser):
    """把 HTML 抽成可读文本（保留段落结构）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._drop_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [line.strip() for line in raw.splitlines()]
        kept: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if kept and not blank:
                    kept.append("")
                blank = True
                continue
            kept.append(line)
            blank = False
        return "\n".join(kept).strip()

    def title(self) -> str | None:
        text = " ".join("".join(self.title_parts).split())
        return text or None


def extract_readable_text(html_text: str) -> tuple[str, str | None]:
    """抽出正文与标题。

    **与 D36 相关**：这里保留段落结构（多行），而原版把所有空白折叠成一行。
    """
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不该让抓取失败
        pass
    return parser.text(), parser.title()


def extract_html_redirect_url(html_text: str, base_url: str) -> str | None:
    """从 HTML 里找出「跳转目标」（`window.location` 或 meta refresh）。

    很多站点用一个只含跳转脚本的壳页做中转。不跟进的话，抓回来的是空壳，
    用户看到「内容为空」却不知道发生了什么。
    """
    script_redirect = first_match(
        r"window\.location(?:\.href)?(?:\.replace)?\((['\"])(.*?)\1\)", html_text, 2
    ) or first_match(
        r"window\.location(?:\.href)?\s*=\s*(['\"])(.*?)\1", html_text, 2
    )
    meta_refresh = first_match(
        r"<meta[^>]*http-equiv=(['\"])refresh\1[^>]*content=(['\"])[\s\S]*?url\s*=\s*('?)([^\"'>;]+)\3[\s\S]*?\2[^>]*>",
        html_text,
        4,
    ) or first_match(
        r"<meta[^>]*content=(['\"])[\s\S]*?url\s*=\s*('?)([^\"'>;]+)\2[\s\S]*?\1[^>]*http-equiv=(['\"])refresh\4[^>]*>",
        html_text,
        3,
    )

    raw = decode_html((script_redirect or meta_refresh or "").strip())
    if not raw:
        return None
    return urllib.parse.urljoin(base_url, raw)


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def is_retryable_status(status: int) -> bool:
    """429 与 5xx 是可重试的。**4xx 其余不可重试**——重试不会让 404 变成 200。"""
    return status == 429 or 500 <= status < 600


def is_retryable_network_error(error: BaseException) -> bool:
    """网络抖动可重试。超时也算。"""
    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    code = getattr(error, "errno", None)
    name = getattr(error, "reason", None)
    for candidate in (code, name):
        if isinstance(candidate, str) and candidate in _RETRYABLE_ERROR_CODES:
            return True
    reason = getattr(error, "reason", None)
    if isinstance(reason, BaseException):
        return is_retryable_network_error(reason)
    # urllib 的网络错误字符串里通常带错误码。
    text = str(error)
    return any(code in text for code in _RETRYABLE_ERROR_CODES)


def format_web_error_message(url: str, error: BaseException | None, timeout_ms: int) -> str:
    """结构化的错误信息。原版 `formatWebErrorMessage`。

    刻意带上 URL 与超时值：只说「请求失败」时，用户无法判断是地址写错了、
    站点挂了、还是超时太短。
    """
    if isinstance(error, (socket.timeout, TimeoutError)):
        return f"request timed out after {timeout_ms}ms for {url}"
    reason = getattr(error, "reason", None)
    if isinstance(reason, BaseException):
        return f"{type(reason).__name__}: {reason} ({url})"
    if error is not None:
        return f"{type(error).__name__}: {error} ({url})"
    return f"request failed for {url}"


def _sleep_ms(milliseconds: float) -> None:
    time.sleep(max(0.0, milliseconds) / 1000.0)


def fetch_with_retry(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> HttpResponse:
    """带重试与退避的请求。

    ## 为什么把 HTTP 错误码当成「响应」而不是异常

    `fetch` 的语义是：只有**网络层**失败才抛异常，4xx/5xx 是一个正常返回的
    `Response`（带 `ok: false`）。原版依赖这个语义来统一处理「可重试状态码」。

    `urllib` 相反：4xx/5xx 抛 `HTTPError`。这层把两者抹平——**HTTP 错误码
    走返回值路径**，只有网络层错误才抛。

    这样做还有一个实际好处：非重试的 4xx（比如 404）也能带着响应体返回，
    调用方可以决定怎么呈现，而不是拿到一个丢失了正文的异常。
    """
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last_error: BaseException | None = None
    last_response: HttpResponse | None = None
    target = url

    for attempt in range(max_retries + 1):
        request = urllib.request.Request(
            target, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_ms / 1000.0) as response:
                snapshot = HttpResponse(
                    status=int(response.status),
                    status_text=str(getattr(response, "reason", "") or ""),
                    content_type=str(response.headers.get("content-type") or ""),
                    final_url=str(response.geturl()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            body = b""
            try:
                body = error.read()
            except Exception:  # noqa: BLE001
                pass
            snapshot = HttpResponse(
                status=int(error.code),
                status_text=str(error.reason or ""),
                content_type=str(error.headers.get("content-type") if error.headers else ""),
                final_url=target,
                body=body,
            )
        except (urllib.error.URLError, OSError) as error:
            last_error = error
            if attempt < max_retries and is_retryable_network_error(error):
                _sleep_ms(BACKOFF_BASE_MS * (2**attempt))
                continue
            raise WebError(format_web_error_message(target, error, timeout_ms)) from error

        last_response = snapshot
        if is_retryable_status(snapshot.status) and attempt < max_retries:
            _sleep_ms(BACKOFF_BASE_MS * (2**attempt))
            continue
        return snapshot

    if last_response is not None:
        return last_response
    raise WebError(format_web_error_message(target, last_error, timeout_ms))


# ---------------------------------------------------------------------------
# 域名过滤
# ---------------------------------------------------------------------------


def normalize_domain_list(domains: Iterable[str] | None) -> list[str]:
    """归一化域名清单：小写、去掉 `*.` / `.` 前缀、能从 URL 里抽出主机名。

    用户（或模型）可能写 `Example.COM`、`*.example.com`、`https://example.com/x`
    ——三种都要能认。认不出来的原样保留，交给后面的匹配去处理。
    """
    normalized: list[str] = []
    for raw in domains or []:
        text = str(raw).strip().lower()
        if not text:
            continue
        without_wildcard = re.sub(r"^\*\.", "", re.sub(r"^\.", "", text))
        try:
            host = urllib.parse.urlparse(without_wildcard).hostname
        except ValueError:
            host = None
        normalized.append((host or without_wildcard).lower())
    return [item for item in normalized if item]


def matches_domain(host: str, domain: str) -> bool:
    """主机是否属于该域名。**含子域。**

    只做后缀匹配会漏掉裸域（`python.org` 不匹配 `python.org` 自己）；
    只做相等会漏掉子域（`docs.python.org`）。两者都要。
    """
    return host == domain or host.endswith(f".{domain}")


def passes_domain_filter(link: str, allowed: list[str], blocked: list[str]) -> bool:
    """链接是否通过域名过滤。

    顺序是「先排除，再限定」：黑名单优先。解析不出主机名的链接**直接排除**
    ——它不是我们能判断的东西，放过去等于让过滤器形同虚设。
    """
    try:
        host = (urllib.parse.urlparse(link).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if any(matches_domain(host, domain) for domain in blocked):
        return False
    if not allowed:
        return True
    return any(matches_domain(host, domain) for domain in allowed)


# ---------------------------------------------------------------------------
# 搜索后端
# ---------------------------------------------------------------------------


def normalize_duckduckgo_link(raw_href: str) -> str:
    """DuckDuckGo 会把结果包在跳转链接里（`uddg=` 参数），剥掉外层。"""
    href = decode_html(raw_href).strip()
    if not href:
        return ""
    absolute = f"https:{href}" if href.startswith("//") else href
    parsed = urllib.parse.urlparse(absolute)
    query = urllib.parse.parse_qs(parsed.query)
    redirect = query.get("uddg")
    if redirect:
        return urllib.parse.unquote(redirect[0])
    return absolute


def normalize_sogou_link(raw_href: str) -> str:
    """搜狗的结果链接有站内相对路径与协议相对路径两种。

    ## 一处有意修正（D37）

    原版的分支顺序是：

        if (href.startsWith('/'))  return `https://www.sogou.com${href}`
        if (href.startsWith('//')) return `https:${href}`      ← 永远走不到

    `//x` 同样以 `/` 开头，因此第二个分支是**死代码**，协议相对链接会被拼成
    `https://www.sogou.com//www.sogou.com/link` —— 一个坏 URL，而且失败是
    静默的（搜索结果里出现一条打不开的链接）。

    这里把 `//` 的判断放在前面。
    """
    href = decode_html(raw_href).strip()
    if not href:
        return ""
    # `//` 必须先判——否则会被下面的 `/` 分支吃掉。见上面的说明。
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return f"https://www.sogou.com{href}"
    return href


def parse_duckduckgo_lite(html_text: str) -> list[SearchResult]:
    """解析 DuckDuckGo **Lite** 页面。

    与 `html.duckduckgo.com` 的页面结构**完全不同**（那里是 `result__a` /
    `result__snippet`，这里是 `result-link` / `result-snippet` / `link-text`）。
    换端点而不换解析器会得到「永远没有结果」——而且看起来像「确实没搜到」。
    """
    results: list[SearchResult] = []
    anchors = [
        (match.start(), match.group(0))
        for match in re.finditer(r"<a\b[^>]*>[\s\S]*?</a>", html_text, re.IGNORECASE)
    ]

    for index, (position, anchor_html) in enumerate(anchors):
        class_value = first_match(r"class=(['\"])([\s\S]*?)\1", anchor_html, 2) or ""
        if not re.search(r"\bresult-link\b", class_value, re.IGNORECASE):
            continue

        raw_href = first_match(r"href=(['\"])([\s\S]*?)\1", anchor_html, 2) or ""
        title = decode_html(
            strip_tags(first_match(r"<a\b[^>]*>([\s\S]*?)</a>", anchor_html, 1) or "")
        )

        next_position = anchors[index + 1][0] if index + 1 < len(anchors) else len(html_text)
        block = html_text[position:next_position]

        snippet = decode_html(
            strip_tags(
                first_match(
                    r"<td[^>]*class=(['\"])[^'\"]*\bresult-snippet\b[^'\"]*\1[^>]*>\s*([\s\S]*?)\s*</td>",
                    block,
                    2,
                )
                or ""
            )
        )
        link = normalize_duckduckgo_link(raw_href)
        if not title or not link:
            continue
        results.append(SearchResult(title=title, url=link, snippet=snippet))

    return results


def parse_sogou_search(html_text: str) -> list[SearchResult]:
    """解析搜狗搜索结果页。"""
    results: list[SearchResult] = []
    headings = [
        (match.start(), match.group(0))
        for match in re.finditer(r"<h3\b[^>]*>\s*([\s\S]*?)</h3>", html_text, re.IGNORECASE)
    ]

    for index, (position, h3_html) in enumerate(headings):
        raw_href = decode_html(
            first_match(r"href=(['\"])([\s\S]*?)\1", h3_html, 2) or ""
        )
        title = decode_html(
            strip_tags(first_match(r"<a\b[^>]*>([\s\S]*?)</a>", h3_html, 1) or "")
        )
        link = normalize_sogou_link(raw_href)
        if not title or not link:
            continue

        next_position = headings[index + 1][0] if index + 1 < len(headings) else len(html_text)
        block = html_text[position:next_position]
        snippet = decode_html(
            strip_tags(
                first_match(
                    r"<(div|p)\b[^>]*class=(['\"])[^'\"]*(fz-mid|str-text-info|text-layout|space-txt)[^'\"]*\2[^>]*>([\s\S]*?)</\1>",
                    block,
                    4,
                )
                or ""
            )
        )
        results.append(SearchResult(title=title, url=link, snippet=snippet))

    return results


def parse_search_results(provider: str, html_text: str) -> list[SearchResult]:
    if provider == "duckduckgo-lite":
        return parse_duckduckgo_lite(html_text)
    return parse_sogou_search(html_text)


def fetch_search_page(provider: str, query: str, *, max_retries: int = DEFAULT_MAX_RETRIES) -> HttpResponse:
    """按后端发起搜索请求。"""
    if provider == "duckduckgo-lite":
        url = f"{_DDG_LITE_ENDPOINT}?{urllib.parse.urlencode({'q': query})}"
        return fetch_with_retry(
            url,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
            },
            max_retries=max_retries,
        )
    if provider == "sogou":
        url = f"{_SOGOU_ENDPOINT}?{urllib.parse.urlencode({'query': query})}"
        return fetch_with_retry(
            url,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.6",
            },
            max_retries=max_retries,
        )
    raise WebError(f"unsupported search provider: {provider}")


def search_web(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    allowed_domains: Iterable[str] | None = None,
    blocked_domains: Iterable[str] | None = None,
) -> tuple[list[SearchResult], str]:
    """多后端搜索。返回 `(结果, 实际使用的后端)`。

    ## 后端依次尝试

    一个后端被限流、改版、或干脆没有结果时，退到下一个。**只有全部失败才报错**
    ——单一后端会让「换个源」变成用户必须手动做的事。

    ## 有结果才算成功

    后端返回 200 但解析出 0 条，不算成功——那是「这个源今天不给我结果」，
    该继续试下一个，而不是把空结果当成答案。
    """
    allowed = normalize_domain_list(allowed_domains)
    blocked = normalize_domain_list(blocked_domains)
    errors: list[str] = []

    for provider in SEARCH_PROVIDERS:
        try:
            response = fetch_search_page(provider, query)
            if not (200 <= response.status < 300):
                errors.append(f"{provider}: HTTP {response.status}")
                continue

            parsed = parse_search_results(provider, response.text())
            organic = [
                item
                for item in parsed
                if passes_domain_filter(item.url, allowed, blocked)
            ][:max_results]

            if organic:
                return organic, provider
            errors.append(f"{provider}: no results")
        except WebError as error:
            errors.append(f"{provider}: {error}")

    if errors:
        # **只要有后端失败就抛。** 原版是 `if (errors.length > 0) throw`。
        #
        # 注意这意味着「两个后端都返回 200 但都没解析出结果」也会被报成
        # 错误（`all search providers failed (... no results)`），而不是
        # 「没搜到」。原版如此，本实现镜像它——**但下面的空返回因此成了
        # 不可达分支**，保留只是为了保持结构完整。
        #
        # 这个取舍值得知道：对模型来说 `ok: false` 与「空结果」触发不同的
        # 后续行为，前者会让它换关键词或换思路。
        raise WebError(f"all search providers failed ({'; '.join(errors)})")
    return [], "fallback-empty"


def search_duckduckgo(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> list[SearchResult]:
    """兼容旧调用点：只要当前可用后端的结果。"""
    results, _provider = search_web(query, max_results)
    return results


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def decode_body(raw: bytes, content_type: str) -> str:
    """按 `content-type` 里的 charset 解码，缺省 UTF-8。"""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def http_get(
    url: str, *, timeout: float | None = None, max_retries: int = DEFAULT_MAX_RETRIES
) -> HttpResponse:
    """带重试的 GET。保留这个名字是为了兼容既有调用点。"""
    timeout_ms = (
        DEFAULT_TIMEOUT_MS if timeout is None else int(round(timeout * 1000))
    )
    return fetch_with_retry(url, timeout_ms=timeout_ms, max_retries=max_retries)


def fetch_web_page(
    url: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> WebPage:
    """抓取一个网页并抽出正文。

    **会跟进 HTML 跳转**：很多站点用只含跳转脚本的壳页做中转。不跟进的话
    抓回来的是空壳，用户看到「内容为空」却不知道发生了什么。
    """
    request_headers = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7"
        ),
        "accept-language": "en-US,en;q=0.9",
    }
    response = fetch_with_retry(
        url, headers=request_headers, max_retries=max_retries
    )
    text = response.text()
    content_type = response.content_type
    final_url = response.final_url or url

    if "html" in content_type.lower():
        redirect_url = extract_html_redirect_url(text, final_url)
        if redirect_url and redirect_url != final_url:
            response = fetch_with_retry(
                redirect_url, headers=request_headers, max_retries=max_retries
            )
            text = response.text()
            content_type = response.content_type
            final_url = response.final_url or redirect_url

    is_html = "html" in content_type.lower() or "xml" in content_type.lower()
    if is_html:
        content, _title = extract_readable_text(text)
        title = extract_title(text)
    else:
        # 非 HTML（纯文本、JSON）：原样返回，不必抽正文。
        content, title = text.strip(), None

    if max_chars and len(content) > max_chars:
        content = content[:max_chars]

    return WebPage(
        url=url,
        final_url=final_url,
        status=response.status,
        status_text=response.status_text,
        content_type=content_type or "unknown",
        content=content,
        title=title,
    )
