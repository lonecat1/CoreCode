"""本地安装器（`install.ts`）的测试。

## 测什么

安装器的价值在「**不用手写 JSON**」——键名、嵌套结构、以及「模型名要写两处」
这些细节靠记忆很容易漏一处，而漏了的症状是「配置看起来对了但没生效」。

因此测试的重点是**写出来的配置内容正确**，而不是「交互流程跑通了」。

## 一个直接的教训

"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.install import (  # noqa: E402
    ask_required,
    default_bin_dir,
    has_path_entry,
    install_local,
    launcher_scripts,
)

class InstallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="corecode-install-home-"))
        self.bin_dir = Path(tempfile.mkdtemp(prefix="corecode-install-bin-"))
        self._env = patch.dict(
            os.environ, {"CORECODE_HOME": str(self.home), "PATH": ""}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.bin_dir, ignore_errors=True)

    def settings(self) -> dict:
        return json.loads((self.home / "settings.json").read_text(encoding="utf-8"))

    def run_install(self, answers: list[str], **kwargs):
        """用预设答案跑一次安装，返回 `(摘要, 输出)`。"""
        queue = list(answers)
        stream = io.StringIO()

        def read_line() -> str | None:
            return queue.pop(0) if queue else None

        result = install_local(
            read_line=read_line,
            stream=stream,
            bin_dir=self.bin_dir,
            repo_root=Path(tempfile.gettempdir()),
            **kwargs,
        )
        return result, stream.getvalue()

class AskRequiredTests(unittest.TestCase):
    def _ask(self, answers: list[str], default: str = "") -> str:
        queue = list(answers)
        stream = io.StringIO()

        def read_line() -> str | None:
            return queue.pop(0) if queue else None

        with patch("sys.stdout", stream):
            return ask_required(read_line, "Label", default, stream=stream)

    def test_returns_the_answer(self) -> None:
        self.assertEqual(self._ask(["hello"]), "hello")

    def test_uses_the_default_on_blank(self) -> None:
        self.assertEqual(self._ask(["", "x"], default="d"), "d")

    def test_retries_until_non_empty(self) -> None:
        """**空白答案回退到默认值，都没有就重问。**

        直接接受空值会让 `ANTHROPIC_BASE_URL` 变成空字符串，而后续报错指向
        「请求失败」而不是「你没填这一项」。
        """
        self.assertEqual(self._ask(["", "", "ok"]), "ok")

    def test_eof_returns_the_default(self) -> None:
        """EOF 时不再重问——否则管道输入会死循环。"""
        self.assertEqual(self._ask([], default="fallback"), "fallback")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(self._ask(["  spaced  "]), "spaced")

    def test_default_is_shown_in_the_prompt(self) -> None:
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            ask_required(lambda: "x", "Label", "d", stream=stream)
        self.assertIn("[d]", stream.getvalue())

    def test_no_default_has_no_bracket(self) -> None:
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            ask_required(lambda: "x", "Label", stream=stream)
        self.assertNotIn("[", stream.getvalue())

class PathEntryTests(unittest.TestCase):
    def test_detects_present_entry(self) -> None:
        target = Path(tempfile.gettempdir())
        with patch.dict(os.environ, {"PATH": str(target)}):
            self.assertTrue(has_path_entry(target))

    def test_detects_missing_entry(self) -> None:
        with patch.dict(os.environ, {"PATH": str(Path(tempfile.gettempdir()) / "nope")}):
            self.assertFalse(has_path_entry(Path(tempfile.gettempdir()) / "other"))

    def test_empty_path(self) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertFalse(has_path_entry(Path(tempfile.gettempdir())))

    def test_uses_the_platform_separator(self) -> None:
        """**必须用 `os.pathsep`，不能写死 `:`。**

        """
        first = tempfile.gettempdir()
        second = str(Path(first) / "second")
        joined = os.pathsep.join([first, second])
        with patch.dict(os.environ, {"PATH": joined}):
            self.assertTrue(has_path_entry(Path(first)))
            self.assertTrue(has_path_entry(Path(second)))

    def test_trailing_separator_is_tolerated(self) -> None:
        target = Path(tempfile.gettempdir())
        with patch.dict(os.environ, {"PATH": f"{target}{os.sep}"}):
            self.assertTrue(has_path_entry(target))

    def test_case_insensitive_on_windows(self) -> None:
        if platform.system() != "Windows":
            self.skipTest("只在 Windows 上讨论大小写")
        target = Path(tempfile.gettempdir())
        with patch.dict(os.environ, {"PATH": str(target).upper()}):
            self.assertTrue(has_path_entry(target))

class DefaultBinDirTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        custom = Path(tempfile.gettempdir()) / "custom-bin"
        with patch.dict(os.environ, {"CORECODE_BIN_DIR": str(custom)}):
            self.assertEqual(default_bin_dir(), custom.resolve())

    def test_platform_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            result = default_bin_dir()
        self.assertTrue(result.is_absolute())
        if platform.system() == "Windows":
            self.assertEqual(result, Path.home() / "bin")
        else:
            self.assertEqual(result, Path.home() / ".local" / "bin")

class LauncherTests(unittest.TestCase):
    def test_generates_three_files(self) -> None:
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        self.assertEqual(set(scripts), {"corecode", "corecode.cmd", "corecode.ps1"})

    def test_posix_launcher_is_a_shell_script(self) -> None:
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        self.assertTrue(scripts["corecode"].startswith("#!/usr/bin/env sh"))

    def test_cmd_launcher_uses_crlf(self) -> None:
        """`.cmd` 的**每一行**都必须是 CRLF，不能有孤立的 LF。

        早先这里只断言「内容里有 `\\r\\n`」——太弱：把其中一行改成 LF，其余行
        仍然带 `\\r\\n`，断言照样通过。而 `.cmd` 的解析器对混合行尾很敏感：
        被拆开的那一行可能被当成两条命令。

        判据改成「不存任何**前面不是 `\\r`** 的 `\\n`」。
        """
        content = launcher_scripts(Path(tempfile.gettempdir()))["corecode.cmd"]
        stray = [
            index for index, char in enumerate(content)
            if char == "\n" and (index == 0 or content[index - 1] != "\r")
        ]
        self.assertEqual(stray, [], f"第 {stray} 个字符处的换行不是 CRLF")

    def test_launchers_run_the_module_entry(self) -> None:
        """**指向 `python -m corecode.cli` 而不是某个 `.py` 文件。**

        """
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        for name, content in scripts.items():
            self.assertIn("-m corecode.cli", content, name)

    def test_launchers_quote_the_interpreter(self) -> None:
        """解释器路径可能含空格（Windows 的 `Program Files`）。"""
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        self.assertIn(f'"{sys.executable}"', scripts["corecode"])

    def test_launchers_are_pure_ascii(self) -> None:
        """**启动器必须纯 ASCII。**

        Windows PowerShell 5.1 读取无 BOM 的 UTF-8 文件时按 ANSI 解码，非 ASCII
        字符会乱码。注释乱码不影响执行，但一旦有人把非 ASCII 写进**代码**就会
        真的出问题。launcher 保持纯 ASCII 最稳。
        """
        for name, content in launcher_scripts(Path(tempfile.gettempdir())).items():
            with self.subTest(launcher=name):
                content.encode("ascii")  # 抛 UnicodeEncodeError 即失败

    # ------------------------------------------------------------------
    # 启动器必须自己设 `PYTHONPATH`
    #
    # **这一组是补的，因为真实发生过事故**：早先 `launcher_scripts(repo_root)`
    # 收下了 `repo_root` 却**从未使用**，生成的脚本里只有
    # `python -m corecode.cli`。本项目是 `src/` 布局，从源码使用时一执行就是：
    #
    #     ModuleNotFoundError: No module named 'corecode'
    #
    # 而这个错误的可诊断性极差——用户会以为「没装」，实际装的是**启动器有问题**。
    # ------------------------------------------------------------------

    def test_every_launcher_puts_src_on_pythonpath(self) -> None:
        repo = Path(tempfile.gettempdir()) / "fake-repo"
        scripts = launcher_scripts(repo)
        expected = str(repo / "src")
        for name, content in scripts.items():
            with self.subTest(launcher=name):
                self.assertIn("PYTHONPATH", content, name)
                self.assertIn(expected, content, name)

    def test_pythonpath_stays_visible_to_the_interpreter(self) -> None:
        """变量必须真的被**导出**。

        POSIX 脚本里少一句 `export`，子进程就看不到它——那是「写进文本但没生效」
        的典型形态，光断言字符串存在查不出来。
        """
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        self.assertIn("export PYTHONPATH", scripts["corecode"])
        self.assertIn("setlocal", scripts["corecode.cmd"])
        self.assertIn("$env:PYTHONPATH", scripts["corecode.ps1"])

    def test_repo_src_wins_over_existing_pythonpath(self) -> None:
        """仓库的 `src/` 必须排在**前面**。

        排在后面时，任何已安装的同名包会先命中——用户改了仓库代码却看不到效果，
        而且不会有任何报错。
        """
        repo = Path(tempfile.gettempdir()) / "fake-repo"
        src = str(repo / "src")
        scripts = launcher_scripts(repo)
        self.assertLess(
            scripts["corecode"].index(src),
            scripts["corecode"].index("${PYTHONPATH"),
        )
        self.assertLess(
            scripts["corecode.cmd"].index(src),
            scripts["corecode.cmd"].index("%PYTHONPATH%"),
        )
        # ps1 里 `$env:PYTHONPATH` 在赋值**左边**也出现一次，位置更靠前。
        # 要比较的是右值那一处（`+ $env:PYTHONPATH`），否则这条断言恒假。
        self.assertLess(
            scripts["corecode.ps1"].index(src),
            scripts["corecode.ps1"].index("+ $env:PYTHONPATH"),
        )

    def test_ps1_propagates_the_exit_code(self) -> None:
        """PowerShell 里 `&` 之后脚本会继续走；不显式 `exit`，退出码会被吞掉。

        后果很具体：`corecode --help; echo $LASTEXITCODE` 恒为 0，
        CI 判断不出成败。
        """
        scripts = launcher_scripts(Path(tempfile.gettempdir()))
        self.assertIn("$LASTEXITCODE", scripts["corecode.ps1"])
        self.assertIn("exit $code", scripts["corecode.ps1"])

    @unittest.skipUnless(os.name == "nt", "只有 Windows 需要验 .cmd")
    def test_generated_cmd_actually_runs_from_source(self) -> None:
        """**端到端**：把生成的 `.cmd` 真的跑起来。

        这条直接复现用户遇到的场景——环境里**没有** `PYTHONPATH`，只有启动器
        自己补的那一份。跑 `--help`，不进入 REPL、不读用户配置。
        """
        repo_root = Path(__file__).resolve().parent.parent
        scripts = launcher_scripts(repo_root)
        with tempfile.TemporaryDirectory(prefix="launcher-e2e-") as tmp:
            target = Path(tmp) / "corecode.cmd"
            target.write_text(scripts["corecode.cmd"], encoding="utf-8", newline="")
            env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
            proc = subprocess.run(
                ["cmd", "/c", str(target), "--help"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=120,
            )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined[:600])
        self.assertNotIn("No module named", combined)
        # 所以输出是管理命令清单，不是主 CLI 帮助。
        self.assertIn("corecode mcp", combined)

class InstallFlowTests(InstallTestCase):
    def test_writes_model_and_env(self) -> None:
        self.run_install(["my-model", "https://api.example.com", "tok-123"])
        settings = self.settings()
        self.assertEqual(settings["model"], "my-model")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://api.example.com")
        self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], "tok-123")

    def test_model_is_written_in_both_places(self) -> None:
        """**模型名要写两处**：顶层 `model` 与 `env.ANTHROPIC_MODEL`。

        只写一处时配置看起来是对的，但实际生效的那个可能不是你填的
        ——这正是「手写 JSON 容易漏」的典型。
        """
        self.run_install(["m", "https://x", "t"])
        settings = self.settings()
        self.assertEqual(settings["model"], "m")
        self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "m")

    def test_existing_settings_are_preserved(self) -> None:
        (self.home / "settings.json").write_text(
            json.dumps({"maxOutputTokens": 16000, "model": "old"}), encoding="utf-8"
        )
        self.run_install(["new", "https://x", "t"])
        settings = self.settings()
        self.assertEqual(settings["maxOutputTokens"], 16000)
        self.assertEqual(settings["model"], "new")

    def test_existing_token_is_reused_on_blank_input(self) -> None:
        """留空表示「沿用已保存的 token」，而不是清空它。"""
        (self.home / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "saved-token"}}),
            encoding="utf-8",
        )
        self.run_install(["m", "https://x", ""])
        self.assertEqual(self.settings()["env"]["ANTHROPIC_AUTH_TOKEN"], "saved-token")

    def test_existing_values_become_defaults(self) -> None:
        (self.home / "settings.json").write_text(
            json.dumps(
                {"model": "old-model", "env": {"ANTHROPIC_BASE_URL": "https://old"}}
            ),
            encoding="utf-8",
        )
        # 两处直接回车 = 接受默认值
        self.run_install(["", "", "t"])
        settings = self.settings()
        self.assertEqual(settings["model"], "old-model")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://old")

    def test_missing_token_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.run_install(["m", "https://x", ""])
        self.assertIn("ANTHROPIC_AUTH_TOKEN", str(caught.exception))

    def test_creates_launchers(self) -> None:
        result, _ = self.run_install(["m", "https://x", "t"])
        self.assertEqual(len(result["launchers"]), 3)
        for path in result["launchers"]:
            self.assertTrue(Path(path).exists(), path)

    def test_creates_the_bin_directory_when_missing(self) -> None:
        """**目录不存在时必须自己建。**

        这条容易漏测：`tempfile.mkdtemp()` 建出来的目录**已经存在**，于是
        `mkdir` 从不被需要，把那一行删掉测试也照样过。而真实场景里
        `~/bin` / `~/.local/bin` 常常**根本不存在**——缺了 `mkdir` 会直接
        FileNotFoundError，安装器在最常见的路径上失败。
        """
        missing = self.bin_dir / "does" / "not" / "exist"
        self.assertFalse(missing.exists())

        stream = io.StringIO()
        queue = ["m", "https://x", "t"]
        result = install_local(
            read_line=lambda: queue.pop(0) if queue else None,
            stream=stream,
            bin_dir=missing,
            repo_root=Path(tempfile.gettempdir()),
        )
        self.assertTrue(missing.is_dir())
        for path in result["launchers"]:
            self.assertTrue(Path(path).exists(), path)

    def test_launcher_is_marked_executable(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("Windows 不靠权限位")
        result, _ = self.run_install(["m", "https://x", "t"])
        posix = [p for p in result["launchers"] if p.endswith("corecode")][0]
        self.assertTrue(os.access(posix, os.X_OK))

    def test_can_skip_launchers(self) -> None:
        result, _ = self.run_install(["m", "https://x", "t"], write_launcher=False)
        self.assertEqual(result["launchers"], [])

    def test_reports_install_result(self) -> None:
        result, output = self.run_install(["m", "https://x", "t"])
        self.assertEqual(result["model"], "m")
        self.assertEqual(result["base_url"], "https://x")
        self.assertIn("安装完成", output)
        self.assertIn(str(self.home / "settings.json"), output)

    def test_never_prints_the_token(self) -> None:
        """**token 不能被回显。** 终端输出会进会话记录与日志。"""
        _, output = self.run_install(["m", "https://x", "super-secret-token"])
        self.assertNotIn("super-secret-token", output)

    def test_token_prompt_shows_status_not_value(self) -> None:
        _, output = self.run_install(["m", "https://x", "t"])
        self.assertIn("[not set]", output)

    def test_token_prompt_says_saved_when_present(self) -> None:
        (self.home / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}}), encoding="utf-8"
        )
        _, output = self.run_install(["m", "https://x", "new"])
        self.assertIn("[saved]", output)

class PathHintTests(InstallTestCase):
    def test_hint_is_shown_when_not_on_path(self) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            result, output = self.run_install(["m", "https://x", "t"])
        self.assertFalse(result["on_path"])
        self.assertIn("PATH", output)

    def test_success_message_when_on_path(self) -> None:
        with patch.dict(os.environ, {"PATH": str(self.bin_dir)}):
            result, output = self.run_install(["m", "https://x", "t"])
        self.assertTrue(result["on_path"])
        self.assertIn("任意终端", output)

    def test_hint_matches_the_platform(self) -> None:
        with patch.dict(os.environ, {"PATH": ""}):
            _, output = self.run_install(["m", "https://x", "t"])
        if platform.system() == "Windows":
            self.assertIn("PowerShell", output)
        else:
            self.assertIn("bashrc", output)

if __name__ == "__main__":
    unittest.main(verbosity=2)
