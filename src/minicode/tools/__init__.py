"""内置工具集合与默认注册表。

两个注册表构造器体现了「根 Agent 与子 Agent 能力不同」这条结构边界：

    create_root_registry()      根：全部工具 + 子 Agent 管理工具
    create_worker_registry()    子 Agent：只读子集（**看不到任何写工具**）

隔离发生在**注册表层**，不是靠每个工具的自觉——worker 根本看不到
`write_file`，因此不存在「忘了检查权限」的可能。
"""

from __future__ import annotations

import os

from ..agents.manager import SubAgentManager
from ..agents.types import SUBAGENT_TOOL_NAMES as _AGENT_TOOL_NAMES
from ..tooling import ToolRegistry
from ..types import ChatMessage
from .ask_user import ask_user_tool
from .files import list_files_tool, read_file_tool, write_file_tool
from .grep_files import grep_files_tool
from .run_command import run_command_tool
from .web import web_fetch_tool, web_search_tool

__all__ = [
    "SUBAGENT_TOOL_NAMES",
    "ask_user_tool",
    "create_default_registry",
    "create_root_registry",
    "create_worker_registry",
    "grep_files_tool",
    "list_files_tool",
    "read_file_tool",
    "run_command_tool",
    "subagent_registry",
    "web_fetch_tool",
    "web_search_tool",
    "write_file_tool",
]
#: 子 Agent 能拿到的工具名白名单。只读——根 Agent 独占一切写操作。
#: 权威定义在 `agents/types.py`，这里只是转出，避免两处不一致。
SUBAGENT_TOOL_NAMES = _AGENT_TOOL_NAMES


def create_default_registry(cwd: str | None = None) -> ToolRegistry:
    """不含子 Agent 管理能力的根工具集。

    保留它是为了测试与「单 Agent 模式」——此时模型看不到 `spawn_agent`，
    也就不会尝试委派。

    **`load_skill` 始终注册**，即使 `cwd` 为 None。原因：子 Agent 的只读白名单
    里有 `load_skill`，而白名单是在注册表层裁剪的——工具不在表里，worker 就会
    拿到「Unknown tool」。工具的存在性不该依赖调用方是否传了 cwd。
    `cwd` 只影响**技能发现**（枚举出哪些技能）与 `load_skill` 的查找目录。
    """
    from .load_skill import create_load_skill_tool

    tools = [
        ask_user_tool,
        list_files_tool,
        grep_files_tool,
        read_file_tool,
        write_file_tool,
        run_command_tool,
        web_fetch_tool,
        web_search_tool,
        create_load_skill_tool(cwd or os.getcwd()),
    ]

    skills: list[dict[str, object]] = []
    if cwd is not None:
        try:
            from ..skills import discover_skills

            skills = [
                {
                    "name": item.name,
                    "description": item.description,
                    "path": item.path,
                    "source": item.source,
                }
                for item in discover_skills(cwd)
            ]
        except Exception:  # noqa: BLE001 - 技能发现失败不该让工具表建不起来
            skills = []

    registry = ToolRegistry(tools)
    registry.set_skills(skills)
    return registry


def create_root_registry(
    subagents: SubAgentManager | None = None, cwd: str | None = None
) -> ToolRegistry:
    """根 Agent 的完整工具集。

    传入 `subagents` 时额外叠加四个管理工具；不传则退化为 `create_default_registry()`
    的行为。这样「有没有子 Agent 能力」是构造期决定的，不需要运行时开关。
    """
    registry = create_default_registry(cwd)
    if subagents is not None:
        from .subagents import create_subagent_tools

        registry.add_tools(create_subagent_tools(subagents))
    return registry


def create_worker_registry(registry: ToolRegistry) -> ToolRegistry:
    """从根注册表裁出子 Agent 的只读子集。"""
    return registry.subset(SUBAGENT_TOOL_NAMES)


#: 旧名保留，避免已有调用点失效。
subagent_registry = create_worker_registry
