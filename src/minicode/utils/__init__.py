"""内部工具：Web 抓取/搜索、路径与错误辅助等。"""

from .web import (
    DEFAULT_MAX_CHARS,
    SearchResult,
    WebError,
    WebPage,
    extract_readable_text,
    fetch_web_page,
    http_get,
    search_duckduckgo,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "SearchResult",
    "WebError",
    "WebPage",
    "extract_readable_text",
    "fetch_web_page",
    "http_get",
    "search_duckduckgo",
]
