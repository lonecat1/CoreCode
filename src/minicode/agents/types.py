"""子 Agent 的类型与限额。

三个数字定义了整个子 Agent 能力的边界，它们不是随手取的：

  - `MAX_SUB_AGENTS = 3`：并发上限。子 Agent 会真花钱、真占 API 配额，
    3 个足够覆盖「并行调研几个方向」，又不会让成本失控。
  - `MAX_SUB_AGENT_STEPS = 20`：单个 worker 的工具步数上限。
    比根 Agent 低得多——worker 只做调研，不该需要几十步。
  - `SUBAGENT_TOOL_NAMES`：只读工具白名单。**根 Agent 独占写权限。**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 并发上限。
MAX_SUB_AGENTS = 3
#: 单个 worker 的最大工具步数。
MAX_SUB_AGENT_STEPS = 20

#: 子 Agent 能拿到的工具名白名单 —— 全部只读。
#:
#: 隔离是**在注册表层**完成的（`ToolRegistry.subset`），不是靠每个工具的自觉：
#: 子 Agent 根本看不到 `write_file`，因此不存在「忘了检查权限」的可能。
#:
#: `load_skill` 暂不在列表里：Skills 模块尚未实现，把不存在的工具列进来会让
#: worker 拿到「Unknown tool」错误。等 Skills 落地后再加回（见 TODO）。
SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "grep_files",
    "read_file",
    "web_fetch",
    "web_search",
)

#: 等 Skills 模块落地后应补进白名单的工具。
#: 放在这里而不是注释里，这样它有测试可以钉住「实现了就该加回来」。
PENDING_SUBAGENT_TOOLS: tuple[str, ...] = ("load_skill",)

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
        return data


@dataclass(slots=True)
class WaitResult:
    timed_out: bool
    agents: list[SubAgentSnapshot]
