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
    DEFAULT_MAX_RESULTS,
    WebError,
    fetch_web_page,
    search_web,
)

MIN_MAX_CHARS = 500

MAX_MAX_RESULTS = 20

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
        return ToolResult.failure(f"HTTP {page.status} {page.status_text}: {url}")

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
        "Fetch a web page and extract its readable text content. "
        "Use this after web_search when you need the full content of a specific page."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP or HTTPS URL to fetch."},
            "max_chars": {
                "type": "number",
                "description": (
                    "Maximum number of characters to return from the page content. "
                    f"Defaults to {DEFAULT_MAX_CHARS}."
                ),
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

def _validate_domain_list(data: dict[str, Any], key: str) -> list[str]:
    raw = data.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{key} 必须是字符串数组")
    values: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key}[{index}] 必须是非空字符串")
        values.append(item.strip())
    return values

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
        if max_results < 1 or max_results > MAX_MAX_RESULTS:
            raise ValueError(
                f"max_results 必须在 1 到 {MAX_MAX_RESULTS} 之间"
            )
        result["max_results"] = max_results

    allowed = _validate_domain_list(data, "allowed_domains")
    blocked = _validate_domain_list(data, "blocked_domains")

    # **两者不能同时给。** 一个限定范围、一个排除其中一部分，同时给时语义
    # superRefine 挡住，这里等价地挡住——宁可明确报错，也不要猜用户想要什么。
    if allowed and blocked:
        raise ValueError(
            "Cannot specify both allowed_domains and blocked_domains in one request."
        )

    if allowed:
        result["allowed_domains"] = allowed
    if blocked:
        result["blocked_domains"] = blocked
    return result

def _run_web_search(data: Any, context: ToolContext) -> ToolResult:
    query = data["query"]
    limit = data.get("max_results", DEFAULT_MAX_RESULTS)
    try:
        results, _provider = search_web(
            query,
            limit,
            allowed_domains=data.get("allowed_domains"),
            blocked_domains=data.get("blocked_domains"),
        )
    except WebError as error:
        return ToolResult.failure(f"Web search failed: {error}")

    if not results:
        return ToolResult.success("No results found.")

    lines: list[str] = [f"QUERY: {query}", ""]
    for index, item in enumerate(results, start=1):
        lines.append(f"[{index}] {item.title}")
        lines.append(f"    URL: {item.url}")
        if item.snippet:
            lines.append(f"    {item.snippet}")
        lines.append("")
    return ToolResult.success("\n".join(lines).rstrip())

web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "Search the public web using DuckDuckGo. Use this for current information, "
        "documentation, or anything outside the local workspace."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "number",
                "description": (
                    "Maximum number of results to return. "
                    f"Defaults to {DEFAULT_MAX_RESULTS}."
                ),
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only return results from these domains.",
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exclude results from these domains.",
            },
        },
        "required": ["query"],
    },
    validator=_validate_web_search,
    run=_run_web_search,
)
