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
    tools = create_root_registry()
    worker_tools = create_worker_registry(tools)

    model = _build_model(runtime, args.mock, subagents_holder)

    subagents = SubAgentManager(
        model=model,
        tools=worker_tools,
        cwd=cwd,
    )
    tools.add_tools([])  # 占位：子 Agent 工具在下面统一加入
    from .tools.subagents import create_subagent_tools

    tools.add_tools(create_subagent_tools(subagents))
    subagents_holder["manager"] = subagents

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
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
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
