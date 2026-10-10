"""内置文件工具：list_files / read_file / write_file。

这三个工具构成最小可用集合，也正好覆盖三条不同的安全路径：
读取（只读）、列举（只读）、写入（必须过写前审查）。
"""

from __future__ import annotations

import os
from typing import Any

from ..file_review import apply_reviewed_file_change
from ..tooling import ToolContext, ToolDefinition, ToolResult
from ..workspace import resolve_tool_path

#: 目录列举的条目上限，避免把巨型目录塞满上下文。
MAX_LIST_ENTRIES = 400

#: read_file 单次返回的字符预算。
MAX_READ_CHARS = 60_000

#: 默认读取行数。
DEFAULT_READ_LIMIT = 2_000

#: 判断「看起来像二进制」的探测字节数。
_BINARY_SNIFF_BYTES = 8_000


def _require_str(data: Any, key: str, *, allow_empty: bool = False) -> str:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"字段 {key} 必须是字符串")
    if not allow_empty and not value.strip():
        raise ValueError(f"字段 {key} 不能为空")
    return value


def _optional_int(data: Any, key: str) -> int | None:
    value = (data or {}).get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"字段 {key} 必须是整数")
    return value


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def _run_list_files(data: Any, context: ToolContext) -> ToolResult:
    target = resolve_tool_path(context, str(data.get("path", ".") or "."), "list")
    if not os.path.isdir(target):
        return ToolResult.failure(f"不是目录或不存在：{data.get('path', '.')}")

    entries: list[str] = []
    truncated = False
    for name in sorted(os.listdir(target)):
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        full = os.path.join(target, name)
        if os.path.isdir(full):
            entries.append(f"{name}/")
        else:
            try:
                entries.append(f"{name}  ({os.path.getsize(full)} bytes)")
            except OSError:
                entries.append(name)

    if not entries:
        body = "(空目录)"
    else:
        body = "\n".join(entries)
    if truncated:
        body += f"\n… 已截断（上限 {MAX_LIST_ENTRIES} 条）"
    return ToolResult.success(body)


list_files_tool = ToolDefinition(
    name="list_files",
    description="列出工作目录下某个目录的条目。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的路径，默认 ."}
        },
    },
    validator=lambda data: {"path": str((data or {}).get("path", ".") or ".")},
    run=_run_list_files,
)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def _run_read_file(data: Any, context: ToolContext) -> ToolResult:
    path = _require_str(data, "path")
    offset = _optional_int(data, "offset") or 0
    limit = _optional_int(data, "limit") or DEFAULT_READ_LIMIT
    target = resolve_tool_path(context, path, "read")

    if not os.path.isfile(target):
        return ToolResult.failure(f"文件不存在：{path}")

    try:
        with open(target, "rb") as handle:
            sniff = handle.read(_BINARY_SNIFF_BYTES)
        if b"\x00" in sniff:
            return ToolResult.failure(f"{path} 看起来是二进制文件，已跳过。")
        with open(target, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as error:
        return ToolResult.failure(f"读取 {path} 失败：{error}")

    total_lines = len(lines)
    selected = lines[offset : offset + limit]
    body = "".join(selected)

    char_truncated = len(body) > MAX_READ_CHARS
    if char_truncated:
        body = body[:MAX_READ_CHARS]

    end_line = offset + len(selected)
    truncated = char_truncated or end_line < total_lines

    header = [
        f"PATH: {path}",
        f"LINES: {total_lines}",
        f"SHOWING: {offset + 1}-{end_line}" if selected else "SHOWING: (none)",
        f"TRUNCATED: {'yes' if truncated else 'no'}",
    ]
    if truncated:
        header.append(f"HINT: 继续读取请使用 offset={end_line}")

    numbered = "".join(
        f"{offset + index + 1}\t{line}" for index, line in enumerate(selected)
    )
    return ToolResult.success("\n".join(header) + "\n\n" + numbered)


def _validate_read_file(data: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"path": _require_str(data, "path")}
    offset = _optional_int(data, "offset")
    limit = _optional_int(data, "limit")
    if offset is not None:
        if offset < 0:
            raise ValueError("offset 不能为负")
        result["offset"] = offset
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit 必须为正")
        result["limit"] = limit
    return result


read_file_tool = ToolDefinition(
    name="read_file",
    description=(
        "读取工作目录下的文本文件。返回带行号的正文与一个头部，"
        "头部的 TRUNCATED 字段说明内容是否被截断；被截断时按 HINT 给出 offset 继续读。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "offset": {"type": "integer", "description": "起始行号（从 0 计）"},
            "limit": {"type": "integer", "description": "最多读取行数"},
        },
        "required": ["path"],
    },
    validator=_validate_read_file,
    run=_run_read_file,
)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def _run_write_file(data: Any, context: ToolContext) -> ToolResult:
    path = _require_str(data, "path")
    content = str((data or {}).get("content", ""))
    target = resolve_tool_path(context, path, "write")
    return apply_reviewed_file_change(context, path, target, content)


def _validate_write_file(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    if "content" not in data:
        raise ValueError("缺少字段 content")
    content = data.get("content")
    if not isinstance(content, str):
        raise ValueError("字段 content 必须是字符串")
    return {"path": _require_str(data, "path"), "content": content}


write_file_tool = ToolDefinition(
    name="write_file",
    description="写入一个 UTF-8 文本文件（相对工作目录）。写入前会展示 diff 请求批准。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    validator=_validate_write_file,
    run=_run_write_file,
)
