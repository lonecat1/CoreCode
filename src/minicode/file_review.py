"""写前审查。

所有会改文件的工具都必须经过这里。它做四件事，顺序不能变：

1. 读旧内容（不存在则视为空）；
2. 内容没变化就**不写盘、不请求批准**，直接返回「无需修改」；
3. 生成 unified diff 交给权限层，由用户看过 diff 再决定是否落盘；
4. 落盘（顺带补齐父目录）。

把这一步收敛成单个函数，是「模型不能悄悄改文件」这条保证的实现方式。

## 换行必须按字节处理（`newline=""`）

原版用 Node 的 `readFile(path, 'utf8')` 与 `writeFile(path, text, 'utf8')`，
两者都是**字节级**的：读进来是什么、写出去就是什么，不做任何换行转换。

Python 的文本模式默认**不是**这样：

  - 读：universal newlines 会把 `\\r\\n` 归一成 `\\n`；
  - 写：Windows 上会把 `\\n` 转成 `\\r\\n`。

后果很严重：在一个 LF 文件上做一次 `edit_file`，**整个文件会被改写成 CRLF**，
unified diff 显示「每一行都变了」。用户看到的是一个被无端重写的文件。

因此读写都显式传 `newline=""`。这个参数在 Python 里的含义是
「不做任何换行转换」——正是我们要的字节保真。
"""

from __future__ import annotations

import difflib
import os

from .tooling import ToolContext, ToolResult


def load_existing_file(target_path: str) -> str:
    try:
        # `newline=""`：不归一化换行，保持与原版 `readFile` 一致。
        with open(target_path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except IsADirectoryError:
        return ""
    except UnicodeDecodeError:
        # 非 UTF-8 文本（二进制）不该让工具崩掉，交给调用方按空处理。
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
    # `newline=""`：**不做换行转换**，与原版 `writeFile` 的字节级写入一致。
    # 少了它，Windows 上每次编辑都会把 LF 文件改写成 CRLF。
    with open(target_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(next_content)

    return ToolResult.success(f"已应用审查通过的变更到 {file_path}")
