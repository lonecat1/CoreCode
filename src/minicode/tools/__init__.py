"""内置工具集合与默认注册表。"""

from __future__ import annotations

from ..tooling import ToolRegistry
from ..types import ChatMessage
from .ask_user import ask_user_tool
from .files import list_files_tool, read_file_tool, write_file_tool

__all__ = [
    "ask_user_tool",
    "list_files_tool",
    "read_file_tool",
    "write_file_tool",
    "create_default_registry",
    "subagent_registry",
]

#: 子 Agent 能拿到的工具名白名单。只读——根 Agent 独占一切写操作。
SUBAGENT_TOOL_NAMES = ("list_files", "read_file")


def create_default_registry() -> ToolRegistry:
    """根 Agent 的完整工具集。"""
    return ToolRegistry(
        [
            ask_user_tool,
            list_files_tool,
            read_file_tool,
            write_file_tool,
        ]
    )


def subagent_registry(registry: ToolRegistry) -> ToolRegistry:
    """从根注册表裁出子 Agent 的只读子集。

    工具隔离靠 `subset()` 在**注册表层**完成，而不是靠每个工具的自觉——
    因此即使子 Agent 试图调用 `write_file`，它也看不到那个工具。
    """
    return registry.subset(SUBAGENT_TOOL_NAMES)
