"""run_command 与 background_tasks 的测试。

对应原版 `MiniCode/src/tools/run-command.ts` 与 `src/background-tasks.ts`。

## 这个工具的安全边界值得单独测

`run_command` 是唯一能执行动作的工具，因此它的**判断逻辑**（哪些命令免审批、
哪些必须审批、什么算 shell 片段、什么算后台）比它的执行逻辑更值得钉死。
这些判断一旦错，要么让安全边界失效，要么让工具变得没法用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode import background_tasks  # noqa: E402
from minicode.permissions import (  # noqa: E402
    PermissionManager,
    PermissionPromptResult,
)
from minicode.tooling import ToolContext  # noqa: E402
from minicode.tools.run_command import (  # noqa: E402
    DEVELOPMENT_COMMANDS,
    READONLY_COMMANDS,
    _is_wsl_shim,
    _resolve_shell,
    is_allowed_command,
    is_background_shell_snippet,
    is_readonly_command,
    looks_like_shell_snippet,
    normalize_command_input,
    run_command_tool,
    split_command_line,
    strip_trailing_background_operator,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class CommandClassificationTests(unittest.TestCase):
    """允许清单的语义。"""

    def test_readonly_commands_are_allowed(self) -> None:
        for name in ("pwd", "ls", "cat", "grep", "echo", "wc"):
            self.assertTrue(is_allowed_command(name), name)
            self.assertTrue(is_readonly_command(name), name)

    def test_development_commands_are_allowed_but_not_readonly(self) -> None:
        """**这是免审批与需审批的分界线。**

        把 `git` 误判为只读，等于让 agent 可以不经批准改写 git 历史。
        """
        for name in ("git", "npm", "node", "pytest"):
            self.assertTrue(is_allowed_command(name), name)
            self.assertFalse(
                is_readonly_command(name), f"{name} 不该被当成只读命令"
            )

    def test_unknown_command_is_not_allowed(self) -> None:
        self.assertFalse(is_allowed_command("rm"))
        self.assertFalse(is_allowed_command("curl"))
        self.assertFalse(is_allowed_command(""))

    def test_allowlists_do_not_overlap(self) -> None:
        """两个清单必须互斥，否则「只读」判定就没有意义了。"""
        self.assertFalse(READONLY_COMMANDS & DEVELOPMENT_COMMANDS)

    def test_dangerous_commands_are_not_allowlisted(self) -> None:
        """破坏性命令不该出现在任何允许清单里。"""
        for name in ("rm", "mv", "dd", "mkfs", "shutdown", "format"):
            self.assertNotIn(name, READONLY_COMMANDS)
            self.assertNotIn(name, DEVELOPMENT_COMMANDS)


class SplitCommandLineTests(unittest.TestCase):
    """命令行的拆分（引号与转义）。"""

    def test_plain_command(self) -> None:
        self.assertEqual(split_command_line("git status --short"), ["git", "status", "--short"])

    def test_double_quotes_group_whitespace(self) -> None:
        self.assertEqual(split_command_line('echo "a b" c'), ["echo", "a b", "c"])

    def test_single_quotes_group_whitespace(self) -> None:
        self.assertEqual(split_command_line("echo 'a b' c"), ["echo", "a b", "c"])

    def test_backslash_escapes_next_char(self) -> None:
        self.assertEqual(split_command_line("a\\ b"), ["a b"])

    def test_trailing_backslash_is_preserved(self) -> None:
        """结尾孤立的反斜杠应当保留，而不是被吞掉。"""
        self.assertEqual(split_command_line("abc\\"), ["abc\\"])

    def test_multiple_whitespace_is_collapsed(self) -> None:
        self.assertEqual(split_command_line("x   y"), ["x", "y"])

    def test_empty_string_yields_nothing(self) -> None:
        self.assertEqual(split_command_line(""), [])
        self.assertEqual(split_command_line("   "), [])

    def test_quotes_are_removed_from_result(self) -> None:
        self.assertEqual(split_command_line('"a"'), ["a"])

    def test_backslash_inside_quotes_still_escapes(self) -> None:
        """**原版行为**：转义在引号内同样生效。

        这会让 Windows 路径在单行形式里被破坏（`C:\\x` → `C:x`），
        因此 `args` 数组才是传 Windows 路径的正确方式。这条测试把这个
        事实钉住，避免以后有人「顺手修好它」而破坏与原版的一致性。
        """
        self.assertEqual(split_command_line('"C:\\path"'), ["C:path"])


class NormalizeCommandInputTests(unittest.TestCase):
    def test_args_take_precedence_and_are_not_split(self) -> None:
        """给了 args 就原样使用——这是 Windows 路径的安全通道。"""
        command, args = normalize_command_input("git", ["status", "--short"])
        self.assertEqual(command, "git")
        self.assertEqual(args, ["status", "--short"])

    def test_args_bypass_splitting_of_the_command_field(self) -> None:
        command, args = normalize_command_input("C:\\path\\to\\x.exe", ["a"])
        self.assertEqual(command, "C:\\path\\to\\x.exe")
        self.assertEqual(args, ["a"])

    def test_command_is_split_when_args_absent(self) -> None:
        command, args = normalize_command_input("git log --oneline", None)
        self.assertEqual(command, "git")
        self.assertEqual(args, ["log", "--oneline"])

    def test_empty_command(self) -> None:
        self.assertEqual(normalize_command_input("   ", None), ("", []))

    def test_empty_args_list_falls_back_to_splitting(self) -> None:
        """空 args 等价于没给，应当回落去拆分 command。"""
        command, args = normalize_command_input("git status", [])
        self.assertEqual(command, "git")
        self.assertEqual(args, ["status"])


class ShellSnippetDetectionTests(unittest.TestCase):
    """什么必须交给 shell。"""

    def test_pipe_is_a_shell_snippet(self) -> None:
        self.assertTrue(looks_like_shell_snippet("ls | wc -l", None))

    def test_redirect_is_a_shell_snippet(self) -> None:
        self.assertTrue(looks_like_shell_snippet("echo hi > out.txt", None))

    def test_variable_expansion_is_a_shell_snippet(self) -> None:
        self.assertTrue(looks_like_shell_snippet("echo $HOME", None))

    def test_command_substitution_is_a_shell_snippet(self) -> None:
        self.assertTrue(looks_like_shell_snippet("echo `date`", None))

    def test_plain_command_is_not_a_shell_snippet(self) -> None:
        self.assertFalse(looks_like_shell_snippet("git status", None))

    def test_args_present_disables_snippet_detection(self) -> None:
        """**关键**：给了 args 说明调用方已分好词，不该再当片段。

        否则 `command="echo"`, `args=["a|b"]` 会被塞进 shell，`|` 变成管道，
        用户拿到的结果与预期完全不同。

        **第二个断言才是杀死变异的关键**：只测 `command="echo"`（无元字符）
        时，即使去掉 args 判断，函数仍旧返回 False —— 变异存活。必须让
        `command` 本身含元字符，才能区分「有没有做 args 判断」。
        """
        # 元字符出现在参数里
        self.assertFalse(looks_like_shell_snippet("rg", ["a|b"]))
        self.assertFalse(looks_like_shell_snippet("echo", ["$HOME"]))
        # 元字符出现在 command 里，但给了 args → 仍不当片段
        self.assertFalse(
            looks_like_shell_snippet("echo > out.txt", ["x"]),
            "给了 args 时 command 应被当作可执行文件名，不解析元字符",
        )

    def test_snippet_detection_still_works_without_args(self) -> None:
        """对照组：没给 args 时同样的字符串必须被判定为片段。"""
        self.assertTrue(looks_like_shell_snippet("echo > out.txt", None))
        self.assertTrue(looks_like_shell_snippet("rg a|b", None))


class BackgroundSnippetDetectionTests(unittest.TestCase):
    def test_trailing_ampersand_is_background(self) -> None:
        self.assertTrue(is_background_shell_snippet("sleep 5 &", None))

    def test_double_ampersand_is_not_background(self) -> None:
        """**`&&` 是顺序执行，不是后台。** 混淆这两者会让 agent 以为
        命令在后台跑，实际上它在等前一条命令结束。

        **必须用「以 `&&` 结尾」的输入**：`"make && make test"` 既不结尾于
        `&`，两种实现都返回 False，测不出区别——变异因此存活。只有
        `"make &&"` 才能真正区分「有没有做 `&&` 判断」。
        """
        self.assertFalse(is_background_shell_snippet("make &&", None))
        self.assertFalse(is_background_shell_snippet("make && ", None))

    def test_single_trailing_ampersand_is_still_background(self) -> None:
        """对照组：以单个 `&` 结尾时仍要判定为后台。"""
        self.assertTrue(is_background_shell_snippet("make &", None))
        self.assertTrue(is_background_shell_snippet("make && make test &", None))

    def test_plain_command_is_not_background(self) -> None:
        self.assertFalse(is_background_shell_snippet("sleep 5", None))

    def test_args_present_disables_detection(self) -> None:
        self.assertFalse(is_background_shell_snippet("echo", ["&"]))

    def test_strip_trailing_operator(self) -> None:
        self.assertEqual(strip_trailing_background_operator("sleep 5 &"), "sleep 5")
        self.assertEqual(strip_trailing_background_operator("sleep 5 &  "), "sleep 5")

    def test_strip_leaves_double_ampersand_alone(self) -> None:
        """`&&` 只去掉一个 `&`，否则会把顺序执行改写成别的意思。"""
        self.assertEqual(strip_trailing_background_operator("a &&"), "a &")


class ShellResolutionTests(unittest.TestCase):
    """shell 解释器的解析。

    Windows 上系统自带的 `System32\\bash.exe` 是 **WSL 转发器**，不是 bash。
    用它会得到一句完全看不懂的 `WSL (11 - Relay) ERROR`。
    """

    def test_wsl_shim_is_recognised(self) -> None:
        import os as _os

        system_root = _os.environ.get("SystemRoot") or r"C:\Windows"
        shim = _os.path.join(system_root, "System32", "bash.exe")
        self.assertTrue(_is_wsl_shim(shim))
        self.assertTrue(_is_wsl_shim(shim.upper()), "大小写不该影响判定")

    def test_regular_bash_is_not_a_wsl_shim(self) -> None:
        self.assertFalse(_is_wsl_shim("/usr/bin/bash"))
        self.assertFalse(_is_wsl_shim(r"C:\Program Files\Git\bin\bash.exe"))

    def test_resolved_shell_is_never_the_wsl_shim(self) -> None:
        """**这条是修复的直接护栏。**"""
        shell = _resolve_shell()
        if shell is not None:
            self.assertFalse(
                _is_wsl_shim(shell), f"解析到了 WSL 转发器：{shell}"
            )

    def test_wsl_shim_is_rejected_when_it_is_the_only_candidate(self) -> None:
        """**WSL 转发器必须被拒绝。**

        本机装了 Git for Windows，所以 `_resolve_shell` 总是先命中 Git Bash，
        转发器那条分支走不到。要覆盖它必须把「Git Bash 不存在」这个前提构造
        出来：让候选列表为空、并让 PATH 只找到转发器。
        """
        from unittest.mock import patch

        from minicode.tools import run_command as module

        if os.name != "nt":
            self.skipTest("WSL 转发器只在 Windows 上存在")

        with patch.object(module, "_windows_bash_candidates", return_value=[]):
            with patch.object(module.shutil, "which", return_value=module._wsl_shim_path()):
                self.assertIsNone(
                    module._resolve_shell(),
                    "只剩 WSL 转发器时必须返回 None，而不是拿它去执行",
                )

    def test_real_bash_is_used_when_available(self) -> None:
        """有真 bash 时必须用它。"""
        from unittest.mock import patch

        from minicode.tools import run_command as module

        fake = r"C:\Program Files\Git\bin\bash.exe"
        with patch.object(module, "_windows_bash_candidates", return_value=[fake]):
            with patch.object(module.os.path, "isfile", return_value=True):
                self.assertEqual(module._resolve_shell(), fake)

    def test_resolved_shell_can_actually_run(self) -> None:
        """解析出来的 shell 必须真的能执行片段。"""
        shell = _resolve_shell()
        if shell is None:
            self.skipTest("本机没有可用的 POSIX shell")
        completed = subprocess.run(
            [shell, "-lc", "echo ok"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)


# ---------------------------------------------------------------------------
# 权限交互
# ---------------------------------------------------------------------------


class RecordingPrompt:
    """记录每一次审批请求，并总是允许。

    签名必须与 `PermissionPromptHandler` 一致：`(request, signal)`。
    少一个参数会抛 TypeError，而它会被 `ToolRegistry.execute` 吞成
    「工具失败」——表现为「一次审批都没发生」，很难排查。
    """

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __call__(self, request, signal=None):  # noqa: ANN001
        self.requests.append(request)
        return PermissionPromptResult(decision="allow_once")


class PermissionIntegrationTests(unittest.TestCase):
    """三种命令应当走三条不同的审批路径。

    ## 这些测试为什么必须用**仓库外**的临时工作区

    `RecordingPrompt` 总是**允许**，所以审批通过的命令会**真的执行**。

    我先后踩了两次坑，第二次比第一次隐蔽得多：

    **第一次**：测试直接跑在仓库根目录上，`git reset --hard` 真的执行，
    把未提交的改动全丢了。

    **第二次**：我把工作区改到 `tests/_tmp/rc-workspace`，以为就安全了。
    **并没有。** git 会从当前目录**向上查找 `.git`**，所以在仓库内的任何
    子目录里执行 `git reset --hard`，作用的仍然是**整个仓库**。文件又被清空
    了一次。

    正确做法是用 `tempfile.mkdtemp()` 建到**系统临时目录**——那里不在任何
    git 仓库里，`git reset --hard` 只会得到一句「not a git repository」，
    没有任何副作用。`test_workspace_is_outside_any_git_repository` 钉住这一点。
    """

    def setUp(self) -> None:
        import shutil
        import tempfile
        from unittest.mock import patch

        self._patch = patch.dict(
            os.environ, {"MINICODE_HOME": os.path.join(REPO, "tests", "_tmp", "rc-home")}
        )
        self._patch.start()
        background_tasks.reset_background_tasks()

        # **系统临时目录**，不在仓库内 —— 见类文档。
        self._temp_root = tempfile.mkdtemp(prefix="minicode-rc-")
        self.workspace = os.path.join(self._temp_root, "workspace")
        os.makedirs(self.workspace, exist_ok=True)
        self._shutil = shutil

    def tearDown(self) -> None:
        self._patch.stop()
        background_tasks.reset_background_tasks()
        self._shutil.rmtree(self._temp_root, ignore_errors=True)

    def _manager(self, prompt=None):  # noqa: ANN001
        from pathlib import Path

        store_path = Path(REPO) / "tests" / "_tmp" / "rc-permissions.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.unlink(missing_ok=True)
        manager = PermissionManager(
            str(self.workspace), prompt or RecordingPrompt(), store_path=store_path
        )
        return manager, manager._prompt

    def _run(self, data: dict, manager=None):  # noqa: ANN001
        from minicode.tools import create_default_registry

        registry = create_default_registry(str(self.workspace))
        registry.add_tools([run_command_tool])
        context = ToolContext(
            cwd=str(self.workspace), permissions=manager, signal=None
        )
        return registry.execute("run_command", data, context)

    def test_workspace_is_outside_any_git_repository(self) -> None:
        """**这条防止前面那个事故重演。**

        只要工作区落在某个 git 仓库内部，`git reset --hard` 就会作用于那个
        仓库——哪怕工作区只是它的一个深层子目录。我因此丢过两次未提交的改动。
        """
        import subprocess

        workspace = os.path.abspath(self.workspace)
        repo = os.path.abspath(REPO)
        self.assertFalse(
            workspace.startswith(repo + os.sep), f"工作区在仓库内：{workspace}"
        )
        # 直接问 git：这里是不是某个仓库的工作区？
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(
            probe.returncode,
            0,
            f"工作区仍属于某个 git 仓库：{probe.stdout.strip()}",
        )

    def test_readonly_command_is_not_prompted(self) -> None:
        """`ls` 这类每天要跑几十次，每次弹审批会让工具不可用。"""
        manager, prompt = self._manager()
        self._run({"command": "echo", "args": ["hi"]}, manager)
        self.assertEqual(prompt.requests, [], "只读命令不该请求审批")

    def test_safe_development_command_is_not_prompted(self) -> None:
        """**调用 `ensure_command` ≠ 一定弹审批。**

        `ensure_command` 只在「危险命令」或「调用方强制要求」时才真正询问。
        因此 `git status` 会被送进权限系统，但不会打扰用户。这个区别很容易
        搞错，所以单独钉一条。
        """
        manager, prompt = self._manager()
        result = self._run({"command": "git", "args": ["status"]}, manager)
        self.assertEqual(prompt.requests, [], "git status 不该请求审批")
        self.assertIn("not a git repository", result.output.lower())

    def test_dangerous_development_command_is_prompted(self) -> None:
        """危险命令必须弹审批——这是权限系统的核心价值。

        命令即使被执行也无害：工作区不在任何 git 仓库里。
        """
        manager, prompt = self._manager()
        self._run({"command": "git", "args": ["reset", "--hard"]}, manager)
        self.assertEqual(len(prompt.requests), 1, "git reset --hard 应当请求审批")

    def test_arbitrary_code_interpreter_is_prompted(self) -> None:
        """`python` / `bash` 这类能执行任意代码的命令必须审批。

        刻意用**逻辑命令名**而不是 `sys.executable` 的绝对路径：分类器认的是
        `python` / `python3` / `node` / `bash` 这些名字，给全路径它匹配不上。
        """
        manager, prompt = self._manager()
        self._run({"command": "python3", "args": ["-c", "print(1)"]}, manager)
        self.assertTrue(prompt.requests, "任意代码解释器应当请求审批")

    def test_dangerous_command_is_never_silently_allowed(self) -> None:
        """**回归护栏**：危险命令绝不能绕过审批。"""
        manager, prompt = self._manager()
        self._run({"command": "git", "args": ["clean", "-fd"]}, manager)
        self.assertTrue(prompt.requests, "git clean -fd 应当请求审批")

    def test_unknown_command_is_prompted_with_reason(self) -> None:
        manager, prompt = self._manager()
        self._run({"command": "definitely_not_a_command_xyz"}, manager)
        self.assertTrue(prompt.requests, "未知命令应当请求审批")
        details = " ".join(prompt.requests[0].details)
        self.assertIn("not in the built-in read-only/development set", details)

    def test_shell_snippet_is_prompted(self) -> None:
        """**这条守着一个我实际写错过的 bug。**

        shell 片段由 `bash` 执行，而 `bash` 属于「可执行任意本地代码」。
        但如果把解析后的**完整路径**交给权限系统，分类器就认不出 `bash`，
        片段会**静默绕过审批**。因此权限判定必须用逻辑命令名。
        """
        manager, prompt = self._manager()
        self._run({"command": "echo hi | head -1"}, manager)
        self.assertTrue(prompt.requests, "shell 片段应当请求审批")
        signature = prompt.requests[0].scope
        self.assertTrue(
            signature.startswith("bash "), f"签名应使用逻辑名 bash：{signature}"
        )

    def test_shell_snippet_approval_signature_has_no_absolute_path(self) -> None:
        """审批里显示给用户的应当是 `bash -lc ...`，而不是一长串安装路径。"""
        manager, prompt = self._manager()
        self._run({"command": "echo hi | head -1"}, manager)
        signature = prompt.requests[0].scope
        self.assertNotIn(":" + os.sep, signature, f"签名里不该出现绝对路径：{signature}")

    def test_denied_command_does_not_execute(self) -> None:
        """被拒绝的命令**绝不能真的执行**——这是权限系统的底线。"""

        def deny(request, signal=None):  # noqa: ANN001
            return PermissionPromptResult(decision="deny_once")

        target = os.path.join(self.workspace, "denied_marker.txt")
        manager, _prompt = self._manager(deny)
        result = self._run({"command": "python3", "args": ["-c", f"open(r'{target}','w')"]}, manager)
        self.assertFalse(result.ok, "被拒绝的命令不该成功")
        self.assertIn("denied", result.output.lower())
        self.assertFalse(os.path.exists(target), "被拒绝的命令不该产生副作用")


# ---------------------------------------------------------------------------
# 真实执行
# ---------------------------------------------------------------------------


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        background_tasks.reset_background_tasks()
        self.context = ToolContext(cwd=REPO, permissions=None, signal=None)

    def tearDown(self) -> None:
        background_tasks.reset_background_tasks()

    def _run(self, data: dict, context=None):  # noqa: ANN001
        return run_command_tool.run(data, context or self.context)

    def test_runs_a_command_and_returns_stdout(self) -> None:
        result = self._run({"command": PYTHON, "args": ["-c", "print('hello')"]})
        self.assertTrue(result.ok, result.output)
        self.assertIn("hello", result.output)

    def test_stderr_is_included(self) -> None:
        result = self._run(
            {"command": PYTHON, "args": ["-c", "import sys; sys.stderr.write('oops')"]}
        )
        self.assertIn("oops", result.output)

    def test_nonzero_exit_is_a_failure(self) -> None:
        result = self._run({"command": PYTHON, "args": ["-c", "raise SystemExit(3)"]})
        self.assertFalse(result.ok)
        self.assertIn("exit code 3", result.output)

    def test_nonzero_exit_is_fatal(self) -> None:
        """**与原版一致**：原版让 execFile 抛异常，最终变成 fatal。

        fatal 只在 goal/loop 模式下会终止回合，普通对话只是标记为错误。
        """
        result = self._run({"command": PYTHON, "args": ["-c", "raise SystemExit(1)"]})
        self.assertTrue(result.fatal, "非零退出应当是 fatal")

    def test_missing_command_is_a_clear_error(self) -> None:
        result = self._run({"command": "definitely_not_a_command_xyz"})
        self.assertFalse(result.ok)
        self.assertIn("not found", result.output.lower())

    def test_empty_command_is_rejected(self) -> None:
        result = self._run({"command": "   "})
        self.assertFalse(result.ok)
        self.assertIn("empty command", result.output)

    def test_cwd_argument_changes_working_directory(self) -> None:
        result = self._run(
            {"command": PYTHON, "args": ["-c", "import os; print(os.getcwd())"], "cwd": "src"}
        )
        self.assertTrue(result.ok, result.output)
        self.assertIn("src", result.output)

    def test_no_output_is_reported_explicitly(self) -> None:
        result = self._run({"command": PYTHON, "args": ["-c", "pass"]})
        self.assertTrue(result.ok)
        self.assertIn("no output", result.output)

    def test_cancellation_kills_a_long_command(self) -> None:
        """**取消必须真的杀掉子进程**，否则一条长命令会让 Ctrl-C 失效。"""
        import threading

        signal = threading.Event()
        signal.set()
        context = ToolContext(cwd=REPO, permissions=None, signal=signal)
        started = time.monotonic()
        result = self._run(
            {"command": PYTHON, "args": ["-c", "import time; time.sleep(30)"]}, context
        )
        self.assertLess(time.monotonic() - started, 20, "取消没有及时生效")
        self.assertFalse(result.ok)

    @unittest.skipIf(_resolve_shell() is None, "本机没有可用的 POSIX shell")
    def test_shell_pipeline_runs(self) -> None:
        result = self._run({"command": "echo hello | tr a-z A-Z"})
        self.assertTrue(result.ok, result.output)
        self.assertIn("HELLO", result.output)

    def test_large_output_is_truncated_with_a_note(self) -> None:
        """**与原版的有意差异。**

        原版用 Node `execFile` 的 `maxBuffer: 1MB`，超限会让整次调用**失败**。
        本实现改成截断并注明——对编码助手来说，把「输出太多」变成硬失败会让
        agent 无法处理 `pytest` 这类输出量大的正常命令。

        这里把上限调小来验证，避免真的产生 1MB 输出。
        """
        from unittest.mock import patch

        from minicode.tools import run_command as module

        with patch.object(module, "MAX_OUTPUT_BYTES", 200):
            result = self._run(
                {"command": PYTHON, "args": ["-c", "print('x' * 5000)"]}
            )
        self.assertTrue(result.ok, result.output)
        self.assertIn("已截断", result.output)
        self.assertLess(len(result.output), 1000, "超限输出应被截断")

    def test_output_under_the_limit_is_not_truncated(self) -> None:
        from unittest.mock import patch

        from minicode.tools import run_command as module

        with patch.object(module, "MAX_OUTPUT_BYTES", 1_000_000):
            result = self._run({"command": PYTHON, "args": ["-c", "print('short')"]})
        self.assertTrue(result.ok)
        self.assertNotIn("已截断", result.output)


class BackgroundExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        background_tasks.reset_background_tasks()
        self.context = ToolContext(cwd=REPO, permissions=None, signal=None)

    def tearDown(self) -> None:
        for task in background_tasks.list_background_tasks():
            try:
                os.kill(task.pid, 9)
            except (OSError, AttributeError):
                pass
        background_tasks.reset_background_tasks()

    def _run(self, data: dict):  # noqa: ANN001
        return run_command_tool.run(data, self.context)

    @unittest.skipIf(_resolve_shell() is None, "本机没有可用的 POSIX shell")
    def test_background_snippet_returns_immediately(self) -> None:
        """**不能等**——后台执行的意义就是立刻交回控制权。"""
        started = time.monotonic()
        result = self._run({"command": "sleep 5 &"})
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok, result.output)
        self.assertLess(elapsed, 2.0, "后台执行不该等待命令结束")

    @unittest.skipIf(_resolve_shell() is None, "本机没有可用的 POSIX shell")
    def test_background_snippet_registers_a_task(self) -> None:
        result = self._run({"command": "sleep 5 &"})
        self.assertIsNotNone(result.background_task)
        task = result.background_task
        self.assertEqual(task.type, "local_bash")
        self.assertEqual(task.command, "sleep 5", "结尾的 & 应被去掉")
        self.assertGreater(task.pid, 0)
        self.assertIn("TASK:", result.output)
        self.assertIn("PID:", result.output)

    @unittest.skipIf(_resolve_shell() is None, "本机没有可用的 POSIX shell")
    def test_registered_task_can_be_looked_up(self) -> None:
        result = self._run({"command": "sleep 5 &"})
        task_id = result.background_task.task_id
        found = background_tasks.get_background_task(task_id)
        self.assertIsNotNone(found)
        self.assertEqual(found.task_id, task_id)

    def test_unknown_task_id_returns_none(self) -> None:
        self.assertIsNone(background_tasks.get_background_task("shell_nope_000000"))


# ---------------------------------------------------------------------------
# 后台任务注册表
# ---------------------------------------------------------------------------


class BackgroundTaskRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        background_tasks.reset_background_tasks()

    def tearDown(self) -> None:
        background_tasks.reset_background_tasks()

    def test_task_id_format(self) -> None:
        task = background_tasks.register_background_shell_task(
            command="x", pid=1, cwd="/tmp"
        )
        self.assertTrue(task.task_id.startswith("shell_"))
        self.assertEqual(task.task_id.count("_"), 2)

    def test_task_ids_are_unique(self) -> None:
        ids = {
            background_tasks.register_background_shell_task(
                command="x", pid=1, cwd="/tmp"
            ).task_id
            for _ in range(20)
        }
        self.assertEqual(len(ids), 20)

    def test_current_process_is_alive(self) -> None:
        self.assertTrue(background_tasks.is_process_alive(os.getpid()))

    def test_finished_process_is_not_alive(self) -> None:
        child = subprocess.Popen([PYTHON, "-c", "pass"])
        child.wait()
        self.assertFalse(
            background_tasks.is_process_alive(child.pid),
            "已退出的进程不该被判为存活",
        )

    def test_nonpositive_pid_is_unknown(self) -> None:
        self.assertIsNone(background_tasks.is_process_alive(0))

    def test_completed_task_is_not_reprobed(self) -> None:
        """**终态不该被重新探测。**

        否则 PID 被系统复用后，一个已完成的任务会被错标回 running。
        """
        from dataclasses import replace

        task = background_tasks.register_background_shell_task(
            command="x", pid=os.getpid(), cwd="/tmp"
        )
        done = replace(task, status="completed")
        from minicode.background_tasks import _refresh

        self.assertEqual(_refresh(done).status, "completed")

    def test_running_task_with_dead_process_becomes_completed(self) -> None:
        from minicode.background_tasks import _refresh

        child = subprocess.Popen([PYTHON, "-c", "pass"])
        child.wait()
        task = background_tasks.register_background_shell_task(
            command="x", pid=child.pid, cwd="/tmp"
        )
        self.assertEqual(_refresh(task).status, "completed")

    def test_list_refreshes_status(self) -> None:
        child = subprocess.Popen([PYTHON, "-c", "pass"])
        child.wait()
        background_tasks.register_background_shell_task(
            command="x", pid=child.pid, cwd="/tmp"
        )
        listed = background_tasks.list_background_tasks()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].status, "completed")

    def test_reset_clears_the_registry(self) -> None:
        background_tasks.register_background_shell_task(command="x", pid=1, cwd="/tmp")
        background_tasks.reset_background_tasks()
        self.assertEqual(background_tasks.list_background_tasks(), [])


class ToolSchemaTests(unittest.TestCase):
    def test_schema_shape_matches_the_original(self) -> None:
        schema = run_command_tool.input_schema
        self.assertEqual(schema["required"], ["command"])
        self.assertEqual(
            sorted(schema["properties"]), ["args", "command", "cwd"]
        )

    def test_validator_accepts_minimal_input(self) -> None:
        self.assertEqual(run_command_tool.validator({"command": "ls"}), {"command": "ls"})

    def test_validator_drops_empty_optional_fields(self) -> None:
        parsed = run_command_tool.validator(
            {"command": "ls", "args": [], "cwd": ""}
        )
        self.assertEqual(parsed, {"command": "ls"})

    def test_validator_coerces_args_to_strings(self) -> None:
        parsed = run_command_tool.validator({"command": "ls", "args": [1, 2]})
        self.assertEqual(parsed["args"], ["1", "2"])

    def test_name_is_stable(self) -> None:
        self.assertEqual(run_command_tool.name, "run_command")


if __name__ == "__main__":
    unittest.main(verbosity=2)
