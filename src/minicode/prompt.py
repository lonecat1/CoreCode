"""系统提示组装。

这份提示是**行为契约**，不是文案。它的每一段都在约束模型的某个具体倾向，
改动会直接改变 agent 的表现。几处刻意写下的规则：

  - **「让你做就直接做，不要停在计划」** —— 模型的默认倾向是先给方案再问要不要动手。
  - **澄清必须用 `ask_user` 工具，不能用普通文本** —— 普通文本会终结回合，
    而 `ask_user` 会挂起回合等回答，语义完全不同。
  - **不要替用户决定主观偏好**（颜色、命名、文案语气）—— 这类决定该由人做。
  - **`<progress>` / `<final>` 响应协议** —— 这是 agent loop 区分
    「还在干活」与「交回控制权」的唯一依据。

组合顺序固定：身份与规则 → 权限上下文 → 子 Agent 协调 → 技能 → MCP → 项目记忆。
"""

from __future__ import annotations

from typing import Any

from .memory import load_memory

IDENTITY_AND_RULES = """You are mini-code, a terminal coding assistant.
Default behavior: inspect the repository, use tools, make code changes when appropriate, and explain results clearly.
Prefer reading files, searching code, editing files, and running verification commands over giving purely theoretical advice.
You can inspect or modify paths outside the current cwd when the user asks, but tool permissions may pause for approval first.
When making code changes, keep them minimal, practical, and working-oriented.
If the user clearly asked you to build, modify, optimize, or generate something, do the work instead of stopping at a plan.
If you need user clarification, call the ask_user tool with one concise question and wait for the user reply. Do not ask clarifying questions as plain assistant text.
Do not choose subjective preferences such as colors, visual style, copy tone, or naming unless the user explicitly told you to decide yourself.
When using read_file, pay attention to the header fields. If it says TRUNCATED: yes, continue reading with a larger offset before concluding that the file itself is cut off.
If the user names a skill or clearly asks for a workflow that matches a listed skill, call load_skill before following it.
Structured response protocol:
- When you are still working and will continue with more tool calls, start your text with <progress>.
- Only when the task is actually complete and you are ready to hand control back, start your text with <final>.
- Use ask_user when clarification is required; that tool ends the turn and waits for user input.
- Do not stop after a progress update. After a <progress> message, continue the task in the next step.
- Plain assistant text without <progress> is treated as a completed assistant message for this turn."""

SUBAGENT_SECTION = """Sub-agent coordination:
- You may run at most {max_concurrent} read-only sub-agents concurrently with spawn_agent.
- Delegate only independent investigation tasks. Sub-agents have separate message histories and cannot modify code or spawn more agents.
- You are the root agent and the only agent allowed to edit files or run commands that change the project.
- Use wait_agent to collect reports. Use close_agent when an agent is looping, no longer useful, or should stop consuming model calls.
- Before giving a final answer, collect the reports you need and close any sub-agents that are still running."""

MCP_NAMING_HINT = (
    "Connected MCP tools are already exposed in the tool list with names prefixed "
    "like mcp__server__tool. To discover callable MCP integrations, inspect the tool "
    "list or use /mcp."
)


def build_system_prompt(
    cwd: str,
    permission_summary: list[str] | None = None,
    *,
    skills: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    subagents: dict[str, Any] | None = None,
) -> str:
    """组装 system 提示。各段之间用空行分隔。"""
    parts: list[str] = [IDENTITY_AND_RULES, f"Current cwd: {cwd}"]

    summary = permission_summary or []
    if summary:
        parts.append("Permission context:\n" + "\n".join(summary))

    if subagents:
        parts.append(
            SUBAGENT_SECTION.format(max_concurrent=subagents.get("maxConcurrent", 3))
        )

    skill_list = skills or []
    if skill_list:
        lines = "\n".join(
            f"- {item.get('name')}: {item.get('description')}" for item in skill_list
        )
        parts.append(f"Available skills:\n{lines}")
    else:
        parts.append("Available skills:\n- none discovered")

    servers = mcp_servers or []
    if servers:
        lines = []
        for server in servers:
            suffix = f" ({server.get('error')})" if server.get("error") else ""
            protocol = f", protocol={server['protocol']}" if server.get("protocol") else ""
            resources = (
                f", resources={server['resourceCount']}"
                if server.get("resourceCount") is not None
                else ""
            )
            prompts = (
                f", prompts={server['promptCount']}"
                if server.get("promptCount") is not None
                else ""
            )
            lines.append(
                f"- {server.get('name')}: {server.get('status')}, "
                f"tools={server.get('toolCount', 0)}{resources}{prompts}{protocol}{suffix}"
            )
        parts.append("Configured MCP servers:\n" + "\n".join(lines))

        connected = [s for s in servers if s.get("status") == "connected"]
        if connected:
            hints = [MCP_NAMING_HINT]
            if any((s.get("resourceCount") or 0) > 0 for s in connected):
                hints.append(
                    "Some connected MCP servers also publish resources, so "
                    "list_mcp_resources/read_mcp_resource can be useful for reading "
                    "server-provided content."
                )
            if any((s.get("promptCount") or 0) > 0 for s in connected):
                hints.append(
                    "Some connected MCP servers also publish prompts, so "
                    "list_mcp_prompts/get_mcp_prompt can be useful for fetching "
                    "server-provided prompt templates."
                )
            parts.append(" ".join(hints))

    memory = load_memory(cwd)
    if memory:
        parts.append(memory)

    return "\n\n".join(parts)
