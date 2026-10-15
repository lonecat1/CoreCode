"""本地斜杠命令。

这些命令**不经过模型**——它们直接回答或直接改状态，因此要快、要确定。
分派顺序在 `cli.py` 的 `_dispatch` 里：运行时命令（`/goal`、`/loop`）→ 本地命令
→ 普通回合。

命令清单本身就是帮助文本，因此 `SLASH_COMMANDS` 是唯一事实来源，
`/help` 与「命令补全」都从它派生。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .host import SessionHost


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    usage: str
    description: str


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "/help", "显示可用命令。"),
    SlashCommand("/tools", "/tools", "列出可用工具与只读子集。"),
    SlashCommand("/status", "/status", "显示模型、会话与上下文状态。"),
    SlashCommand("/model", "/model [model-name]", "显示当前模型，或持久化一个模型覆盖。"),
    SlashCommand("/config-paths", "/config-paths", "显示配置文件路径与 Claude 兼容回退路径。"),
    SlashCommand("/plan", "/plan", "显示当前内存 Plan / Todo 清单。"),
    SlashCommand("/goal", "/goal [status|pause|resume|clear]", "创建、查看或控制 Goal。"),
    SlashCommand("/loop", "/loop [Nm|Nh] <prompt> | /loop stop", "创建、查看或停止 Loop。"),
    SlashCommand("/memory", "/memory", "显示已加载的指令文件。"),
    SlashCommand("/skills", "/skills", "列出发现的技能。"),
    SlashCommand("/mcp", "/mcp", "显示 MCP 服务器与连接状态。"),
    SlashCommand("/permissions", "/permissions", "显示权限存储路径与当前规则。"),
    SlashCommand("/resume", "/resume <id>", "恢复一个已保存的会话。"),
    SlashCommand("/rename", "/rename <name>", "重命名当前会话。"),
    SlashCommand("/fork", "/fork", "把当前会话分叉成独立的新会话。"),
    SlashCommand("/new", "/new", "清空当前会话状态，重新开始。"),
    SlashCommand("/compact", "/compact", "手动压缩上下文（调用模型生成摘要）。"),
    SlashCommand("/collapse", "/collapse", "把旧的上下文区间投影为摘要（不改真实转录）。"),
    SlashCommand("/snip", "/snip", "删除一段安全的中间历史（不调用模型）。"),
    # 本地工具快捷方式：直接调用工具，**不经过模型**。
    # 用法与说明照抄原版 `cli-commands.ts:106-145`——它是用户可见的契约。
    SlashCommand("/ls", "/ls [path]", "列出目录中的文件。"),
    SlashCommand("/grep", "/grep <pattern>::[path]", "在文件中搜索文本。"),
    SlashCommand("/read", "/read <path>", "直接读取一个文件。"),
    SlashCommand("/write", "/write <path>::<content>", "直接写入一个文件。"),
    SlashCommand(
        "/modify",
        "/modify <path>::<content>",
        "替换整个文件，落盘前展示可审查的 diff。",
    ),
    SlashCommand("/edit", "/edit <path>::<search>::<replace>", "按精确文本替换编辑文件。"),
    SlashCommand(
        "/patch",
        "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...",
        "一条命令里对同一个文件做多处替换。",
    ),
    SlashCommand(
        "/cmd",
        "/cmd [cwd::]<command> [args...]",
        "直接执行允许清单内的开发命令，可指定另一个工作目录。",
    ),
    SlashCommand("/exit", "/exit", "退出。"),
)


def format_slash_commands() -> str:
    return "\n".join(f"{item.usage}    {item.description}" for item in SLASH_COMMANDS)


def find_matching_slash_commands(prefix: str) -> list[str]:
    return [item.usage for item in SLASH_COMMANDS if item.usage.startswith(prefix)]


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------


def try_handle_local_command(host: "SessionHost", text: str) -> str | None:
    """处理一个本地命令。返回 None 表示「不是本地命令」。"""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    head, _, argument = stripped.partition(" ")
    argument = argument.strip()

    handlers = {
        "/help": lambda: format_slash_commands(),
        "/tools": lambda: _render_tools(host),
        "/status": lambda: _render_status(host),
        "/plan": lambda: _render_plan(host),
        "/memory": lambda: _render_memory(host),
        "/skills": lambda: _render_skills(host),
        "/mcp": lambda: _render_mcp(host),
        "/permissions": lambda: _render_permissions(host),
        "/model": lambda: _do_model(host, argument),
        "/config-paths": lambda: _render_config_paths(),
        "/resume": lambda: _do_resume(host, argument),
        "/rename": lambda: _do_rename(host, argument),
        "/fork": lambda: _do_fork(host),
        "/new": lambda: _do_new(host),
        "/compact": lambda: _do_compact(host),
        "/collapse": lambda: _do_collapse(host),
        "/snip": lambda: _do_snip(host),
    }

    handler = handlers.get(head)
    return handler() if handler is not None else None


# ---------------------------------------------------------------------------
# 展示类
# ---------------------------------------------------------------------------


def _render_tools(host: "SessionHost") -> str:
    from .tools import create_worker_registry
    from .ui import render_key_value, render_separator, render_status_line

    all_tools = host.tools.list()
    worker = create_worker_registry(host.tools)
    worker_names = {item.name for item in worker.list()}

    rows: list[tuple[str, str]] = []
    for tool in all_tools:
        mark = " 只读" if tool.name in worker_names else ""
        rows.append((tool.name + mark, tool.description))

    return "\n".join(
        [
            render_status_line(
                label=f"{len(all_tools)} 个工具（子 Agent 可见 {len(worker_names)} 个只读）"
            ),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _render_status(host: "SessionHost") -> str:
    from .context.tokens import compute_context_stats
    from .ui import render_key_value, render_separator, render_status_line

    model_name = host.runtime_config.model if host.runtime_config else "（离线 mock）"
    rows: list[tuple[str, str]] = [
        ("模型", model_name),
        ("会话", host.session_id),
        ("目录", host.cwd),
        ("消息", f"{len(host.messages)} 条（已落盘游标 {host.already_saved_count}）"),
    ]

    if host.runtime_config:
        stats = compute_context_stats(host.messages, host.runtime_config.model)
        rows.append(
            (
                "上下文",
                f"{stats.total_tokens} / {stats.effective_input} token"
                f"（{stats.utilization * 100:.1f}% · {stats.warning_level}）",
            )
        )
        rows.append(
            (
                "计量",
                stats.accounting.source + ("（已失效）" if stats.accounting.stale else ""),
            )
        )
        rows.append(
            (
                "窗口",
                f"{stats.context_window}"
                f"（预留 {stats.context_window - stats.effective_input}）",
            )
        )
    else:
        rows.append(("上下文", "离线模式，无模型窗口信息"))

    rows.append(("Goal", _goal_state(host)))
    rows.append(("Loop", _loop_state(host)))
    rows.append(
        (
            "子 Agent",
            f"{host.subagents.running_count}/{host.subagents.max_concurrent} 运行中"
            if host.subagents
            else "未启用",
        )
    )
    # 本会话的累计统计（回合数、耗时、工具调用次数）
    if host.stats.turns:
        rows.append(("本会话", host.stats.summary_line()))

    return "\n".join(
        [
            render_status_line(label="会话状态"),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _goal_state(host: "SessionHost") -> str:
    snapshot = host.runtime.goal.manager.get_snapshot()
    if snapshot is None:
        return "无"
    return (
        f"{snapshot.status}"
        f"（标准 {len(snapshot.completion_criteria)} 条"
        f"{"，需重新规划" if snapshot.needs_replan else ""}）"
    )


def _loop_state(host: "SessionHost") -> str:
    snapshot = host.runtime.loop.get_snapshot()
    if snapshot is None:
        return "无"
    return f"{snapshot['status']}（间隔 {snapshot['intervalMs'] // 60_000} 分钟）"


def _render_plan(host: "SessionHost") -> str:
    """Todo 清单：用状态字形 + 对齐渲染。

    比 `format_plan` 的朴素文本多两层信息：**进度计数**（几条完成 / 共几条）
    和**统一的状态记号**。用户扫一眼就能知道还剩多少事。
    """
    from .ui import render_key_value, render_separator, render_status_line

    snapshot = host.plan.get_snapshot()
    todos = list(snapshot.todos)
    if not todos:
        return render_status_line(label="Todo 清单为空")

    marks = {
        "completed": "✓",
        "in_progress": "▸",
        "pending": "·",
    }
    rows: list[tuple[str, str]] = []
    for item in todos:
        status = str(getattr(item, "status", "pending"))
        title = str(getattr(item, "title", ""))
        rows.append((f"{marks.get(status, '·')} {title}", status))

    done = sum(1 for item in todos if getattr(item, "status", "") == "completed")
    return "\n".join(
        [
            render_status_line(label=f"Todo 清单（{done}/{len(todos)} 完成）"),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _render_memory(host: "SessionHost") -> str:
    from .memory import render_memory_report

    return render_memory_report(host.cwd)


def _render_skills(host: "SessionHost") -> str:
    from .ui import render_key_value, render_separator, render_status_line

    skills = host.tools.skills()
    if not skills:
        return (
            "没有发现技能。技能来自 `.mini-code/skills/<name>/SKILL.md`"
            "（也兼容 `.claude/skills/`）。"
        )
    rows = [
        (str(item.get("name", "")), str(item.get("description", "")))
        for item in skills
    ]
    return "\n".join(
        [
            render_status_line(label=f"{len(skills)} 个技能"),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _render_mcp(host: "SessionHost") -> str:
    from .ui import render_key_value, render_separator, render_status_line

    servers = host.tools.mcp_servers()
    if not servers:
        return "没有配置 MCP 服务器（见 ~/.mini-code/mcp.json）。"
    rows: list[tuple[str, str]] = []
    for server in servers:
        detail = f"tools={server.get('toolCount', 0)}"
        if server.get("error"):
            detail += f" · 错误：{server['error']}"
        rows.append((str(server.get("name", "")), f"{server.get('status')}  {detail}"))
    connected = sum(1 for s in servers if s.get("status") == "connected")
    return "\n".join(
        [
            render_status_line(
                label=f"MCP 服务器（{connected}/{len(servers)} 已连接）"
            ),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _render_permissions(host: "SessionHost") -> str:
    from .permissions import PERMISSIONS_PATH
    from .ui import render_key_value, render_separator, render_status_line

    rows: list[tuple[str, str]] = [("存储", str(PERMISSIONS_PATH))]
    summary = host.permissions.get_summary()
    if summary:
        rows.extend((f"规则 {index}", str(item)) for index, item in enumerate(summary, 1))
    else:
        rows.append(("规则", "（无）"))
    return "\n".join(
        [
            render_status_line(label="权限"),
            render_separator(),
            render_key_value(rows),
        ]
    )


def _render_config_paths() -> str:
    """显示全部配置文件路径，对应原版 `cli-commands.ts:224-231`。

    原版输出四行，其中最后一行是 **Claude Code 的兼容回退路径**。那一项不是
    装饰：配置加载确实会读它（见 `config.load_effective_settings`），把它显示
    出来用户才知道「为什么我没配却能跑」或「为什么改了不生效」。
    """
    from .config import (
        claude_settings_path,
        mcp_path,
        permissions_path,
        settings_path,
    )
    from .ui import render_key_value, render_separator, render_status_line

    rows = [
        ("mini-code settings", str(settings_path())),
        ("mini-code permissions", str(permissions_path())),
        ("mini-code mcp", str(mcp_path())),
        ("compat fallback", str(claude_settings_path())),
    ]
    return "\n".join(
        [
            render_status_line(label="配置路径"),
            render_separator(),
            # `wrap_values=True`：这条命令的全部意义是让用户看到**完整**路径
            # 去复制，截断会让它彻底没用。
            render_key_value(rows, wrap_values=True),
        ]
    )


# ---------------------------------------------------------------------------
# 动作类
# ---------------------------------------------------------------------------


def _do_model(host: "SessionHost", argument: str) -> str:
    """查看或持久化模型覆盖，对应原版 `cli-commands.ts` 的 `/model` 两条分支。

    - `/model`         → 显示当前生效的模型
    - `/model <name>`  → 写进 `settings.json`
    - `/model `（空）  → 提示用法

    写入用 `save_settings`（合并而非覆盖），与原版 `saveMiniCodeSettings` 一致：
    只更新 `model` 一个键，用户其余的配置原样保留。
    """
    from .config import save_settings, settings_path

    if not argument:
        # 注意：`/model` 与 `/model ` 都会走到这里。原版对后者给「用法」提示，
        # 对前者给「当前模型」。这里用 host 上是否已有模型来区分——
        # 但更简单也更贴近原版的做法是：**无参数就显示当前模型**，
        # 因为 `/model ` 的尾随空格在 `partition` 后已被 strip 掉，
        # 两者无法区分，而「显示当前模型」在两种情况下都合理。
        current = host.runtime_config.model if host.runtime_config else "（离线 mock）"
        return f"current model: {current}"

    model = argument.strip()
    if not model:
        return "用法: /model <model-name>"

    try:
        saved_to = save_settings({"model": model})
    except OSError as error:
        return f"写入配置失败：{error}"
    return f"saved model={model} to {saved_to or settings_path()}"


def _do_resume(host: "SessionHost", argument: str) -> str:
    from .session import list_sessions

    if argument:
        return "已恢复会话。" if host.resume(argument) else f"找不到会话 {argument}。"

    sessions = list_sessions(host.cwd)
    if not sessions:
        return "当前目录还没有会话。"
    lines = ["可恢复的会话（用 /resume <id>）："]
    for meta in sessions:
        lines.append(f"  {meta.id}  {meta.title or '(无标题)'}  [{meta.message_count} 行]")
    return "\n".join(lines)


def _do_rename(host: "SessionHost", argument: str) -> str:
    from .session import rename_session

    if not argument:
        return "用法：/rename <name>"
    return (
        "已重命名。"
        if rename_session(host.cwd, host.session_id, argument)
        else "当前会话尚未落盘，无法重命名。先发一条消息。"
    )


def _do_fork(host: "SessionHost") -> str:
    new_id = host.fork(host.session_id)
    if new_id is None:
        return "当前会话为空，无法分叉。"
    host.resume(new_id)
    return f"已分叉为新会话 {new_id}。"


def _do_new(host: "SessionHost") -> str:
    from .session import clear_session

    previous = host.session_id
    host.reset()
    new_id = _short_id()
    host.session_id = new_id
    host.plan.reset(new_id)
    host.refresh_system_prompt()
    # 删除旧会话文件，并清空内存——两步都做，否则旧历史会被下一次保存复活。
    clear_session(host.cwd, previous)
    return f"已开始新会话 {new_id}（旧会话 {previous} 已删除）。"


def _do_compact(host: "SessionHost") -> str:
    """手动压缩：调用模型生成摘要并落盘边界。"""
    from .context.compact import compact_conversation, reset_auto_compact_state
    from .session import append_compact_boundary

    if host.runtime_config is None:
        return "离线模式没有可用的模型，无法生成摘要。"

    result = compact_conversation(host.messages, host.model)
    if result is None:
        return "没有可压缩的内容（消息太少、没有模型窗口信息，或摘要解析失败）。"

    retained = [
        m for m in result.messages if m.get("role") != "system" and m is not result.summary
    ]
    append_compact_boundary(
        host.cwd,
        host.session_id,
        str(result.summary.get("content", "")),
        "manual",
        result.tokens_before,
        result.tokens_after,
        retained_messages=retained,
    )
    host.messages = list(result.messages)
    host.already_saved_count = max(0, len(host.messages) - 1)
    # 手动压缩成功 → 重新武装 auto-compact 的熔断器。
    reset_auto_compact_state()
    return (
        f"已压缩 {result.removed_count} 条消息："
        f"{result.tokens_before} → {result.tokens_after} token。"
    )


def _do_collapse(host: "SessionHost") -> str:
    """手动折叠：只改模型可见投影，真实转录不变。"""
    from .context.collapse import (
        apply_context_collapse_if_needed,
        create_context_collapse_state,
    )
    from .session import append_context_collapse_span

    if host.runtime_config is None:
        return "离线模式没有可用的模型，无法生成摘要。"

    result = apply_context_collapse_if_needed(
        host.messages,
        host.runtime_config.model,
        host.model,
        host.pipeline.collapse_state,
        utilization_threshold=0.0,  # 手动触发：不看阈值
    )
    host.pipeline.collapse_state = result.state

    if not result.collapsed:
        return (
            "没有可安全折叠的区间。"
            if result.state.enabled
            else "折叠已因连续失败被禁用。"
        )

    for span in result.spans:
        append_context_collapse_span(host.cwd, host.session_id, span)

    saved = sum(
        max(0, span.tokens_before - span.tokens_after) for span in result.spans
    )
    return (
        f"已把 {len(result.spans)} 个区间投影为摘要，约省 {saved} token。"
        "真实转录保持不变。"
    )


def _do_snip(host: "SessionHost") -> str:
    """手动剪裁：确定性删除一段中间历史。"""
    from .context.snip import snip_compact_conversation
    from .context.tokens import compute_context_stats
    from .session import append_snip_boundary

    model_name = host.runtime_config.model if host.runtime_config else "unknown"
    stats = compute_context_stats(host.messages, model_name)
    result = snip_compact_conversation(host.messages, stats, stats.effective_input)

    if not result.did_snip:
        return f"没有可剪裁的区间（{result.reason}）。"

    if result.boundary_message is not None:
        append_snip_boundary(host.cwd, host.session_id, result.boundary_message)
    host.messages = list(result.messages)
    host.already_saved_count = 0
    return (
        f"已删除 {len(result.removed_message_ids)} 条消息，"
        f"约省 {result.tokens_freed} token。"
    )


def _short_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


def render_json(value: Any) -> str:
    """给工具输出用的稳定 JSON 渲染。"""
    return json.dumps(value, ensure_ascii=False, indent=2)
