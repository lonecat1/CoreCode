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
from .ui import (
    COLOR_HINT,
    COLOR_USER,
    ConsoleUI,
    build_console_ui,
    paint,
    render_banner,
    set_animation_enabled,
)

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
        "--no-animation",
        action="store_true",
        help="关闭启动动画与转圈（非终端环境下本就会自动关闭）",
    )
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _ensure_utf8_output() -> None:
    """把 stdin / stdout / stderr 都钉在 UTF-8。

    Windows 控制台默认编码是 cp936 之类，而本程序的界面文本与用户输入都是中文。
    三个流都要处理，缺一个就会出问题：

      - 只改 stdout → 输出乱码（「程序明明能用却像是坏了」）。
      - 只改 stdout+stderr → **管道输入**仍然按 cp936 解码，中文变成代理字符，
        随后写回输出时抛 `UnicodeEncodeError: surrogates not allowed`。
        本实现实际踩过这条。

    无法重配时静默跳过；errors="replace" 保证任何编码异常都不会中断程序。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
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

    if getattr(args, "no_animation", False):
        set_animation_enabled(False)

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
        return _run_repl(host)
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        host.stop()


def _run_once(host: SessionHost, prompt: str) -> int:
    """单次提问后退出。这是集成测试与脚本使用的主路径。

    分派顺序必须与 REPL 一致（`_dispatch`）：运行时命令 → 本地命令 → 普通回合。
    少了中间那一步，`-p /status` 之类的本地命令会被当成对话内容发给模型。

    输出走与 REPL 相同的界面层，因此两种模式的外观一致；同时因为界面层
    会在非终端环境下自动关闭颜色，`-p` 的输出可以直接重定向到文件。
    """
    ui = build_console_ui()
    _attach_ui_callbacks(host, ui)
    try:
        reply = host.runtime.command(prompt)
        if reply is not None:
            ui.write(reply)
            return 0

        local = handle_local_command(host, prompt)
        if local is not None:
            ui.write(local)
            return 0

        if prompt.startswith("/"):
            ui.error(f"未知命令：{prompt}。输入 /help 查看可用命令。")
            return 1

        ui.begin_turn()
        result = host.runtime.submit(prompt)
        ui.end_turn()
        if result.outcome in ("failed", "aborted"):
            ui.error(f"回合结束：{result.outcome} {result.error or ''}")
            return 1
        return 0
    except Exception as error:  # noqa: BLE001 - CLI 顶层要把错误变成退出码
        ui.error(f"错误：{error}")
        return 1


def _run_repl(host: SessionHost) -> int:
    """交互式行模式（美化输出）。

    **这是默认且唯一推荐的交互方式。** 它不做 raw mode、不做光标定位、
    不做全屏重绘——只往 stdout 写文本。因此在你任何终端里都能工作，
    也能安全地在管道/CI 里使用。

    （早先有一个全屏 TUI 模式，因为依赖 raw mode 与非阻塞读键，在
    Git Bash / MSYS2 等环境下按键永远读不到，已移除。见 README。）
    """
    ui = build_console_ui()
    ui.play_startup()
    _print_banner(host, ui)
    _attach_ui_callbacks(host, ui)

    while True:
        try:
            line = input(paint("› ", COLOR_USER)).strip()
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
            _dispatch(host, line, ui)
        except KeyboardInterrupt:
            ui.notice("已中断。")
            host.runtime.turns.stop("用户中断")
        except Exception as error:  # noqa: BLE001 - 单条命令失败不该退出 REPL
            ui.error(f"错误：{error}")


def _attach_ui_callbacks(host: SessionHost, ui: "ConsoleUI") -> None:
    """把界面接到宿主的观察点上。

    宿主通过 `HostCallbacks` 暴露观察点，因此界面层不需要知道 agent 循环的
    任何细节——这也是行模式界面能这么薄的原因。
    """
    callbacks = ui.callbacks()
    host.callbacks.on_assistant = callbacks["on_assistant"]
    host.callbacks.on_progress = callbacks["on_progress"]
    host.callbacks.on_tool_start = callbacks["on_tool_start"]
    host.callbacks.on_tool_result = callbacks["on_tool_result"]
    host.callbacks.on_notice = callbacks["on_notice"]


def _print_banner(host: SessionHost, ui: "ConsoleUI") -> None:
    from .tools import create_worker_registry

    model_name = host.runtime_config.model if host.runtime_config else "离线 mock"
    skills = []
    try:
        skills = host.tools.skills()
    except Exception:  # noqa: BLE001
        skills = []

    ui.write(
        render_banner(
            model=model_name,
            session_id=host.session_id,
            cwd=host.cwd,
            tool_count=len(host.tools.list()),
            readonly_tool_count=len(create_worker_registry(host.tools).list()),
            skill_count=len(skills),
            width=ui.width,
        )
    )
    ui.write(
        paint("输入 ", COLOR_HINT)
        + paint("/help", COLOR_USER)
        + paint(" 查看命令 · ", COLOR_HINT)
        + paint("/exit", COLOR_USER)
        + paint(" 退出", COLOR_HINT)
    )
    ui.blank()


def _dispatch(host: SessionHost, line: str, ui: "ConsoleUI") -> None:
    """一条输入的分派顺序：运行时命令 → 本地命令 → 普通回合。"""
    reply = host.runtime.command(line)
    if reply is not None:
        ui.write(reply)
        ui.blank()
        return

    local = handle_local_command(host, line)
    if local is not None:
        ui.write(local)
        ui.blank()
        return

    if line.startswith("/"):
        ui.error(f"未知命令：{line}。输入 /help 查看可用命令。")
        return

    ui.begin_turn()
    result = host.runtime.submit(line)
    ui.end_turn()
    if result.outcome in ("failed", "aborted"):
        ui.error(f"回合结束：{result.outcome} {result.error or ''}")


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
