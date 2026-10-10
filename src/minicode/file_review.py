"""写前审查。

所有会改文件的工具都必须经过这里。它做四件事，顺序不能变：

1. 读旧内容（不存在则视为空）；
2. 内容没变化就**不写盘、不请求批准**，直接返回「无需修改」；
3. 生成 unified diff 交给权限层，由用户看过 diff 再决定是否落盘；
4. 落盘（顺带补齐父目录）。

把这一步收敛成单个函数，是「模型不能悄悄改文件」这条保证的实现方式。
"""

from __future__ import annotations

import difflib
import os

from .tooling import ToolContext, ToolResult


def load_existing_file(target_path: str) -> str:
    try:
        with open(target_path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def build_unified_diff(file_path: str, before: str, after: str) -> str:
    """生成 unified diff。前后相同返回一句说明而不是空字符串。"""
    if before == after:
        return f"(no changes for {file_path})"

    normalized_before = before if before.endswith("\n") or not before else before + "\n"
    normalized_after = after if after.endswith("\n") or not after else after + "\n"

    diff_lines = difflib.unified_diff(
        normalized_before.splitlines(keepends=True),
        normalized_after.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3,
    )
    return "".join(diff_lines).rstrip("\n")


def apply_reviewed_file_change(
    context: ToolContext,
    file_path: str,
    target_path: str,
    next_content: str,
) -> ToolResult:
    """审查并落盘一次文件变更。"""
    previous = load_existing_file(target_path)

    if previous == next_content:
        return ToolResult.success(f"文件 {file_path} 无需修改。")

    diff = build_unified_diff(file_path, previous, next_content)

    if context.permissions is not None:
        context.permissions.ensure_edit(target_path, diff)

    if context.signal is not None and getattr(context.signal, "is_set", lambda: False)():
        return ToolResult.failure("变更在落盘前被取消。")

    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        handle.write(next_content)

    return ToolResult.success(f"已应用审查通过的变更到 {file_path}")
