"""内置工具集合与默认注册表。

三个注册表构造器体现「根 Agent 与两类子 Agent 能力不同」这条结构边界：

    create_root_registry()      根：全部工具 + 子 Agent 管理工具
    create_worker_registry()    调研/Fork worker：只读子集
    create_coder_registry()     Worktree worker：读写子集，仍看不到 Agent 管理工具

隔离发生在**注册表层**，不是靠每个工具的自觉——worker 根本看不到
`spawn_agent` 或 `ask_user`；worktree worker 即使看到 `write_file`，也受
`WorkerPermissionManager` 的目录与命令约束。
"""

from __future__ import annotations

import os
from typing import Any

from ..agents.manager import SubAgentManager
from ..agents.types import CODER_SUBAGENT_TOOL_NAMES as _CODER_TOOL_NAMES
from ..agents.types import SUBAGENT_TOOL_NAMES as _AGENT_TOOL_NAMES
from ..tooling import ToolRegistry
from ..types import ChatMessage
from .ask_user import ask_user_tool
from .edit import edit_file_tool, modify_file_tool, patch_file_tool
from .files import list_files_tool, read_file_tool, write_file_tool
from .grep_files import grep_files_tool
from .run_command import run_command_tool
from .web import web_fetch_tool, web_search_tool

__all__ = [
    "SUBAGENT_TOOL_NAMES",
    "CODER_SUBAGENT_TOOL_NAMES",
    "ask_user_tool",
    "create_default_registry",
    "create_coder_registry",
    "create_root_registry",
    "create_worker_registry",
    "edit_file_tool",
    "grep_files_tool",
    "list_files_tool",
    "modify_file_tool",
    "patch_file_tool",
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
CODER_SUBAGENT_TOOL_NAMES = _CODER_TOOL_NAMES

def create_default_registry(
    cwd: str | None = None, plan: Any | None = None
) -> ToolRegistry:
    """不含子 Agent 管理能力的根工具集。

    保留它是为了测试与「单 Agent 模式」——此时模型看不到 `spawn_agent`，
    也就不会尝试委派。

    **`load_skill` 始终注册**，即使 `cwd` 为 None。原因：子 Agent 的只读白名单
    里有 `load_skill`，而白名单是在注册表层裁剪的——工具不在表里，worker 就会
    拿到「Unknown tool」。工具的存在性不该依赖调用方是否传了 cwd。
    `cwd` 只影响**技能发现**（枚举出哪些技能）与 `load_skill` 的查找目录。

    ## `plan` 参数为什么是必需的

    `update_plan` 是模型更新 Todo 清单的**唯一**入口，而系统提示每个回合都在
    告诉模型「用 update_plan 跟踪多步工作」。但早先的实现虽然定义了
    `create_update_plan_tool`，却**从来没有调用它**——工具没注册，模型一调
    就得到「Unknown tool: update_plan」。也就是说 Plan 功能实际不可用，
    而提示词还在不断诱导模型去用一个不存在的工具。

    """
    from .goal import create_update_plan_tool
    from .load_skill import create_load_skill_tool

    tools = [
        ask_user_tool,
        list_files_tool,
        grep_files_tool,
        read_file_tool,
        write_file_tool,
        edit_file_tool,
        patch_file_tool,
        modify_file_tool,
        run_command_tool,
        web_fetch_tool,
        web_search_tool,
        create_load_skill_tool(cwd or os.getcwd()),
    ]
    # 不传 plan 的场景（如只读探测、子集裁剪）不该凭空多出一个写工具。
    if plan is not None:
        tools.append(create_update_plan_tool(plan))

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
                    "category": item.category,
                    "subcategory": item.subcategory,
                    "tags": list(item.tags or []),
                    "intents": list(item.intents or []),
                    "when_to_use": item.when_to_use,
                    "when_not_to_use": item.when_not_to_use,
                    "examples": list(item.examples or []),
                    "priority": item.priority,
                    "capabilities": list(item.capabilities or []),
                }
                for item in discover_skills(cwd)
            ]
        except Exception:  # noqa: BLE001 - 技能发现失败不该让工具表建不起来
            skills = []

    registry = ToolRegistry(tools)
    registry.set_skills(skills)
    return registry

def create_root_registry(
    subagents: SubAgentManager | None = None,
    cwd: str | None = None,
    plan: Any | None = None,
) -> ToolRegistry:
    """根 Agent 的完整工具集。

    传入 `subagents` 时额外叠加四个管理工具；不传则退化为 `create_default_registry()`
    的行为。这样「有没有子 Agent 能力」是构造期决定的，不需要运行时开关。

    `plan` 用来注册 `update_plan` —— 见 `create_default_registry` 的说明。
    """
    registry = create_default_registry(cwd, plan)
    if subagents is not None:
        from .subagents import create_subagent_tools

        registry.add_tools(create_subagent_tools(subagents))
    return registry

def create_worker_registry(registry: ToolRegistry) -> ToolRegistry:
    """从根注册表裁出子 Agent 的只读子集。"""
    return registry.subset(SUBAGENT_TOOL_NAMES)

def create_coder_registry(registry: ToolRegistry) -> ToolRegistry:
    """裁出 worktree 编码 worker 的工具子集。"""
    return registry.subset(CODER_SUBAGENT_TOOL_NAMES)

#: 旧名保留，避免已有调用点失效。
subagent_registry = create_worker_registry
