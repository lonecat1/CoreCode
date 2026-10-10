"""子 Agent：隔离上下文 + 受限工具 + 后台并发 + 生命周期控制。

    types.py         限额与快照类型（MAX_SUB_AGENTS / MAX_SUB_AGENT_STEPS）
    worker_prompt.py worker 的只读系统提示
    manager.py       SubAgentManager —— 生命周期、并发上限、取消
"""

from .manager import SubAgentError, SubAgentManager
from .types import (
    MAX_SUB_AGENTS,
    MAX_SUB_AGENT_STEPS,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SUBAGENT_TOOL_NAMES,
    SubAgentSnapshot,
    WaitResult,
)
from .worker_prompt import build_subagent_prompt

__all__ = [
    "MAX_SUB_AGENTS",
    "MAX_SUB_AGENT_STEPS",
    "STATUS_CLOSED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "SUBAGENT_TOOL_NAMES",
    "SubAgentError",
    "SubAgentManager",
    "SubAgentSnapshot",
    "WaitResult",
    "build_subagent_prompt",
]
