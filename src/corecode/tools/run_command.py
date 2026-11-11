"""run_command —— 在允许清单内执行开发命令。

## 这个工具为什么关键

它是唯一让 agent **执行动作**的工具。缺了它，agent 只能读写文件——不能跑
测试、编译、git、构建。也就是说「会写代码」和「会验证代码」是两件事，后者
完全依赖它。

**① 命令允许清单。** 只有 `READONLY_COMMANDS`（纯观察类）与
`DEVELOPMENT_COMMANDS`（开发工具）里的命令可以直接执行。

**② 权限分级。** 三种情况的处理不同：

    useShell（shell 片段）        → 要审批（片段内容无法静态判断）
    已知的只读命令                 → **完全不审批**（pwd/ls/cat 这类）
    已知的开发命令 / 未知命令      → 要审批；未知命令额外带 force_prompt_reason

「已知只读命令不审批」是刻意的：`ls`、`cat`、`pwd` 这类每天要跑几十次，
每次都弹审批会让工具变得不可用，而它们本来就不改变任何状态。

**③ shell 片段必须整体交给 shell。** 命令里出现 `| & ; < > ( ) $ \\`` 时，
静态拆分没有意义（管道、重定向、变量展开都需要 shell），因此整条交给
`bash -lc`。此时**不做**命令名白名单检查——白名单只对拆得出的命令名有效。

**④ 后台执行。** 片段以 `&` 结尾（且不是 `&&`）时不等待，立刻返回任务句柄，
交给 `background_tasks` 登记，之后可以查询进程是否还活着。

"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Any

from ..background_tasks import register_background_shell_task
from ..tooling import ToolContext, ToolDefinition, ToolResult
from ..workspace import resolve_tool_path

__all__ = ["run_command_tool"]

#: 纯观察类命令。它们不改变任何状态，因此**不需要审批**。
#:
#: 必须先确认它确实无副作用。
READONLY_COMMANDS = frozenset(
    {
        "pwd",
        "ls",
        "find",
        "rg",
        "grep",
        "cat",
        "head",
        "tail",
        "wc",
        "sed",
        "echo",
        "df",
        "du",
        "free",
        "uname",
        "uptime",
        "whoami",
    }
)

#: 开发工具。它们会改变状态（编译产物、git 历史），因此需要审批。
DEVELOPMENT_COMMANDS = frozenset(
    {
        "git",
        "npm",
        "node",
        "python3",
        "pytest",
        "bash",
        "sh",
        "bun",
    }
)

#: 输出上限（字节）。超过就截断并注明。
#:
#: 理由见模块文档。
MAX_OUTPUT_BYTES = 1024 * 1024

#: 轮询进程状态的间隔（秒）。用于在等待时检查取消信号。
#:
#: 取 0.1 是权衡：更小会让长命令空转 CPU，更大则让取消有明显的迟滞感。
POLL_INTERVAL_SECONDS = 0.1

SHELL_EXECUTABLE = "bash"

#: 出现在命令里就说明「这是一个 shell 片段」的字符。
#:
#: 管道、后台、顺序执行、重定向、子 shell、命令替换、变量展开——
#: 这些都无法靠静态拆分正确还原，必须整体交给 shell。
SHELL_METACHARACTERS = frozenset("|&;<>()$`")

def is_allowed_command(command: str) -> bool:
    """命令名是否在允许清单里（只读或开发）。"""
    return command in READONLY_COMMANDS or command in DEVELOPMENT_COMMANDS

def is_readonly_command(command: str) -> bool:
    """命令名是否是纯观察类。只有这一类**免审批**。"""
    return command in READONLY_COMMANDS

def split_command_line(command_line: str) -> list[str]:
    """把一行命令拆成 `[命令, 参数...]`。

    ## 一个必须知道的副作用

    反斜杠**在任何位置都是转义字符**，引号内也一样。因此在 Windows 上
    `python C:\\path\\to\\x.py` 会被拆成 `C:pathtox.py`。

    """
    parts: list[str] = []
    current = ""
    quote: str | None = None
    escaping = False

    for char in command_line:
        if escaping:
            current += char
            escaping = False
            continue

        if char == "\\":
            escaping = True
            continue

        if quote:
            if char == quote:
                quote = None
            else:
                current += char
            continue

        if char in ("'", '"'):
            quote = char
            continue

        if char.isspace():
            if current:
                parts.append(current)
                current = ""
            continue

        current += char

    # 结尾是孤立的反斜杠：原样保留，而不是丢掉。
    if escaping:
        current += "\\"

    if current:
        parts.append(current)

    return parts

def normalize_command_input(command: str, args: list[str] | None) -> tuple[str, list[str]]:
    """归一化成 `(命令, 参数)`。

    给了 `args` 就以它为准（**不做拆分**，因此 Windows 路径可以安全地走这条路）；
    否则把 `command` 当成一整行来拆。
    """
    if args:
        return command.strip(), list(args)

    trimmed = command.strip()
    if not trimmed:
        return "", []

    parsed = split_command_line(trimmed)
    if not parsed:
        return "", []
    return parsed[0], parsed[1:]

def looks_like_shell_snippet(command: str, args: list[str] | None) -> bool:
    """是否必须整条交给 shell 执行。

    **只有没给 `args` 时才判断**：给了 `args` 说明调用方已经把命令和参数
    分开了，不需要也不应该再当成片段。
    """
    if args:
        return False
    return any(char in SHELL_METACHARACTERS for char in command)

def is_background_shell_snippet(command: str, args: list[str] | None) -> bool:
    """是否是「后台执行」的片段：以 `&` 结尾，但**不是** `&&`。

    `&&` 是顺序执行（跑完再跑下一个），不是后台，必须区分开。
    """
    if args:
        return False
    trimmed = command.strip()
    return trimmed.endswith("&") and not trimmed.endswith("&&")

def strip_trailing_background_operator(command: str) -> str:
    """去掉结尾的 `&`。交给 shell 的片段不该再带后台操作符——
    后台与否由 `Popen` 的分离方式决定，而不是让 shell 自己 fork。"""
    return command.strip().removesuffix("&").strip()

def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")

def _run_process(
    cmd_args: list[str],
    *,
    cwd: str,
    signal: Any | None,
) -> tuple[int, bytes, bytes, bool]:
    """跑一个前台进程，支持取消与输出上限。

    返回 `(退出码, stdout, stderr, 是否被截断)`。

    用 `communicate(timeout=...)` 轮询而不是 `subprocess.run`：后者无法在等待
    期间检查取消信号，一条 `sleep 300` 就会让 Ctrl-C 失效。
    """
    process = subprocess.Popen(  # noqa: S603 - 命令来自允许清单或用户审批
        cmd_args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=POLL_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if signal is not None and getattr(signal, "is_set", lambda: False)():
                    process.kill()
                    stdout, stderr = process.communicate()
                    return process.returncode or -1, stdout or b"", stderr or b"", False
    except BaseException:
        # 任何异常路径都不能留下孤儿进程。
        process.kill()
        process.communicate()
        raise

    truncated = False
    if len(stdout) + len(stderr) > MAX_OUTPUT_BYTES:
        truncated = True
        budget = MAX_OUTPUT_BYTES
        stdout = stdout[:budget]
        budget -= len(stdout)
        stderr = stderr[: max(0, budget)]

    return process.returncode or 0, stdout, stderr, truncated

def _spawn_background(cmd_args: list[str], *, cwd: str) -> int:
    """启动一个分离的后台进程，返回 PID。

    分离（detached）是必需的：agent 回合结束后父进程可能退出，后台任务
    不该被一起带走。三路 stdio 都接到 `DEVNULL`，否则子进程持有管道会让
    父进程的读取端永远等不到 EOF。
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    child = subprocess.Popen(cmd_args, **kwargs)  # noqa: S603
    return child.pid

def _wsl_shim_path() -> str:
    """WSL 转发器的路径：`%SystemRoot%\\System32\\bash.exe`。"""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(system_root, "System32", "bash.exe")

def _is_wsl_shim(path: str) -> bool:
    """这个 `bash.exe` 是不是 WSL 转发器。

    ## 为什么要专门识别它

    Windows 自带 `System32\\bash.exe`，它**不是 bash**，而是转发到 WSL 发行版的
    代理。在没有安装 Linux 发行版（或只有 docker-desktop）的机器上，它会把
    任何命令都变成这样一句难以理解的话：

        WSL (11 - Relay) ERROR: CreateProcessCommon:875: execvpe(/bin/bash) ...

    用户看到这句话完全无法判断问题出在哪。识别出来并换用 Git Bash（或给出
    清晰的替代提示）比让这个错误冒出来有用得多。
    """
    try:
        return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(_wsl_shim_path())
        )
    except (OSError, ValueError):
        return False

def _windows_bash_candidates() -> list[str]:
    """Windows 上真正的 POSIX bash 的常见位置（Git for Windows）。"""
    candidates: list[str] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.append(os.path.join(root, "Git", "bin", "bash.exe"))
        candidates.append(os.path.join(root, "Programs", "Git", "bin", "bash.exe"))
    return candidates

def _resolve_shell() -> str | None:
    """找到可用于 `-lc` 的 POSIX shell，找不到返回 `None`。

    ## 查找顺序（Windows）

    1. **Git for Windows 的 `bin\\bash.exe`** —— 真正的 bash，首选；
    2. PATH 里的 `bash`，**但排除 WSL 转发器**（见 `_is_wsl_shim`）；
    3. 都没有 → `None`，由调用方给出可操作的错误。

    刻意**不回退到 `cmd.exe`**：cmd 支持 `|` 和重定向，但不支持 `$VAR`、
    反引号、子 shell。用它执行一个 POSIX 片段会**静默产生错误结果**——
    这比明确报错更糟。宁可不做，也不要装作做了。
    """
    if os.name != "nt":
        return shutil.which(SHELL_EXECUTABLE)

    for candidate in _windows_bash_candidates():
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which(SHELL_EXECUTABLE)
    if found and not _is_wsl_shim(found):
        return found
    return None

def _run_command(data: Any, context: ToolContext) -> ToolResult:
    raw = data or {}
    command_line = str(raw.get("command", ""))
    raw_args = raw.get("args")
    args = [str(item) for item in raw_args] if isinstance(raw_args, list) else None
    raw_cwd = raw.get("cwd")

    # --- 1. 解析工作目录 ---------------------------------------------------
    #
    # 内部还会用 `'command_cwd'` 再校验一次。两次校验是有意的：前者管「能不能
    # 访问这个目录」，后者管「能不能在这个目录里跑命令」。
    effective_cwd = (
        resolve_tool_path(context, str(raw_cwd), "list") if raw_cwd else context.cwd
    )

    # --- 2. 归一化命令 -----------------------------------------------------
    command, command_args = normalize_command_input(command_line, args)
    if not command:
        return ToolResult.failure("Command not allowed: empty command")

    use_shell = looks_like_shell_snippet(command_line, args)
    background_shell = is_background_shell_snippet(command_line, args)
    known_command = is_allowed_command(command)

    # --- 3. 组装真正要执行的东西 -------------------------------------------
    #
    # 注意这里区分了**逻辑命令名**与**实际可执行文件**：
    #
    #   逻辑名   "bash"                    → 交给权限系统做分类与签名
    #   可执行   "C:\...\Git\bin\bash.exe" → 真正去跑
    #
    # 需要显式解析路径（Windows 上 Path 里的 bash 是 WSL 转发器），但如果把
    # 解析后的全路径交给权限系统，`classify_dangerous_command` 就拿不到
    # "bash" 这个名字——而**正是它判定「可执行任意本地代码」的依据**。
    # 结果是 shell 片段会绕过审批。这是必须分开的原因。
    if use_shell:
        shell = _resolve_shell()
        if shell is None:
            return ToolResult.failure(
                f"shell 片段需要真正的 `{SHELL_EXECUTABLE}`，但在本机找不到可用的。\n"
                "· Windows 上系统自带的 `System32\\bash.exe` 是 WSL 转发器，不算；\n"
                "· 请安装 Git for Windows（它自带 bash），"
                "或改用 `command` + `args` 的形式——那条路径不拆分、也不需要 shell。"
            )
        snippet = (
            strip_trailing_background_operator(command_line)
            if background_shell
            else command_line
        )
        executable = shell
        executable_args = ["-lc", snippet]
        permission_command = SHELL_EXECUTABLE
        permission_args = executable_args
    else:
        # 非 shell 路径下，交给系统的可执行文件解析（Windows 上会补 .exe 等）。
        executable = command
        executable_args = command_args
        permission_command = command
        permission_args = command_args

    # --- 4. 权限 -----------------------------------------------------------
    #
    # 三种情况，顺序不能变：
    #   a) shell 片段        → 审批（内容无法静态判断）
    #   b) 未知命令          → 审批，并带 force_prompt_reason 说明原因
    #   c) 已知但非只读      → 审批
    #   d) 已知只读          → 直接放行
    permissions = context.permissions
    if permissions is not None:
        force_reason: str | None = None
        if not use_shell and not known_command:
            force_reason = (
                f"Unknown command '{command}' is not in the built-in "
                "read-only/development set"
            )

        if force_reason is not None:
            permissions.ensure_command(
                permission_command,
                permission_args,
                effective_cwd,
                force_prompt_reason=force_reason,
            )
        elif use_shell or not is_readonly_command(command):
            permissions.ensure_command(
                permission_command, permission_args, effective_cwd
            )

    if context.signal is not None and getattr(context.signal, "is_set", lambda: False)():
        return ToolResult.failure("Cancelled before execution.", fatal=True)

    # --- 5. 后台执行 -------------------------------------------------------
    if use_shell and background_shell:
        pid = _spawn_background([executable, *executable_args], cwd=effective_cwd)
        task = register_background_shell_task(
            command=strip_trailing_background_operator(command_line),
            pid=pid,
            cwd=effective_cwd,
        )
        return ToolResult(
            ok=True,
            output=(
                "Background command started.\n"
                f"TASK: {task.task_id}\n"
                f"PID: {task.pid}"
            ),
            background_task=task,
        )

    # --- 6. 前台执行 -------------------------------------------------------
    try:
        returncode, stdout, stderr, truncated = _run_process(
            [executable, *executable_args], cwd=effective_cwd, signal=context.signal
        )
    except FileNotFoundError:
        return ToolResult.failure(
            f"Command not found: {executable}"
        )
    except OSError as error:
        return ToolResult.failure(f"{type(error).__name__}: {error}", fatal=True)

    parts = [part for part in (_decode(stdout), _decode(stderr)) if part.strip()]
    body = "\n".join(parts).strip()
    if truncated:
        body += f"\n\n… 输出超过 {MAX_OUTPUT_BYTES // 1024}KB，已截断。"

    if returncode != 0:
        # `ok:false, fatal:true`；这里显式构造同样的结果，且**保留输出**。
        detail = body or "(no output)"
        return ToolResult.failure(
            f"Command failed (exit code {returncode}): "
            f"{' '.join([executable, *executable_args])}\n{detail}",
            fatal=True,
        )

    return ToolResult.success(body or "(no output)")

run_command_tool = ToolDefinition(
    name="run_command",
    description=(
        "Run a common development command from an allowlist. "
        "For shell pipelines or variable expansion, pass the full snippet "
        "in command and corecode will run it via bash -lc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
        },
        "required": ["command"],
    },
    validator=lambda data: {
        "command": str((data or {}).get("command", "")),
        **(
            {"args": [str(item) for item in (data or {}).get("args") or []]}
            if (data or {}).get("args")
            else {}
        ),
        **({"cwd": str((data or {}).get("cwd"))} if (data or {}).get("cwd") else {}),
    },
    run=_run_command,
)
