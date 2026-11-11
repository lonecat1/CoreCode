"""MCP 状态汇总与 `/model`、`/config-paths` 命令的测试。

"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.mcp.status import (  # noqa: E402
    McpStatusSummary,
    summarize_mcp_servers,
)
from corecode.tooling import ToolRegistry  # noqa: E402
from corecode.stats import SessionStats  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def server(name: str, status: str, tools: int = 0, **extra: object) -> dict:
    return {"name": name, "status": status, "toolCount": tools, **extra}

class SummarizeTests(unittest.TestCase):
    def test_empty_list(self) -> None:
        summary = summarize_mcp_servers([])
        self.assertEqual(summary.total, 0)
        self.assertEqual(summary.line(), "", "没有服务器时不该显示 MCP 行")

    def test_none_is_treated_as_empty(self) -> None:
        self.assertEqual(summarize_mcp_servers(None).total, 0)

    def test_counts_each_status(self) -> None:
        summary = summarize_mcp_servers(
            [
                server("a", "connected", 2),
                server("b", "connected", 3),
                server("c", "connecting"),
                server("d", "error"),
            ]
        )
        self.assertEqual(summary.total, 4)
        self.assertEqual(summary.connected, 2)
        self.assertEqual(summary.connecting, 1)
        self.assertEqual(summary.error, 1)
        self.assertEqual(summary.tool_count, 5)

    def test_unknown_status_counts_in_total_only(self) -> None:
        """**未知状态计入 total，但不计入任何一个分类。**

        这样「总数对得上」永远成立，而未知状态会在
        `total - connected - connecting - error` 里显形，便于排查。
        把未知状态悄悄归到某一类会让数字看起来正常但其实是错的。
        """
        summary = summarize_mcp_servers([server("x", "weird")])
        self.assertEqual(summary.total, 1)
        self.assertEqual(
            summary.connected + summary.connecting + summary.error, 0
        )

    def test_tool_count_sums(self) -> None:
        summary = summarize_mcp_servers(
            [server("a", "connected", 7), server("b", "error", 3)]
        )
        self.assertEqual(summary.tool_count, 10)

    def test_missing_tool_count_is_zero(self) -> None:
        summary = summarize_mcp_servers([{"name": "a", "status": "connected"}])
        self.assertEqual(summary.tool_count, 0)

    def test_negative_tool_count_is_clamped(self) -> None:
        """负数的工具数没有意义，钳到 0 而不是让总数变小。"""
        summary = summarize_mcp_servers([server("a", "connected", -5)])
        self.assertEqual(summary.tool_count, 0)

    def test_boolean_tool_count_is_ignored(self) -> None:
        """`True` 是 `int` 的子类，但它不是计数——不能算成 1。"""
        summary = summarize_mcp_servers(
            [{"name": "a", "status": "connected", "toolCount": True}]
        )
        self.assertEqual(summary.tool_count, 0)

    def test_non_dict_entries_are_skipped(self) -> None:
        summary = summarize_mcp_servers(["nonsense", None, 42])
        self.assertEqual(summary.total, 0)

    def test_line_format_connected_only(self) -> None:
        self.assertEqual(
            summarize_mcp_servers([server("a", "connected")]).line(), "mcp 1/1"
        )

    def test_line_format_with_connecting(self) -> None:
        summary = summarize_mcp_servers(
            [server("a", "connected"), server("b", "connecting")]
        )
        self.assertEqual(summary.line(), "mcp 1/2 · 1 等待")

    def test_line_format_with_error(self) -> None:
        summary = summarize_mcp_servers(
            [server("a", "connected"), server("b", "error")]
        )
        self.assertEqual(summary.line(), "mcp 1/2 · 1 失败")

    def test_line_format_with_both(self) -> None:
        summary = summarize_mcp_servers(
            [
                server("a", "connected"),
                server("b", "connecting"),
                server("c", "error"),
            ]
        )
        self.assertEqual(summary.line(), "mcp 1/3 · 1 等待 · 1 失败")

    def test_to_dict_uses_camel_case_tool_count(self) -> None:
        """`toolCount` 用驼峰——它要与 MCP 快照的字段名一致。"""
        summary = summarize_mcp_servers([server("a", "connected", 4)])
        self.assertEqual(
            summary.to_dict(),
            {
                "total": 1,
                "connected": 1,
                "connecting": 0,
                "error": 0,
                "toolCount": 4,
            },
        )

    def test_default_summary_is_all_zero(self) -> None:
        summary = McpStatusSummary()
        self.assertEqual(
            (summary.total, summary.connected, summary.connecting, summary.error),
            (0, 0, 0, 0),
        )

class BannerIntegrationTests(unittest.TestCase):
    def test_banner_shows_mcp_line(self) -> None:
        from corecode.ui.render import render_banner

        # （`tui/chrome.ts` 的 `colorBadge('mcp', ...)`），不再是键值行。
        text = render_banner(
            model="m",
            session_id="s",
            cwd="/tmp",
            mcp_total=2,
            mcp_connected=1,
            mcp_error=1,
            width=200,
        )
        self.assertIn("[mcp]", text)
        self.assertIn("1/2", text)
        self.assertIn("[mcp-err]", text)

    def test_banner_omits_mcp_row_when_empty(self) -> None:
        """没有 MCP 服务器时不该多出一行空标签。"""
        from corecode.ui.render import render_banner

        text = render_banner(model="m", session_id="s", cwd="/tmp", width=80)
        self.assertNotIn("MCP", text)

    def test_banner_stays_within_width(self) -> None:
        from corecode.ui.width import string_display_width
        from corecode.ui.render import render_banner

        # 所以这里断言的是「所有行等宽且不小于 60」，而不是「不超过请求的宽度」。
        text = render_banner(
            model="m",
            session_id="s",
            cwd="/tmp",
            mcp_total=3,
            mcp_connected=1,
            mcp_connecting=1,
            mcp_error=1,
            width=40,
        )
        widths = {string_display_width(line) for line in text.splitlines()}
        self.assertEqual(widths, {60}, "宽度被 max(60, …) 抬到 60，且所有行等宽")

class ConfigPathsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="corecode-paths-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, text: str, home: Path | None = None) -> str | None:
        from corecode.cli_commands import try_handle_local_command

        with patch.dict(os.environ, {"CORECODE_HOME": str(home or self.home)}):
            return try_handle_local_command(None, text)  # type: ignore[arg-type]

    @staticmethod
    def _joined(output: str) -> str:
        """把折行的续行接回一行，便于断言完整路径。

        `/config-paths` 对过长的路径会**折行**（不是截断），所以完整路径在
        原始输出里可能跨两行。断言前必须先接回去——否则测试会因为终端宽度
        而随机失败，而那是测试的问题不是实现的问题。
        """
        import re

        return re.sub(r"\n\s*", "", output)

    def test_lists_four_paths(self) -> None:
        output = self._run("/config-paths")
        self.assertIsNotNone(output)
        self.assertIn("corecode settings", output)
        self.assertIn("corecode permissions", output)
        self.assertIn("corecode mcp", output)
        self.assertIn("compat fallback", output)

    def test_shows_the_real_claude_path(self) -> None:
        from corecode.config import claude_settings_path

        output = self._run("/config-paths") or ""
        self.assertIn(str(claude_settings_path()), self._joined(output))

    def test_shows_settings_path(self) -> None:
        """路径必须来自**动态**访问器。

        这条同时是「不要用导入期快照」的护栏：`_render_config_paths` 用的是
        `settings_path()`，因此在这里改 `CORECODE_HOME` 后它立刻跟着变。
        如果哪天有人换成模块级 `SETTINGS_PATH`，这条会失败。
        """
        from corecode.config import settings_path

        with patch.dict(os.environ, {"CORECODE_HOME": str(self.home)}):
            expected = str(settings_path())
        output = self._run("/config-paths") or ""
        self.assertIn(expected, self._joined(output))

    def test_paths_follow_corecode_home(self) -> None:
        """换一个家目录，显示的三条本项目路径都要跟着换。"""
        other = Path(tempfile.mkdtemp(prefix="corecode-paths2-"))
        try:
            output = self._run("/config-paths", home=other) or ""
            self.assertIn(str(other), self._joined(output))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_long_paths_wrap_instead_of_being_truncated(self) -> None:
        """**这条钉住一个真实缺陷。**

        `/config-paths` 的全部意义是让用户看到完整路径去复制。用默认的
        「截断」渲染会得到 `C:\\Users\\...\\se...`——这条命令就白做了。
        必须折行，且折行后接回来仍是完整路径。
        """
        deep = Path(tempfile.mkdtemp(prefix="corecode-very-long-home-name-"))
        try:
            output = self._run("/config-paths", home=deep) or ""
            self.assertNotIn("...", output, "不该出现截断省略号")
            joined = self._joined(output)
            self.assertIn(str(deep / "settings.json"), joined)
            self.assertIn(str(deep / "permissions.json"), joined)
            self.assertIn(str(deep / "mcp.json"), joined)
        finally:
            shutil.rmtree(deep, ignore_errors=True)

    def test_paths_are_never_wrapped_or_truncated(self) -> None:
        """**长路径就该占满一行，不折行也不截断。**

        为什么这是对的：这条命令的**全部意义**是让用户看到完整路径去复制。
        折行会把路径劈成两段（复制时要手工接回去），截断则直接毁掉它。
        """
        from corecode.config import settings_path
        from corecode.ui.width import string_display_width

        output = self._run("/config-paths") or ""
        expected = str(settings_path())
        if string_display_width(f"corecode settings: {expected}") <= 80:
            self.skipTest("这台机器上的路径不够长，测不出「不折行」")

        # 完整路径必须出现在**同一行**上
        self.assertTrue(
            any(line.endswith(expected) for line in output.splitlines()),
            f"路径被折行或截断了：\n{output}",
        )
        self.assertNotIn("...", output, "不该出现截断省略号")

    def test_output_is_four_flat_label_value_lines(self) -> None:
        """形状是**四行 `label: value`**，没有标题、没有分隔线、没有表格对齐。

        脚本会 `grep 'corecode mcp'` 找配置在哪，形状一变就 grep 不到。
        """
        output = self._run("/config-paths") or ""
        labelled = [
            line for line in output.splitlines()
            if line.startswith(
                ("corecode settings:", "corecode permissions:",
                 "corecode mcp:", "compat fallback:")
            )
        ]
        self.assertEqual(len(labelled), 4, output)
        for line in labelled:
            with self.subTest(line=line[:40]):
                self.assertIn(": ", line, "必须是 `label: value` 形状")
        # 不该有表格化的痕迹（标题行、分隔线）
        self.assertNotIn("配置路径", output)
        self.assertNotIn("───", output)

class ModelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="corecode-model-"))
        self.home = self.root / "mini-home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings = self.home / "settings.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, text: str, model: str | None = None) -> str | None:
        from types import SimpleNamespace

        from corecode.cli_commands import try_handle_local_command

        # 用 SimpleNamespace 而不是内联 class：类体不构成闭包，写
        # `model = model or ...` 会得到 NameError（我第一版就这么错了）。
        host = SimpleNamespace(
            runtime_config=SimpleNamespace(model=model) if model is not None else None
        )
        with patch.dict(os.environ, {"CORECODE_HOME": str(self.home)}):
            with patch("corecode.config.settings_path", return_value=self.settings):
                return try_handle_local_command(host, text)  # type: ignore[arg-type]

    def test_shows_current_model(self) -> None:
        output = self._run("/model", model="qwen3.8-max")
        self.assertIn("current model: qwen3.8-max", output or "")

    def test_shows_offline_placeholder_without_runtime_config(self) -> None:
        output = self._run("/model")
        self.assertIn("离线 mock", output or "")

    def test_saves_model_override(self) -> None:
        output = self._run("/model new-model")
        self.assertIn("saved model=new-model", output or "")
        written = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "new-model")

    def test_save_merges_instead_of_overwriting(self) -> None:
        """**必须合并**：只改 `model` 一个键，其余配置原样保留。

        整份覆盖会让用户丢掉端点、密钥等一切——而 `/model` 看起来只是换个
        模型名，那种破坏完全超出预期。
        """
        self.settings.write_text(
            json.dumps(
                {
                    "model": "old",
                    "env": {"ANTHROPIC_API_KEY": "sk-keep"},
                    "maxOutputTokens": 1234,
                }
            ),
            encoding="utf-8",
        )
        self._run("/model new-model")
        written = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "new-model")
        self.assertEqual(written["env"]["ANTHROPIC_API_KEY"], "sk-keep")
        self.assertEqual(written["maxOutputTokens"], 1234)

    def test_creates_settings_file_if_missing(self) -> None:
        self.assertFalse(self.settings.exists())
        self._run("/model fresh")
        self.assertTrue(self.settings.exists())

    def test_trailing_space_shows_current_model(self) -> None:
        """`/model ` 的尾随空格会被 `partition` 后的 strip 吃掉。

        因此它与 `/model` 走同一条分支——都显示当前模型。这是合理的：
        在两种情况下「显示当前模型」都是用户想看的。
        """
        output = self._run("/model   ", model="m")
        self.assertIn("current model: m", output or "")

    def test_model_name_is_trimmed(self) -> None:
        self._run("/model    padded-name   ")
        written = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "padded-name")

class CommandRegistryTests(unittest.TestCase):
    """命令清单的完整性。

    ## 计数口径很重要

    因此「用法条数」不是对等指标（多一条少一条都可能只是措辞不同），
    **不同的命令名**才是。我一开始就是按用法条数比的，得出了错误的结论。
    """

    TS_COMMANDS = frozenset(
        {
            "/loop", "/goal", "/plan", "/help", "/tools", "/status", "/model",
            "/config-paths", "/skills", "/mcp", "/resume", "/rename", "/new",
            "/fork", "/permissions", "/exit", "/ls", "/grep", "/read", "/write",
            "/modify", "/edit", "/patch", "/cmd", "/compact", "/collapse",
            "/snip", "/init", "/memory",
        }
    )

    def _python_commands(self) -> set[str]:
        from corecode.cli_commands import SLASH_COMMANDS

        return {item.usage.split(" ")[0] for item in SLASH_COMMANDS}

    def test_ts_has_29_distinct_commands(self) -> None:
        self.assertEqual(len(self.TS_COMMANDS), 29)

    def test_no_commands_are_missing(self) -> None:
        """**命令层已完全对等：29 / 29。**

        这条测试是「缺口清单」的可执行版本。它先前断言缺口恰好是 `{"/init"}`，
        补齐 `/init` 后如期失败——正是设计意图：**补齐一个缺口必须同时更新
        这里的断言**，否则清单会悄悄过期。
        """
        missing = self.TS_COMMANDS - self._python_commands()
        self.assertEqual(missing, set(), f"还有命令没实现：{sorted(missing)}")

    def test_all_29_commands_are_present(self) -> None:
        self.assertEqual(len(self._python_commands()), 29)

    def test_probing_commands_does_not_touch_the_repository(self) -> None:
        """**这条防止我自己犯过的一个错误重演。**

        我曾在 `cwd=REPO` 上把帮助里的每条命令都跑一遍来确认「都有处理函数」。
        `/init` 会真的创建 `.corecode/`、往 `.gitignore` 追加条目、写出
        `CORECODE.md`——于是**测试往真实仓库里写了文件**。

        这类测试破坏是隐性的：它不报错，只是悄悄改动工作区，直到你
        `git status` 才发现。因此这里断言：探测命令清单之后，仓库里不该
        多出 `CORECODE.md`，`.gitignore` 也不该被改动。
        """
        import subprocess

        repo = Path(REPO)
        mini_md = repo / "CORECODE.md"
        gitignore_before = None
        gitignore = repo / ".gitignore"
        if gitignore.exists():
            gitignore_before = gitignore.read_text(encoding="utf-8")

        # 有副作用的命令必须被显式登记——否则上面那条探测会把它们跑掉。
        from corecode.cli_commands import SIDE_EFFECTING_COMMANDS

        self.assertIn("/init", SIDE_EFFECTING_COMMANDS)

        # 确认仓库当前状态（探测逻辑在别的测试里已跑过多次）。
        self.assertFalse(mini_md.exists(), "探测命令时往仓库写了 CORECODE.md")
        if gitignore_before is not None:
            self.assertEqual(
                gitignore.read_text(encoding="utf-8"),
                gitignore_before,
                "探测命令时改动了 .gitignore",
            )
        del subprocess

    def test_no_extra_commands(self) -> None:
        extra = self._python_commands() - self.TS_COMMANDS
        self.assertEqual(extra, set(), f"多出了意外的命令：{sorted(extra)}")

    def test_new_commands_are_listed_in_help(self) -> None:
        from corecode.cli_commands import SLASH_COMMANDS

        usages = [item.usage for item in SLASH_COMMANDS]
        self.assertTrue(any(u.startswith("/model") for u in usages))
        self.assertIn("/config-paths", usages)

    def test_every_listed_command_has_a_handler_or_is_a_shortcut(self) -> None:
        """**帮助里列出的每条命令都必须能被处理。**

        列出但点不动的命令是对用户的直接误导——比不列更糟。
        """
        from types import SimpleNamespace

        from corecode.cli_commands import SLASH_COMMANDS, try_handle_local_command
        from corecode.local_tool_shortcuts import parse_local_tool_shortcut

        # 由运行时（SessionRuntime.command）处理，不走本地命令表。
        runtime_handled = {"/goal", "/loop", "/exit"}

        # **会改仓库的命令不能在仓库上探测。**
        #
        # `/init` 会真的创建 `.corecode/`、往 `.gitignore` 追加条目、并写出
        # `CORECODE.md`。我第一版把它连同其他命令一起在 `cwd=REPO` 上跑了一遍，
        # 结果**真的往仓库里写了文件**。它已经由 `tests/test_repo_init.py`
        # 在临时目录里完整覆盖，这里只需确认它「有处理函数」。
        #
        # 判据：这条命令在探测时只做「有没有 handler」的检查，不实际执行。
        side_effecting = {"/init"}

        host = SimpleNamespace(
            runtime_config=None,
            session_id="probe",
            cwd=REPO,
            already_saved_count=0,
            messages=[],
            subagents=None,
            # `_render_tools` 会调 `create_worker_registry(host.tools)`，
            # 那需要**真的** ToolRegistry（有 `subset`），SimpleNamespace 不行。
            tools=ToolRegistry([]),
            permissions=SimpleNamespace(get_summary=lambda: []),
            plan=SimpleNamespace(
                get_snapshot=lambda: SimpleNamespace(todos=[]),
                reset=lambda _session_id: None,
            ),
            stats=SessionStats(),
            # 动作类命令需要的宿主能力。探测「命令有没有处理函数」时它们
            # 大多不会被真正走到，但属性必须存在——否则测的是 AttributeError
            # 而不是「有没有 handler」。
            fork=lambda _session_id: None,
            resume=lambda _session_id: False,
            reset=lambda: None,
            refresh_system_prompt=lambda: None,
        )
        host.runtime = SimpleNamespace(
            goal=SimpleNamespace(
                manager=SimpleNamespace(get_snapshot=lambda: None)
            ),
            loop=SimpleNamespace(get_snapshot=lambda: None),
        )

        # 每条快捷方式都要给一个**合法**示例：`/grep` 单独一个不是快捷方式
        # （缺 pattern），拿裸命令名去解析会误判成「没有处理函数」。
        shortcut_samples = {
            "/ls": "/ls",
            "/grep": "/grep TODO",
            "/read": "/read a.txt",
            "/write": "/write a.txt::x",
            "/modify": "/modify a.txt::x",
            "/edit": "/edit a.txt::s::r",
            "/patch": "/patch a.txt::s::r",
            "/cmd": "/cmd pwd",
        }

        probe_home = Path(tempfile.gettempdir()) / "corecode-probe-home"
        unhandled: list[str] = []
        with patch.dict(os.environ, {"CORECODE_HOME": str(probe_home)}):
            for item in SLASH_COMMANDS:
                head = item.usage.split(" ")[0]
                if head in runtime_handled:
                    continue
                if head in shortcut_samples:
                    sample = shortcut_samples[head]
                    self.assertIsNotNone(
                        parse_local_tool_shortcut(sample),
                        f"{head} 的示例 {sample!r} 解析不出快捷方式",
                    )
                    continue
                if head in side_effecting:
                    # 只断言「handler 表里有它」，不真的调它——见上面的说明。
                    from corecode.cli_commands import _handler_names

                    self.assertIn(head, _handler_names())
                    continue
                if try_handle_local_command(host, head) is None:  # type: ignore[arg-type]
                    unhandled.append(head)

        self.assertEqual(
            unhandled, [], f"这些命令列在帮助里但没有处理函数：{unhandled}"
        )
        # 快捷方式集合与实现必须一一对应——少一个说明帮助里列了没实现的，
        # 多一个说明实现了没告诉用户。
        self.assertEqual(
            set(shortcut_samples),
            {u.split(" ")[0] for u in (
                "/ls [path]", "/grep <pattern>::[path]", "/read <path>",
                "/write <path>::<content>", "/modify <path>::<content>",
                "/edit <path>::<search>::<replace>", "/patch <p>::<s>::<r>",
                "/cmd [cwd::]<command> [args...]",
            )},
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
