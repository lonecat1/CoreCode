"""worker 的系统提示。

提示词随协作模式变化，但三条边界不变：

  1. 子 Agent 永远不接管主控制流；
  2. 只交回一份短报告，不把完整对话或内部状态传回根 Agent；
  3. 编码只能发生在隔离 worktree 内，最终合并必须由根 Agent 审查和批准。
"""

from __future__ import annotations

from .types import (
    MODE_FORK,
    MODE_RESEARCH,
    MODE_WORKTREE,
    ROLE_INVESTIGATOR,
)

WORKER_PROMPT_LINES = [
    "You are a read-only sub-agent helping the root coding agent.",
    "Investigate only the task assigned in the user message.",
    "Use search and read tools to gather concrete evidence.",
    "You cannot modify files, run commands, ask the user questions, or create other agents.",
    "Return a concise report with relevant file paths, line references, findings, and a "
    "recommended next step for the root agent.",
    "Do not claim to have changed code. The root agent is the only agent allowed to make edits.",
]

CODER_PROMPT_LINES = [
    "You are a coding sub-agent working inside an isolated Git worktree.",
    "You may edit files and run bounded verification commands only inside this worktree.",
    "Do not commit, push, reset, clean, create worktrees, or operate outside the worktree.",
    "Do not claim the change is merged. The root agent alone inspects, approves, and applies it.",
    "Return a concise report: summary, files changed, verification run, risks, and acceptance checklist.",
]

FORK_PROMPT_LINES = [
    "You are a forked read-only sub-agent. The user messages before the task are an immutable "
    "snapshot of the root conversation at spawn time.",
    "You do not share live state with the root or any other sub-agent.",
    "Investigate the assigned task and return only a concise report with evidence and next steps.",
]


def build_subagent_prompt(
    cwd: str,
    memory_text: str = "",
    *,
    mode: str = MODE_RESEARCH,
    role: str = ROLE_INVESTIGATOR,
    acceptance: str = "",
) -> str:
    """构造 worker 的系统提示。

    `memory_text` 由调用方（宿主）预先加载的项目指令文本；为空则跳过。
    这样 `agents` 模块不必依赖 `memory` 模块，保持边界干净。
    """
    if mode == MODE_WORKTREE:
        lines = [
            f"You are a coding sub-agent working inside an isolated Git worktree.\n"
            f"Current worktree: {cwd}",
            *CODER_PROMPT_LINES,
        ]
    elif mode == MODE_FORK:
        lines = [
            f"You are a forked read-only sub-agent helping the root coding agent.\n"
            f"Current cwd: {cwd}",
            *FORK_PROMPT_LINES,
        ]
    else:
        lines = [
            f"You are a read-only sub-agent helping the root coding agent.\n"
            f"Current cwd: {cwd}",
            *WORKER_PROMPT_LINES[1:],
        ]

    if role:
        lines.append(f"Assigned role: {role}.")
    if acceptance:
        lines.append(f"Acceptance criteria from the root agent:\n{acceptance}")
    if memory_text:
        lines.append(memory_text)
    return "\n\n".join(lines)
