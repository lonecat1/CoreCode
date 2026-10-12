"""命令行入口。

把前面所有模块串成一个可用的程序：

    minicode                     交互式 REPL
    minicode -p "问题"           单次提问后退出（便于脚本与集成测试）
    minicode --resume [id]       恢复会话
    minicode --fork <id>         从会话分叉
    minicode --mock              离线模式（不需要 API key）

设计上的两个取向：

  - **没有 API key 也要能启动。** 配置缺失时自动退回离线 mock 模型，
    并明确告知用户——而不是直接抛异常退出。这样新用户能先看到它能做什么。
  - **权限提示是注入的。** `PermissionManager` 接收一个处理函数；行模式用
    控制台问答实现它，TUI 会用交互面板实现它，测试用自动批准实现它。
"""

from __future__ import annotations

import argparse
import sys
import threading
from typing import Any

from .adapters.anthropic import AnthropicModelAdapter
from .adapters.mock import MockModelAdapter
from .agents.manager import SubAgentManager
from .config import ConfigError, RuntimeConfig, load_runtime_config
from .host import HostCallbacks, SessionHost
from .permissions import (
    PermissionDenied,
    PermissionManager,
    PermissionRequest,
    auto_approve_prompt,
)
from .runtime.plan import PlanManager
from .tools import create_root_registry, create_worker_registry
from .types import ModelAdapter

#: `/exit` 之外还会结束进程的输入。
_EXIT_WORDS = frozenset({"/exit", "/quit", "exit", "quit"})


# ---------------------------------------------------------------------------
# 权限提示
# ---------------------------------------------------------------------------


def console_permission_prompt(
    request: PermissionRequest, signal: Any
) -> Any:
    """行模式的权限提问。

    展示摘要与细节，让用户按快捷键选择；无法读取输入时**默认拒绝**——
    权限提示绝不能因为读不到输入就放行。
    """
    from .permissions import PermissionPromptResult

    print()
    print(f"── 需要你的批准 ─────────────────────────────")
    print(f"  {request.summary}")
    for line in request.details:
        if line:
            print(f"  {line}")
    print()
    for choice in request.choices:
        print(f"  [{choice.key}] {choice.label}")

    try:
        answer = input("选择（直接回车 = 拒绝）: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return PermissionPromptResult(decision="deny_once")

    for choice in request.choices:
        if answer == choice.key.lower():
            feedback = None
            if choice.decision == "deny_with_feedback":
                try:
                    feedback = input("给模型的反馈: ").strip()
                except (EOFError, KeyboardInterrupt):
                    feedback = None
            return PermissionPromptResult(decision=choice.decision, feedback=feedback)

    # 未识别或空输入 → 拒绝（fail closed）。
    return PermissionPromptResult(decision="deny_once")


# ---------------------------------------------------------------------------
# 依赖装配
# ---------------------------------------------------------------------------


def _build_model(
    runtime: RuntimeConfig | None, mock: bool, tools_holder: dict[str, Any]
) -> ModelAdapter:
    if mock or runtime is None:
        return MockModelAdapter()

    def provider() -> RuntimeConfig:
        # 每次请求重新读配置：`/model` 切换后无需重建适配器。
        current = load_runtime_config()
        tools_holder["runtime"] = current
        return current

    return AnthropicModelAdapter(provider)


def build_host(
    args: argparse.Namespace,
    *,
    callbacks: HostCallbacks | None = None,
) -> SessionHost:
    """按命令行参数装配一个可用的会话宿主。"""
    import os

    cwd = os.path.abspath(args.cwd or os.getcwd())

    runtime: RuntimeConfig | None = None
    config_error: str | None = None
    if not args.mock:
        try:
            runtime = load_runtime_config()
        except ConfigError as error:
            config_error = str(error)

    # 先建工具表（子 Agent 需要只读子集），再建模型（它需要能列出工具）。
    plan = PlanManager("pending")
    permissions = PermissionManager(
        cwd,
        console_permission_prompt,
        store_path=None,
    )

    subagents_holder: dict[str, Any] = {}
    tools = create_root_registry(cwd=cwd)
    worker_tools = create_worker_registry(tools)

    model = _build_model(runtime, args.mock, subagents_holder)

    subagents = SubAgentManager(
        model=model,
        tools=worker_tools,
        cwd=cwd,
    )
    from .tools.subagents import create_subagent_tools

    tools.add_tools(create_subagent_tools(subagents))
    subagents_holder["manager"] = subagents

    # MCP 在**后台**连接：服务器冷启动可能要几秒，不该阻塞启动。
    # 先把状态标为 connecting，让 /mcp 立刻有东西可显示。
    #
    # 这个字典在 host 构造之前就存在，因此后台线程完成时可以安全地回填——
    # 它可能先于 host 就绪，所以回调里要判空。
    host_holder: dict[str, Any] = {}

    # MCP 配置与「有没有模型凭据」无关，因此独立读取。
    #
    # 之前它只经由 `load_runtime_config()` 获得，而 mock 模式不调用那个函数
    # （没有 API key 时会抛错）——结果是「没有 API key 就用不了 MCP」，
    # 这个耦合是错的：MCP 服务器不需要模型凭据。
    from .config import load_mcp_servers

    servers = (
        runtime.mcp_servers if runtime else load_mcp_servers(cwd=cwd)
    ) or {}

    mcp_holder: dict[str, Any] = {"bridge": None, "done": threading.Event()}
    if servers:
        from .mcp import build_connecting_summaries

        tools.set_mcp_servers(
            [item.to_dict() for item in build_connecting_summaries(servers)]
        )

        def _refresh_prompt() -> None:
            host = host_holder.get("host")
            if host is not None:
                host.refresh_system_prompt()

        _start_mcp_hydration(cwd, servers, tools, mcp_holder, on_ready=_refresh_prompt)

    session_id = args.session or _new_session_id()

    host = SessionHost(
        cwd=cwd,
        model=model,
        runtime_config=runtime,
        permissions=permissions,
        tools=tools,
        plan=plan,
        session_id=session_id,
        callbacks=callbacks,
        subagents=subagents,
    )
    host_holder["host"] = host
    if servers:
        host.attach_mcp_completion(mcp_holder["done"])
    host.refresh_system_prompt()

    if config_error:
        host._notice(  # noqa: SLF001 - 一次性告知，随后继续用 mock 模式
            f"未找到可用配置（{config_error}）\n已进入离线 mock 模式："
            "它能跑通完整流程，但不会调用真实模型。"
        )
    return host


def _new_session_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


def _start_mcp_hydration(
    cwd: str,
    servers: dict[str, Any],
    tools: Any,
    holder: dict[str, Any],
    on_ready: Any = None,
) -> None:
    """在后台连接 MCP 服务器，连上后把工具注入注册表。

    放后台的理由：服务器冷启动（`npx` 拉包、Python 导入重库）可能要数秒，
    阻塞启动会让「输入第一条消息」变得很慢。UI 期间显示 `connecting`。

    `on_ready` 用于在连接完成后重建系统提示——否则提示里会一直写着
    `connecting`，也拿不到 MCP 工具的命名说明。

    **`done` 必须在 `on_ready` 之后才置位**：它是「MCP 就绪」的公开信号，
    调用方（脚本、测试）等到它之后就会去读系统提示。先置位再刷新会留下
    一个竞态窗口，让等到信号的人读到尚未更新的提示。这个 bug 真实出现过。
    """

    def hydrate() -> None:
        try:
            from .mcp import create_mcp_backed_tools

            bridge = create_mcp_backed_tools(cwd, servers)
            tools.add_tools(bridge.tools)
            tools.set_mcp_servers([item.to_dict() for item in bridge.summaries])
            tools.add_disposer(bridge.dispose)
            holder["bridge"] = bridge
        except Exception:  # noqa: BLE001 - MCP 失败绝不该影响主流程
            pass
        finally:
            if on_ready is not None:
                try:
                    on_ready()
                except Exception:  # noqa: BLE001
                    pass
            # 最后一步：所有可观察状态都已就绪，此时才对外宣告完成。
            holder["done"].set()

    threading.Thread(target=hydrate, name="minicode-mcp", daemon=True).start()


# ---------------------------------------------------------------------------
# 本地命令
# ---------------------------------------------------------------------------


def handle_local_command(host: SessionHost, text: str) -> str | None:
    """处理不涉及运行时状态的斜杠命令。返回 None 表示「不认识」。"""
    from .cli_commands import try_handle_local_command

    return try_handle_local_command(host, text)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minicode",
        description="轻量的终端编码助手。",
    )
    parser.add_argument("-p", "--prompt", help="单次提问后退出（不进入 REPL）")
    parser.add_argument("--resume", nargs="?", const="__picker__", help="恢复会话（可给 id）")
    parser.add_argument("--fork", help="从指定会话分叉后继续")
    parser.add_argument("--mock", action="store_true", help="离线 mock 模式，不需要 API key")
    parser.add_argument("--cwd", help="工作目录（默认当前目录）")
    parser.add_argument("--session", help="指定会话 id")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="自动批准所有权限请求（危险：仅用于受信任的脚本场景）",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="使用全屏差分帧界面（需要真实终端；非终端环境下自动回退到行模式）",
    )
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _ensure_utf8_output() -> None:
    """把 stdout/stderr 钉在 UTF-8。

    Windows 控制台默认编码是 cp936 之类，而本程序的界面文本是中文。
    不处理的话用户看到的是乱码——「程序明明能用却像是坏了」。

    无法重配时静默跳过；errors="replace" 保证任何编码异常都不会中断程序。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    callbacks = HostCallbacks(
        on_assistant=lambda text, is_final: print(f"\n{text}\n"),
        on_progress=lambda text: print(f"  · {text}"),
        on_tool_start=lambda name, data: print(f"  → {name}"),
        on_tool_result=lambda name, output, is_error: print(
            f"  {'✗' if is_error else '✓'} {name}"
        ),
        on_notice=lambda text: print(f"\n{text}\n"),
    )

    host = build_host(args, callbacks=callbacks)
    if args.yes:
        # 显式替换提示函数：用户已声明「全部放行」。
        host.permissions._prompt = auto_approve_prompt  # noqa: SLF001

    # 会话装载
    if args.fork:
        forked = host.fork(args.fork)
        if forked is None:
            print(f"找不到会话 {args.fork}，无法分叉。", file=sys.stderr)
            return 1
        host.session_id = forked
    elif args.resume:
        if args.resume == "__picker__":
            _print_sessions(host)
        elif not host.resume(args.resume):
            print(f"找不到会话 {args.resume}。", file=sys.stderr)
            return 1

    try:
        if args.prompt:
            return _run_once(host, args.prompt)
        if args.tui:
            code = _run_tui(host)
            if code is not None:
                return code
            print(
                "当前环境不是真实终端，已回退到行模式。"
                "（全屏界面需要 TTY；管道或重定向下无法使用）",
                file=sys.stderr,
            )
        return _run_repl(host)
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        host.stop()


def _run_tui(host: SessionHost) -> int | None:
    """尝试全屏界面。非 TTY 时返回 None，让调用方回退到行模式。"""
    from .tui import (
        AppState,
        HostBridge,
        Terminal,
        build_banner_stats,
        make_terminal_events,
        run_tui,
    )

    terminal = Terminal()
    if not terminal.interactive:
        return None

    events_holder: dict[str, Any] = {}
    state = AppState()
    state.banner = build_banner_stats(host)

    def run_local(text: str) -> str | None:
        reply = host.runtime.command(text)
        if reply is not None:
            return reply
        return handle_local_command(host, text)

    def run_turn(text: str) -> None:
        events = events_holder["events"]

        def on_assistant(content: str, is_final: bool) -> None:
            events.push("assistant", content)

        def on_progress(content: str) -> None:
            events.push("progress", content)

        def on_tool_start(name: str, data: Any) -> None:
            events.push("tool_start", str(name))

        def on_tool_result(name: str, output: str, is_error: bool) -> None:
            events.push("tool_result", str(name), not is_error)

        host.callbacks.on_assistant = on_assistant
        host.callbacks.on_progress = on_progress
        host.callbacks.on_tool_start = on_tool_start
        host.callbacks.on_tool_result = on_tool_result
        host.callbacks.on_notice = lambda text: events.push("notice", text)

        result = host.runtime.submit(text)
        if result.outcome in ("failed", "aborted"):
            events.push("error", f"回合结束：{result.outcome} {result.error or ''}")

    bridge = HostBridge(
        run_turn=run_turn,
        run_local=run_local,
        is_busy=lambda: host.is_busy,
        summary=lambda: build_banner_stats(host),
    )

    # 事件对象要先于回调存在，因此在这里构造并放进 holder。
    from .tui import UiEvents

    events_holder["events"] = UiEvents()
    return run_tui(state, bridge, terminal=terminal)


def _run_once(host: SessionHost, prompt: str) -> int:
    """单次提问后退出。这是集成测试与脚本使用的主路径。

    分派顺序必须与 REPL 一致（`_dispatch`）：运行时命令 → 本地命令 → 普通回合。
    少了中间那一步，`-p /status` 之类的本地命令会被当成对话内容发给模型。
    """
    try:
        reply = host.runtime.command(prompt)
        if reply is not None:
            print(reply)
            return 0

        local = handle_local_command(host, prompt)
        if local is not None:
            print(local)
            return 0

        if prompt.startswith("/"):
            print(f"未知命令：{prompt}。输入 /help 查看可用命令。", file=sys.stderr)
            return 1

        result = host.runtime.submit(prompt)
        if result.outcome in ("failed", "aborted"):
            print(f"\n回合结束：{result.outcome} {result.error or ''}", file=sys.stderr)
            return 1
        return 0
    except Exception as error:  # noqa: BLE001 - CLI 顶层要把错误变成退出码
        print(f"错误：{error}", file=sys.stderr)
        return 1


def _run_repl(host: SessionHost) -> int:
    """交互式行模式。"""
    print(_banner(host))
    print("输入 /help 查看命令，/exit 退出。\n")

    while True:
        try:
            line = input("› ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            # Ctrl-C 在输入行：取消当前输入而不是退出程序。
            print()
            continue

        if not line:
            continue
        if line in _EXIT_WORDS:
            return 0

        try:
            _dispatch(host, line)
        except KeyboardInterrupt:
            print("\n已中断。")
            host.runtime.turns.stop("用户中断")
        except Exception as error:  # noqa: BLE001 - 单条命令失败不该退出 REPL
            print(f"错误：{error}", file=sys.stderr)


def _dispatch(host: SessionHost, line: str) -> None:
    """一条输入的分派顺序：运行时命令 → 本地命令 → 普通回合。"""
    reply = host.runtime.command(line)
    if reply is not None:
        print(reply)
        return

    local = handle_local_command(host, line)
    if local is not None:
        print(local)
        return

    if line.startswith("/"):
        print(f"未知命令：{line}。输入 /help 查看可用命令。")
        return

    host.runtime.submit(line)


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def _banner(host: SessionHost) -> str:
    model_name = host.runtime_config.model if host.runtime_config else "离线 mock"
    return "\n".join(
        [
            "╭──────────────────────────────────────────────╮",
            "│  MiniCode (Python)                           │",
            "╰──────────────────────────────────────────────╯",
            f"  模型    : {model_name}",
            f"  会话    : {host.session_id}",
            f"  工作目录: {host.cwd}",
            f"  工具    : {len(host.tools.list())} 个"
            f"（子 Agent 可见 {len(create_worker_registry(host.tools).list())} 个只读工具）",
        ]
    )


def _print_sessions(host: SessionHost) -> None:
    from .session import list_sessions

    sessions = list_sessions(host.cwd)
    if not sessions:
        print("当前目录还没有会话。")
        return
    print(f"共 {len(sessions)} 个会话：")
    for meta in sessions:
        title = meta.title or "(无标题)"
        print(f"  {meta.id}  {title}  [{meta.message_count} 行]")


if __name__ == "__main__":
    raise SystemExit(main())
