"""轻量 Web 能力：抓取网页并抽正文，以及一个搜索后端。

只用标准库的 `urllib` 与 `html.parser`，不引入 requests / bs4。
HTML→文本刻意做得**保守**：宁可少抓，也不要把脚本、样式、导航当正文塞给模型。

搜索走 DuckDuckGo 的 html 端点（无需 API key）。这条路径依赖对方页面结构，
因此解析失败时返回**明确的空结果**而不是抛异常——搜索不可用不应该让整个回合失败。
"""

from __future__ import annotations

import html as html_module
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (compatible; MiniCode/0.1; +https://github.com/LiuMengxuan04/MiniCode)"
)
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_CHARS = 12_000

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
class WebPage:
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
# HTML → 文本
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """把 HTML 抽成可读文本。"""

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
        # 折叠空白：逐行 strip，丢掉空行，再合并连续空行。
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
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不该让抓取失败
        pass
    return parser.text(), parser.title()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _decode(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str, str, str, bytes]:
    """发起 GET，返回 `(status, status_text, content_type, final_url, body)`。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return (
                int(response.status),
                str(getattr(response, "reason", "") or ""),
                str(response.headers.get("content-type") or ""),
                str(response.geturl()),
                body,
            )
    except urllib.error.HTTPError as error:
        raw = b""
        try:
            raw = error.read()
        except Exception:  # noqa: BLE001
            pass
        return (
            int(error.code),
            str(error.reason or ""),
            str(error.headers.get("content-type") if error.headers else ""),
            url,
            raw,
        )
    except urllib.error.URLError as error:
        raise WebError(f"网络错误：{error.reason}") from error
    except OSError as error:
        raise WebError(f"请求失败：{error}") from error


def fetch_web_page(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> WebPage:
    """抓取一个网页并抽出正文。"""
    status, status_text, content_type, final_url, body = http_get(url)
    text = _decode(body, content_type)

    lowered = content_type.lower()
    if "html" in lowered or "xml" in lowered or not lowered:
        content, title = extract_readable_text(text)
    else:
        # 非 HTML（纯文本、JSON）：原样返回，不必抽正文。
        content, title = text.strip(), None

    if max_chars and len(content) > max_chars:
        content = content[:max_chars] + "\n…（内容已截断）"

    return WebPage(
        final_url=final_url,
        status=status,
        status_text=status_text,
        content_type=content_type or "unknown",
        content=content,
        title=title,
    )


# ---------------------------------------------------------------------------
# 搜索（DuckDuckGo html 端点，无需 API key）
# ---------------------------------------------------------------------------

_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_tags(value: str) -> str:
    return html_module.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _normalize_ddg_link(href: str) -> str:
    """DuckDuckGo 会把结果包在 /l/?uddg=<encoded> 里，剥掉外层。"""
    if href.startswith("//"):
        href = f"https:{href}"
    if "duckduckgo.com/l/" in href:
        parsed = urllib.parse.urlparse(href)
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def search_duckduckgo(query: str, max_results: int = 8) -> list[SearchResult]:
    """搜索并返回结果列表。解析不到就返回空列表（不抛异常）。"""
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    request = urllib.request.Request(
        _DDG_ENDPOINT,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "content-type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            body = _decode(response.read(), str(response.headers.get("content-type") or ""))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
        raise WebError(f"搜索请求失败：{error}") from error

    snippets = [_strip_tags(match) for match in _SNIPPET_RE.findall(body)]
    results: list[SearchResult] = []
    for index, (href, title_html) in enumerate(_RESULT_LINK_RE.findall(body)):
        if len(results) >= max_results:
            break
        title = _strip_tags(title_html)
        url = _normalize_ddg_link(html_module.unescape(href))
        if not title or not url:
            continue
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippets[index] if index < len(snippets) else "",
            )
        )
    return results
