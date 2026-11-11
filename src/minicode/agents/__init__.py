"""中心化多 Agent：Fork / Worktree / Team + 受限工具 + 生命周期控制。

    types.py         限额、模式、角色与快照类型
    worker_prompt.py worker 的只读系统提示
    manager.py       SubAgentManager —— 生命周期、并发上限、取消
    worktree.py      Git worktree 隔离与 patch 回收
    worker_permissions.py  worktree worker 的非交互权限策略
"""

from .manager import SubAgentError, SubAgentManager
from .types import (
    AGENT_MODES,
    AGENT_ROLES,
    CODER_SUBAGENT_TOOL_NAMES,
    MAX_SUB_AGENTS,
    MAX_CODER_AGENT_STEPS,
    MAX_SUB_AGENT_STEPS,
    MODE_FORK,
    MODE_RESEARCH,
    MODE_WORKTREE,
    ROLE_IMPLEMENTER,
    ROLE_INVESTIGATOR,
    ROLE_REVIEWER,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SUBAGENT_TOOL_NAMES,
    SubAgentSnapshot,
    WaitResult,
)
from .worker_prompt import build_subagent_prompt
from .worker_permissions import WorkerPermissionManager
from .worktree import GitWorktreeManager, WorktreeChanges, WorktreeError, WorktreeInfo

__all__ = [
    "AGENT_MODES",
    "AGENT_ROLES",
    "CODER_SUBAGENT_TOOL_NAMES",
    "GitWorktreeManager",
    "MAX_SUB_AGENTS",
    "MAX_CODER_AGENT_STEPS",
    "MAX_SUB_AGENT_STEPS",
    "MODE_FORK",
    "MODE_RESEARCH",
    "MODE_WORKTREE",
    "ROLE_IMPLEMENTER",
    "ROLE_INVESTIGATOR",
    "ROLE_REVIEWER",
    "STATUS_CLOSED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "SUBAGENT_TOOL_NAMES",
    "SubAgentError",
    "SubAgentManager",
    "SubAgentSnapshot",
    "WaitResult",
    "WorkerPermissionManager",
    "WorktreeChanges",
    "WorktreeError",
    "WorktreeInfo",
    "build_subagent_prompt",
]
