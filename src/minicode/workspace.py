"""路径围栏。

工具拿到的路径是**模型给的**，因此不可信。这个模块做两件事：

1. 把相对路径解析到工作目录；
2. 拦住逃出工作区的路径。

注意两种模式的区别，这是有意的：

- **有权限管理器时**（根 Agent）：逃逸**不直接拒绝**，而是交给
  `permissions.ensure_path_access()` 去征询用户。用户可以批准越界访问，
  这是正常的（比如让 agent 改一个相邻仓库的配置）。
- **没有权限管理器时**（子 Agent）：没有征询对象，因此直接拒绝。子 Agent
  的工具是只读子集，让它自由访问工作区之外没有意义。
"""

from __future__ import annotations

import os

from .tooling import ToolContext

PathIntent = str  # "read" | "write" | "list" | "search" | "command_cwd"


class PathEscapesWorkspace(Exception):
    """在没有权限管理器时，路径逃出了工作区。"""


def resolve_tool_path(
    context: ToolContext, target_path: str, intent: PathIntent = "read"
) -> str:
    """解析并（在需要时）校验一个工具路径。"""
    resolved = os.path.abspath(os.path.join(context.cwd, target_path))

    if context.permissions is not None:
        context.permissions.ensure_path_access(resolved, intent)
        return resolved

    if _escapes(context.cwd, resolved):
        raise PathEscapesWorkspace(f"Path escapes workspace: {target_path}")
    return resolved


def _escapes(root: str, target: str) -> bool:
    """`target` 是否越出 `root`——**委托**给 `permissions.is_within_directory`。

    ## 为什么不各写一份

    这里原来是一份手写的反面判断，用 `os.path.relpath`。而 Windows 上
    `relpath` 跨盘符**抛 `ValueError`**，于是「工作目录在 C:、目标在 D:」时
    这条边界检查会**崩**而不是返回「越界」。`permissions.is_within_directory`
    已经处理了那个陷阱；两处各写一遍必然漂移，所以合并成一处。

    依赖方向是安全的：`permissions` 不 import `workspace`（它只依赖
    `abort` 与 `config`），所以这里反向导入不会形成环。
    """
    from .permissions import is_within_directory

    return not is_within_directory(root, target)
