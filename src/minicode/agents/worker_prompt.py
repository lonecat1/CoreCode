"""worker 的系统提示。

要点是**把只读这件事说清楚**，并且明确告诉它「不要说改了代码」——
因为 worker 手里确实没有写工具，但如果提示不说明，模型会倾向于
声称自己完成了修改（它从训练里知道编码助手通常会改文件）。

最后一段是让它交回一份**根 Agent 能直接用**的报告：路径、行号、结论、建议下一步。
"""

from __future__ import annotations

WORKER_PROMPT_LINES = [
    "You are a read-only sub-agent helping the root coding agent.",
    "Investigate only the task assigned in the user message.",
    "Use search and read tools to gather concrete evidence.",
    "You cannot modify files, run commands, ask the user questions, or create other agents.",
    "Return a concise report with relevant file paths, line references, findings, and a "
    "recommended next step for the root agent.",
    "Do not claim to have changed code. The root agent is the only agent allowed to make edits.",
]


def build_subagent_prompt(cwd: str, memory_text: str = "") -> str:
    """构造 worker 的系统提示。

    `memory_text` 由调用方（宿主）预先加载的项目指令文本；为空则跳过。
    这样 `agents` 模块不必依赖 `memory` 模块，保持边界干净。
    """
    parts = [
        f"You are a read-only sub-agent helping the root coding agent.\nCurrent cwd: {cwd}",
        *WORKER_PROMPT_LINES[1:],
    ]
    if memory_text:
        parts.append(memory_text)
    return "\n\n".join(parts)
