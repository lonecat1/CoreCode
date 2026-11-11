"""中心化多 Agent 协作的类型与限额。

三种 worker 模式共享同一个中心控制模型：

  - `research`：只读调研，直接在根工作目录读取证据。
  - `fork`：从主 Agent 的对话快照分叉出的只读分析分支。
  - `worktree`：在独立 Git worktree 内编码，改动只能由主 Agent 审查后合入。

`team` 不是第四种执行器，而是给多个 worker 打上同一个 `team_id` 的扁平
分组。成员之间没有消息通道，所有计划、等待、验收和合并仍由主 Agent 完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 并发上限。
MAX_SUB_AGENTS = 3
#: 单个 worker 的最大工具步数。
MAX_SUB_AGENT_STEPS = 20
#: 编码 worker 有更多读写/验证步骤，但仍显著低于无限循环。
MAX_CODER_AGENT_STEPS = 40

MODE_RESEARCH = "research"
MODE_FORK = "fork"
MODE_WORKTREE = "worktree"
AGENT_MODES: tuple[str, ...] = (MODE_RESEARCH, MODE_FORK, MODE_WORKTREE)

ROLE_INVESTIGATOR = "investigator"
ROLE_IMPLEMENTER = "implementer"
ROLE_REVIEWER = "reviewer"
AGENT_ROLES: tuple[str, ...] = (
    ROLE_INVESTIGATOR,
    ROLE_IMPLEMENTER,
    ROLE_REVIEWER,
)

#: 子 Agent 能拿到的工具名白名单 —— 全部只读。
#:
#: 隔离是**在注册表层**完成的（`ToolRegistry.subset`），不是靠每个工具的自觉：
#: 子 Agent 根本看不到 `write_file`，因此不存在「忘了检查权限」的可能。
SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "grep_files",
    "read_file",
    "load_skill",
    "web_fetch",
    "web_search",
)

#: Worktree 编码 worker 的工具白名单。
#:
#: 它拥有写文件、编辑和运行验证命令的能力，但执行目录、路径与命令都受
#: `WorkerPermissionManager` 约束；同时仍看不到 `ask_user` 与 Agent 管理工具。
CODER_SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "grep_files",
    "read_file",
    "write_file",
    "edit_file",
    "patch_file",
    "modify_file",
    "run_command",
    "load_skill",
    "web_fetch",
    "web_search",
)

#: 等某个模块落地后应补进白名单的工具。
#: 放在这里而不是注释里，这样有测试钉住「实现了就该加回来」——
#: `load_skill` 就是随 Skills 模块落地后从这里移到上面去的。
PENDING_SUBAGENT_TOOLS: tuple[str, ...] = ()

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CLOSED = "closed"


@dataclass(slots=True)
class SubAgentSnapshot:
    """一个 worker 的对外可见状态。"""

    id: str
    task: str
    status: str
    started_at: float
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None
    mode: str = MODE_RESEARCH
    role: str = ROLE_INVESTIGATOR
    team_id: str = ""
    team_name: str = ""
    acceptance: str = ""
    worktree_path: str = ""
    base_commit: str = ""
    patch_stat: str = ""
    changed_files: list[str] = field(default_factory=list)
    patch_available: bool = False
    inspected: bool = False
    applied: bool = False
    worktree_removed: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "started_at": self.started_at,
        }
        if self.finished_at is not None:
            data["finished_at"] = self.finished_at
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        data.update(
            {
                "mode": self.mode,
                "role": self.role,
                "team_id": self.team_id,
                "team_name": self.team_name,
                "acceptance": self.acceptance,
                "worktree_path": self.worktree_path,
                "base_commit": self.base_commit,
                "patch_stat": self.patch_stat,
                "changed_files": list(self.changed_files),
                "patch_available": self.patch_available,
                "inspected": self.inspected,
                "applied": self.applied,
                "worktree_removed": self.worktree_removed,
            }
        )
        return data


@dataclass(slots=True)
class WaitResult:
    timed_out: bool
    agents: list[SubAgentSnapshot]
