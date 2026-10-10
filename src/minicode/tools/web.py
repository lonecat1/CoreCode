"""web_fetch / web_search —— 需要外部网络的两个工具。

它们都刻意**把失败变成结果而不是异常**：网络不可用、对方页面改版、
被限流——这些都是常态，不应该让整个回合崩掉。模型拿到错误文本后
可以换个思路，这是更合理的行为。
"""

from __future__ import annotations

from typing import Any

from ..tooling import ToolContext, ToolDefinition, ToolResult
from ..utils.web import (
    DEFAULT_MAX_CHARS,
    WebError,
    fetch_web_page,
    search_duckduckgo,
)

#: `max_chars` 的下限：给太少等于没抓。
MIN_MAX_CHARS = 500

#: 搜索默认返回多少条。
DEFAULT_MAX_RESULTS = 8


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


def _validate_web_fetch(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url 必须是非空字符串")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url 必须以 http:// 或 https:// 开头")

    result: dict[str, Any] = {"url": url}
    max_chars = data.get("max_chars")
    if max_chars is not None:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            raise ValueError("max_chars 必须是整数")
        if max_chars < MIN_MAX_CHARS:
            raise ValueError(f"max_chars 不能小于 {MIN_MAX_CHARS}")
        result["max_chars"] = max_chars
    return result


def _run_web_fetch(data: Any, context: ToolContext) -> ToolResult:
    url = data["url"]
    max_chars = data.get("max_chars", DEFAULT_MAX_CHARS)
    try:
        page = fetch_web_page(url, max_chars)
    except WebError as error:
        return ToolResult.failure(f"Web fetch failed: {error}")

    if page.status >= 400:
        return ToolResult.failure(
            f"HTTP {page.status} {page.status_text}: {url}"
        )

    lines = [
        f"URL: {page.final_url}",
        f"STATUS: {page.status}",
        f"CONTENT_TYPE: {page.content_type}",
    ]
    if page.title:
        lines.append(f"TITLE: {page.title}")
    lines.extend(["", page.content])
    return ToolResult.success("\n".join(lines))


web_fetch_tool = ToolDefinition(
    name="web_fetch",
    description=(
        "抓取一个网页并抽出可读正文。在 web_search 之后用它获取某个页面的完整内容。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的 HTTP/HTTPS 地址"},
            "max_chars": {
                "type": "number",
                "description": f"返回正文字符上限，默认 {DEFAULT_MAX_CHARS}",
            },
        },
        "required": ["url"],
    },
    validator=_validate_web_fetch,
    run=_run_web_fetch,
)


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def _validate_web_search(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 必须是非空字符串")

    result: dict[str, Any] = {"query": query.strip()}
    max_results = data.get("max_results")
    if max_results is not None:
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            raise ValueError("max_results 必须是整数")
        if max_results <= 0:
            raise ValueError("max_results 必须为正")
        result["max_results"] = min(max_results, 20)
    return result


def _run_web_search(data: Any, context: ToolContext) -> ToolResult:
    query = data["query"]
    limit = data.get("max_results", DEFAULT_MAX_RESULTS)
    try:
        results = search_duckduckgo(query, limit)
    except WebError as error:
        return ToolResult.failure(f"Web search failed: {error}")

    if not results:
        return ToolResult.success(
            f"没有找到「{query}」的结果（或搜索后端暂时不可用）。"
        )

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.title}")
        lines.append(f"   {item.url}")
        if item.snippet:
            lines.append(f"   {item.snippet}")
    return ToolResult.success("\n".join(lines))


web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "搜索网络并返回标题、链接与摘要。需要某个页面的完整内容时，"
        "再用 web_fetch 抓取它。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {
                "type": "number",
                "description": f"返回结果条数，默认 {DEFAULT_MAX_RESULTS}",
            },
        },
        "required": ["query"],
    },
    validator=_validate_web_search,
    run=_run_web_search,
)
