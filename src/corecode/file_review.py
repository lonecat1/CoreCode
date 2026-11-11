"""写前审查。

所有会改文件的工具都必须经过这里。它做四件事，顺序不能变：

1. 读旧内容（不存在则视为空）；
2. 内容没变化就**不写盘、不请求批准**，直接返回「无需修改」；
3. 生成 unified diff 交给权限层，由用户看过 diff 再决定是否落盘；
4. 落盘（顺带补齐父目录）。

把这一步收敛成单个函数，是「模型不能悄悄改文件」这条保证的实现方式。

## 换行必须按字节处理（`newline=""`）

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

class FileReviewError(Exception):
    """写前审查无法继续（例如目标不是 UTF-8 文本）。

    抛给调用方而不是返回 `ToolResult.failure`：这里是**底层读函数**，
    它不该知道「工具失败」长什么样。工具注册表会把任何异常变成模型可读的
    失败文本，所以最终用户看到的仍是一条干净的失败。
    """

def load_existing_file(target_path: str) -> str:
    try:
        with open(target_path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        # 其余错误一律上抛）。
        #
        # 这一点很重要：目录、二进制文件、权限不足都**不该**被当成「空文件」——
        # 那样 `modify_file` 会以为自己在「创建新文件」，实际上是在覆盖一个
        # 已存在但读不出来的东西。宁可让工具失败，也不要静默替换。
        return ""
    except UnicodeDecodeError as error:
        # **非 UTF-8 内容不当作空文件，而是明确失败。**
        #
        # **不抛错**，而是替换成 U+FFFD——于是 `edit_file` 会拿着替换后的
        # 文本去比较、去写回，把用户的文件**真的改坏**。
        #
        # 本实现选择失败而不是有损读取（见 D45）。裸的 `UnicodeDecodeError`
        # 也可以交给上层兜住（工具注册表确实会兜），但它的消息是
        # `'utf-8' codec can't decode byte 0xff in position 0: ...`，
        # 对用户毫无指导意义。这里换成一句能照着做的话。
        raise FileReviewError(
            f"{target_path} 不是 UTF-8 文本（{error.reason}），"
            "为避免写坏文件，本次修改已取消。"
        ) from error

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
    # 少了它，Windows 上每次编辑都会把 LF 文件改写成 CRLF。
    with open(target_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(next_content)

    return ToolResult.success(f"已应用审查通过的变更到 {file_path}")
