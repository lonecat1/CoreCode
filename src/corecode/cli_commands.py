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
    # ## 一条 usage 一个条目，**不要合并**
    #
    # `/help` 与 Tab 补全都直接读这张表，所以合并会同时坏掉两件事：
    #
    #   - `/help` 少列 8 行，用户看不到 `/loop stop`、`/goal pause` 这些子形式；
    #   - 补全错。合并成 `/loop [Nm|Nh] <prompt> | /loop stop` 之后，敲
    #     `/loop stop`。
    #
    SlashCommand("/loop", "/loop", "显示当前内存中的 Loop。"),
    SlashCommand(
        "/loop",
        "/loop [Nm|Nh] <prompt>",
        "按间隔重复一个 prompt；默认 10 分钟，最小 1 分钟。",
    ),
    SlashCommand("/loop", "/loop stop", "停止 Loop，回合进行中或等待批准时也生效。"),
    SlashCommand("/goal", "/goal", "显示当前内存中的 Goal、完成标准、状态与 Plan。"),
    SlashCommand("/goal", "/goal <description>", "创建一个 Goal 并开始自动推进。"),
    SlashCommand("/goal", "/goal status", "显示 Goal 状态。"),
    SlashCommand("/goal", "/goal pause [reason]", "暂停 Goal，回合进行中或等待批准时也生效。"),
    SlashCommand("/goal", "/goal resume", "显式恢复一个已暂停或受阻的 Goal。"),
    SlashCommand("/goal", "/goal clear", "停止并清除 Goal；保留 Plan。"),
    SlashCommand("/plan", "/plan", "显示当前内存中的 Todo 清单。"),
    SlashCommand("/help", "/help", "显示可用命令。"),
    SlashCommand("/tools", "/tools", "列出可用工具与只读子集。"),
    SlashCommand("/status", "/status", "显示当前模型与配置来源。"),
    SlashCommand("/model", "/model", "显示当前模型。"),
    SlashCommand("/model", "/model <model-name>", "把模型覆盖持久化到 ~/.corecode/settings.json。"),
    SlashCommand("/config-paths", "/config-paths", "显示 corecode 与 Claude 回退配置的路径。"),
    SlashCommand("/skills", "/skills", "列出发现的 SKILL.md 技能。"),
    SlashCommand("/mcp", "/mcp", "显示已配置的 MCP 服务器与连接状态。"),
    SlashCommand("/resume", "/resume", "恢复一个已保存的会话（交互选择，或 /resume <id>）。"),
    SlashCommand("/rename", "/rename <name>", "重命名当前会话。"),
    SlashCommand("/new", "/new", "清空已保存会话，重新开始。"),
    SlashCommand("/fork", "/fork", "把当前会话分叉成独立的新会话。"),
    SlashCommand("/permissions", "/permissions", "显示权限存储路径与当前规则。"),
    SlashCommand("/exit", "/exit", "退出。"),
    # 本地工具快捷方式：直接调用工具，**不经过模型**。
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
    SlashCommand("/compact", "/compact", "压缩会话上下文，释放上下文窗口空间。"),
    SlashCommand(
        "/collapse",
        "/collapse",
        "把旧的上下文区间投影为摘要，不删除真实转录。",
    ),
    SlashCommand("/snip", "/snip", "删除一段安全的中间历史，不调用模型。"),
    SlashCommand(
        "/init",
        "/init",
        "在当前项目里创建 .corecode/、.gitignore 条目与 CORECODE.md（幂等）。",
    ),
    SlashCommand("/memory", "/memory", "显示已加载进系统提示的指令文件。"),
)

def format_slash_commands() -> str:
    return "\n".join(f"{item.usage}    {item.description}" for item in SLASH_COMMANDS)

def find_matching_slash_commands(prefix: str) -> list[str]:
    return [item.usage for item in SLASH_COMMANDS if item.usage.startswith(prefix)]

def all_slash_usages() -> list[str]:
    return [item.usage for item in SLASH_COMMANDS]

def complete_slash_command(line: str) -> tuple[list[str], str]:
    """Tab 补全的候选集。**

        const hits = SLASH_COMMANDS.map(c => c.usage).filter(u => u.startsWith(line))
        return [hits.length > 0 ? hits : SLASH_COMMANDS.map(c => c.usage), line]

    也就是「按**整行**前缀匹配；一个都不匹配时列出全部」。返回
    `(候选, 被补全的内容)`，这是 Node `readline` 的 completer 契约。

    ## 这条函数被定义后很久没接线

    它一直存在，但**没有任何调用点**，也没有测试——`check_constants.py` 只检查
    **常量**的引用情况，不检查函数，所以这类缺陷逃过了工具。
    `tools/check_orphans.py` 就是为此补的。

    现在它由 `install_slash_completion()` 接进 readline。
    """
    hits = find_matching_slash_commands(line)
    return (hits if hits else all_slash_usages(), line)

def install_slash_completion() -> bool:
    """把斜杠命令补全装进 readline。返回是否装上。

    ## 为什么经常返回 False

    `readline` 是**可选**的 C 扩展：POSIX 上随标准库提供，**Windows 上没有**
    （`pyreadline3` 是第三方包，而本项目零第三方依赖）。

    所以这里是「有则用、无则静默跳过」：装不上不影响任何功能，只是按 Tab 没有
    补全。绝不能让「补全不可用」变成「程序起不来」。

    ## 为什么要把 delims 清空

    readline 默认按 ` \t\n"` 等字符切词，于是输入 `/loop st` 时按 Tab 只补全

    语义。Tab 绑定到 `complete`，让它列出全部候选。
    """
    try:
        import readline  # noqa: PLC0415 - 可选扩展，必须在函数内导入
    except ImportError:
        return False

    def _completer(text: str, state: int) -> str | None:
        # 用整行（而不是 readline 传来的词）做前缀匹配，见上面的说明。
        try:
            line = readline.get_line_buffer()
        except Exception:  # noqa: BLE001 - 某些实现没有 get_line_buffer
            line = text
        matches, _ = complete_slash_command(line)
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer_delims("")
        readline.set_completer(_completer)
        readline.parse_and_bind("tab: complete")
    except Exception:  # noqa: BLE001 - 装不上就当没有，不能影响启动
        return False
    return True

# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------

#: 会**改动文件系统或会话状态**的命令。
#:
#: 暴露出来是给测试用的：想确认「帮助里列的命令都有处理函数」时，不能把
#: `/init` 这类命令真的跑一遍——那会往仓库里写文件（我踩过：测试在真实仓库
#: 里创建了 `CORECODE.md` 并追加了 `.gitignore`）。
#: 有副作用的命令应当由各自的测试在**临时目录**里覆盖。
SIDE_EFFECTING_COMMANDS: frozenset[str] = frozenset(
    {
        "/init",
        "/new",
        "/fork",
        "/rename",
        "/resume",
        "/compact",
        "/collapse",
        "/snip",
        "/model",
    }
)

def _handler_names() -> frozenset[str]:
    """本地命令表里有哪些键。

    与 `try_handle_local_command` 共用同一份定义——两处各写一份清单迟早不一致，
    而那种不一致的症状是「测试说没问题，实际点了没反应」。
    """
    return frozenset(_HANDLER_KEYS)

#: 本地命令的键。`try_handle_local_command` 按它构造 handler 表。
_HANDLER_KEYS: tuple[str, ...] = (
    "/help",
    "/tools",
    "/status",
    "/plan",
    "/memory",
    "/skills",
    "/mcp",
    "/permissions",
    "/model",
    "/config-paths",
    "/init",
    "/resume",
    "/rename",
    "/fork",
    "/new",
    "/compact",
    "/collapse",
    "/snip",
)

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
        "/init": lambda: _do_init(host),
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
    """

        model: claude-sonnet-4
        baseUrl: https://api.anthropic.com
        auth: ANTHROPIC_API_KEY
        mcp servers: 0
        config: …/settings.json > …/.claude/settings.json > process.env

    这三样东西是**可被脚本读取**的：`corecode <<< /status | grep '^model:'` 是
    用户真实会做的事。把它换成中文表格（`模型  …`）会让这类用法直接失效。

    所以五行原样保留在**输出开头**，本实现多出来的信息（会话、上下文利用率、
    Goal、Loop、子 Agent、本会话统计）放在分隔线**之后**——加法，不是替换。
    """
    from .context.tokens import compute_context_stats
    from .ui import render_key_value, render_separator, render_status_line

    config = host.runtime_config
    #
    # 退出），所以 `(offline mock)` 是本实现自有的占位，不影响对等。
    original = [
        f"model: {config.model if config else '(offline mock)'}",
        f"baseUrl: {config.base_url if config else '(offline mock)'}",
        "auth: " + (
            "ANTHROPIC_AUTH_TOKEN"
            if config and config.auth_token
            else "ANTHROPIC_API_KEY"
        ),
        f"mcp servers: {len(config.mcp_servers) if config else 0}",
        config.source_summary if config else "config: (offline mock)",
    ]

    rows: list[tuple[str, str]] = [
        ("会话", host.session_id),
        ("目录", host.cwd),
        ("消息", f"{len(host.messages)} 条（已落盘游标 {host.already_saved_count}）"),
    ]

    if config:
        stats = compute_context_stats(host.messages, config.model)
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
            *original,
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

    # **状态标记是给人看的，用字形常量而不是字面量。**
    #
    # 注意这与 `format_plan()` 用的标记**刻意不同**：那个函数产出的是
    # **注入模型上下文**的文本（经 goal/context 进 system 消息），必须与
    # 用更易读的字形。
    from .ui import GLYPH_BULLET, GLYPH_OK

    marks = {
        "completed": GLYPH_OK,
        "in_progress": "▸",
        "pending": GLYPH_BULLET,
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

    static = render_memory_report(host.cwd)
    evolved = getattr(host, "evolved_memory", None)
    if evolved is None:
        return static
    return f"{static}\n\n{evolved.render_report()}"

def _render_skills(host: "SessionHost") -> str:
    from .ui import render_key_value, render_separator, render_status_line

    skills = host.tools.skills()
    if not skills:
        return (
            "没有发现技能。技能来自 `.corecode/skills/<name>/SKILL.md`"
            "（也兼容 `.claude/skills/`）。"
        )
    rows = []
    for item in skills:
        category = str(item.get("category") or "general")
        name = str(item.get("name", ""))
        rows.append((f"{category}/{name}", str(item.get("description", ""))))
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
        return "没有配置 MCP 服务器（见 ~/.corecode/mcp.json）。"
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
    """

    `cli-commands.ts` 的 `formatPermissionSummary()` 从一串 `label: value`
    里取值，拼成四行：

        permission store: {path}
        cwd: {workspace_root}
        extra allowed dirs: {…|none}
        dangerous allowlist: {…|none}

    ## 为什么不用表格

    `get_summary()` 产出的就是这串 `label: value`（系统提示用的也是它）。
    之前渲染时把它塞进对齐表格，于是：

    - 少了 `permission store:` 这个**路径**（最容易出问题的一项——用户要先找到
      文件才谈得上改规则）；
    - 标签变成中文、没有冒号，脚本 grep 不到。

    现在四行原样输出；本实现多出的 `trusted edit targets`（若有）附在末尾——
    """
    # **用动态访问器，不用导入期快照。**
    #
    # 这里此前用的是 `permissions.PERMISSIONS_PATH`——一个**导入期**求值的
    # 模块级常量。`CORECODE_HOME` 在导入后变化（测试就会这么做）时它指向错误
    # 位置，于是 `/permissions` 显示的是一个**并不生效**的路径。
    #
    # 与 `/config-paths` 保持一致：那条命令一直用的是动态访问器。
    from .permissions import permissions_path

    summary = host.permissions.get_summary()

    def value_of(label: str) -> str:
        prefix = f"{label}: "
        for part in summary:
            if part.startswith(prefix):
                return part[len(prefix):].strip() or "none"
        return "none"

    lines = [
        f"permission store: {permissions_path()}",
        f"cwd: {value_of('cwd')}",
        f"extra allowed dirs: {value_of('extra allowed dirs')}",
        f"dangerous allowlist: {value_of('dangerous allowlist')}",
    ]
    # 本实现多出的一类规则（D17）：不显示等于让人以为它不存在。
    trusted = value_of("trusted edit targets")
    if trusted != "none":
        lines.append(f"trusted edit targets: {trusted}")
    return "\n".join(lines)

def _render_config_paths() -> str:
    """

    对齐。理由有两条：

    1. 这四条路径的意义就是让人**复制**去改，任何截断或折行都会毁掉它；
    2. 用户会 `grep 'corecode mcp'` 来找配置在哪。形状一变就 grep 不到。

    最后一行是 **Claude Code 的兼容回退路径**。那一项不是装饰：配置加载确实
    会读它（见 `config.load_effective_settings`），把它显示出来用户才知道
    「为什么我没配却能跑」或「为什么改了不生效」。
    """
    from .config import (
        claude_settings_path,
        mcp_path,
        permissions_path,
        settings_path,
    )

    return "\n".join(
        [
            f"corecode settings: {settings_path()}",
            f"corecode permissions: {permissions_path()}",
            f"corecode mcp: {mcp_path()}",
            f"compat fallback: {claude_settings_path()}",
        ]
    )

# ---------------------------------------------------------------------------
# 动作类
# ---------------------------------------------------------------------------

def _do_model(host: "SessionHost", argument: str) -> str:
    """

    - `/model`         → 显示当前生效的模型
    - `/model <name>`  → 写进 `settings.json`
    - `/model `（空）  → 提示用法

    只更新 `model` 一个键，用户其余的配置原样保留。
    """
    from .config import save_settings, settings_path

    if not argument:
        # 对前者给「当前模型」。这里用 host 上是否已有模型来区分——
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

def _do_init(host: "SessionHost") -> str:
    """初始化仓库：`.corecode/` + `.gitignore` 条目 + `CORECODE.md`。

    重复跑只会得到「全部 skipped」。
    """
    from .repo_init import initialize_repo, render_init_report

    try:
        report = initialize_repo(host.cwd)
    except OSError as error:
        return f"初始化失败：{error}"
    return render_init_report(report)

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

