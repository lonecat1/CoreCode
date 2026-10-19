"""四条诊断命令的输出形状：`/status` `/config-paths` `/permissions` `/memory`。

## 为什么形状需要单独钉住

这四条命令的**全部价值**是「让用户读懂 + 让脚本 grep 到」。形状走样（表格换成
扁平行、少个冒号、少个字段）不影响任何程序运行，所以普通测试不会发现，而
用户会以为信息丢了。

这些形状由 `tools/e2e_compare.py` 的 `诊断:` 场景与 TS 原版**逐字**对照过
（9/9 通过）。但那个工具需要 TS 仓库在场、还要跑 `tsx`，进不了单元测试。
所以这里把对照结论固化成本地断言：日后任何形状漂移都会在这里变红。

## 这一组发现过的问题

- 四条命令原本都是**中文表格**，TS 都是 `label: value` 扁平行 → 脚本 grep 不到
- `/status` 的来源摘要缺了 Claude 回退那一段
- `/memory` 少了 `preview` 字段；`lines` 没先 trim；尾部换行被吃掉导致
  `chars` 少 1；`\\r\\n` 被归一化导致 `chars` 再少 3
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from minicode.cli_commands import _render_config_paths  # noqa: E402
from minicode.memory import describe_memory_files, render_memory_report  # noqa: E402

#: `renderMemoryReport()` 的字段顺序（`memory.ts:218-224`）。
MEMORY_FIELDS = ["scope", "lines", "chars", "preview"]


class ConfigPathsShapeTests(unittest.TestCase):
    """`/config-paths`：四行 `label: value`，无标题、无分隔线、不折行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="diag-")
        self.home = Path(self._tmp) / "home"
        self.home.mkdir()
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)}, clear=False)
        self._env.start()
        os.environ.pop("MINI_CODE_HOME", None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_exactly_four_labelled_lines(self) -> None:
        lines = _render_config_paths().splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(
            [line.split(":")[0] for line in lines],
            ["mini-code settings", "mini-code permissions",
             "mini-code mcp", "compat fallback"],
        )

    def test_every_line_uses_label_colon_value(self) -> None:
        for line in _render_config_paths().splitlines():
            with self.subTest(line=line[:30]):
                self.assertIn(": ", line)
                # 表格化会带两空格缩进；这里不该有
                self.assertFalse(line.startswith(" "), "不该有缩进")

    def test_no_title_or_separator(self) -> None:
        output = _render_config_paths()
        self.assertNotIn("配置路径", output)
        self.assertNotIn("───", output)

    def test_paths_are_complete(self) -> None:
        """路径**完整**出现，没有省略号、没有被折成两行。"""
        from minicode.config import claude_settings_path, settings_path

        output = _render_config_paths()
        self.assertNotIn("...", output)
        self.assertIn(str(settings_path()), output)
        self.assertIn(str(claude_settings_path()), output)


class MemoryReportShapeTests(unittest.TestCase):
    """`/memory`：抬头 + 空行 + 每文件五行（含 `preview`）。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="memory-shape-"))

    def _write(self, relative: str, content: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # `newline=""` 才会写 `\n` 而不是 `\r\n`，让期望值可预测
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)

    def test_empty_case_is_the_original_sentence(self) -> None:
        self.assertEqual(render_memory_report(str(self.root)), "No memory files loaded.")

    def test_header_and_field_order(self) -> None:
        self._write("MINI.md", "# 项目说明\n\n正文一行。\n")
        output = render_memory_report(str(self.root))
        lines = output.splitlines()
        self.assertTrue(lines[0].startswith("Memory files loaded: "), lines[0])
        self.assertEqual(lines[0], "Memory files loaded: 1")
        # 抬头与条目之间隔得比条目之间更宽（原版数组里有 `''` 元素）
        self.assertIn("\n\n\n", output.split("1. MINI.md")[0])
        body = [line.strip() for line in lines if line.strip().startswith(
            ("scope:", "lines:", "chars:", "preview:"))]
        self.assertEqual([line.split(":")[0] for line in body], MEMORY_FIELDS)

    def test_preview_is_the_first_line_of_trimmed_content(self) -> None:
        self._write("MINI.md", "\n\n# 标题在前\n正文。\n")
        entries = describe_memory_files(str(self.root))
        self.assertEqual(entries[0]["preview"], "# 标题在前")

    def test_preview_falls_back_for_blank_file(self) -> None:
        self._write("MINI.md", "   \n\n")
        # 全空白文件会被 `_read_if_present` 当成不存在
        self.assertEqual(render_memory_report(str(self.root)), "No memory files loaded.")

    def test_lines_counts_after_trimming(self) -> None:
        """`lines` 先 trim 再数——首尾空行不算（原版 `trimmed.split('\\n')`）。"""
        self._write("MINI.md", "\n\n\n正文\n\n\n")
        entries = describe_memory_files(str(self.root))
        self.assertEqual(entries[0]["lines"], 1)

    def test_chars_counts_the_raw_content(self) -> None:
        """`chars` **不 trim**，也不吃尾部换行——原版是 `content.length`。

        这一条曾经错过两次：先用 `splitlines()` 吃掉了尾部换行（少 1），
        再用默认换行模式读文件把 `\\r\\n` 折成 `\\n`（在 Windows 上再少 3）。
        """
        content = "# 标题\n正文\n"
        self._write("MINI.md", content)
        entries = describe_memory_files(str(self.root))
        self.assertEqual(entries[0]["chars"], len(content))

    def test_crlf_is_preserved(self) -> None:
        """Windows 换行必须原样计入——Node 的 `readFile('utf8')` 不做归一化。"""
        crlf = "# 标题\r\n正文\r\n"
        target = self.root / "MINI.md"
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(crlf)
        entries = describe_memory_files(str(self.root))
        self.assertEqual(entries[0]["chars"], len(crlf))
        self.assertEqual(entries[0]["lines"], 2)

    def test_scope_values_are_the_original_three(self) -> None:
        self._write("MINI.md", "项目级\n")
        entries = describe_memory_files(str(self.root))
        self.assertEqual(entries[0]["scope"], "project")


class StatusPrefixTests(unittest.TestCase):
    """`/status` 的开头**必须**是原版那五行（可被脚本 grep）。"""

    #: 原版 `cli-commands.ts:276-282` 的字段顺序。
    TS_FIELDS = ["model", "baseUrl", "auth", "mcp servers", "config"]

    def test_first_five_lines_are_the_original_fields(self) -> None:
        from minicode.cli import build_host, build_parser
        from minicode.cli_commands import _render_status

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.dict(os.environ, {"MINICODE_HOME": str(home)}):
                os.environ.pop("MINI_CODE_HOME", None)
                args = build_parser().parse_args(["--mock", "--cwd", tmp])
                host = build_host(args, warn_offline=False)
            output = _render_status(host)
        lines = output.splitlines()
        self.assertEqual([line.split(":")[0] for line in lines[:5]], self.TS_FIELDS)
        # 补充信息在五行**之后**，不能混进去
        self.assertNotIn("会话状态", "\n".join(lines[:5]))

    def _status_lines(self, env: dict[str, str], home: Path | None = None) -> dict[str, str]:
        """配好环境后跑一次 `/status`，把前五行解析成 `{字段: 值}`。

        `home` 可以复用：调用方先在同一目录里放好 `mcp.json` 之类，再调这里。
        """
        from minicode.cli import build_host, build_parser
        from minicode.cli_commands import _render_status

        tmp = tempfile.mkdtemp(prefix="status-shape-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        actual_home = home if home is not None else Path(tmp) / "home"
        actual_home.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"MINICODE_HOME": str(actual_home), **env}):
            os.environ.pop("MINI_CODE_HOME", None)
            args = build_parser().parse_args(["--cwd", tmp])
            host = build_host(args, warn_offline=False)
        output = _render_status(host)
        pairs: dict[str, str] = {}
        for line in output.splitlines()[:5]:
            key, _, value = line.partition(": ")
            pairs[key] = value
        return pairs

    def test_auth_line_reflects_which_variable_supplied_the_token(self) -> None:
        """有 `ANTHROPIC_AUTH_TOKEN` 时报它，否则报 `ANTHROPIC_API_KEY`。

        原版是 `runtime.authToken ? 'ANTHROPIC_AUTH_TOKEN' : 'ANTHROPIC_API_KEY'`
        ——这不是装饰：它告诉用户**当前生效的是哪个变量**，配错变量时一眼可见。
        """
        with_token = self._status_lines({
            "ANTHROPIC_AUTH_TOKEN": "t", "MINICODE_MODEL": "claude-sonnet-4",
        })
        self.assertEqual(with_token["auth"], "ANTHROPIC_AUTH_TOKEN")

        without = self._status_lines({
            "ANTHROPIC_API_KEY": "k", "MINICODE_MODEL": "claude-sonnet-4",
        })
        # 只给了 API_KEY 时仍然报 API_KEY
        self.assertEqual(without["auth"], "ANTHROPIC_API_KEY")

    def test_mcp_server_count_is_real(self) -> None:
        """`mcp servers:` 必须是**配置里真实的条数**，不是写死的 0。

        注意必须给凭据：没有凭据时 `build_host` 捕获 `ConfigError` 后
        `runtime_config=None`，`/status` 走离线分支、计数恒为 0——那样这条
        断言就测不到「计数来自配置」这件事。
        """
        empty = self._status_lines({
            "MINICODE_MODEL": "claude-sonnet-4", "ANTHROPIC_AUTH_TOKEN": "t",
        })
        self.assertEqual(empty["mcp servers"], "0")

        from minicode.config import mcp_path

        # 先造好 home 与 mcp.json，再用**同一个 home** 跑 /status
        home = Path(tempfile.mkdtemp(prefix="status-mcp-")) / "home"
        self.addCleanup(__import__("shutil").rmtree, str(home.parent), True)
        home.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"MINICODE_HOME": str(home)}):
            os.environ.pop("MINI_CODE_HOME", None)
            path = mcp_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}',
                encoding="utf-8",
            )
        configured = self._status_lines(
            {"MINICODE_MODEL": "claude-sonnet-4", "ANTHROPIC_AUTH_TOKEN": "t"},
            home=home,
        )
        self.assertEqual(configured["mcp servers"], "2")

    def test_source_summary_names_the_claude_fallback(self) -> None:
        """来源摘要必须有三段，中间那段是 Claude 回退。"""
        from minicode.config import claude_settings_path, load_runtime_config

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with patch.dict(os.environ, {
                "MINICODE_HOME": str(home),
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "MINICODE_MODEL": "claude-sonnet-4",
            }):
                os.environ.pop("MINI_CODE_HOME", None)
                runtime = load_runtime_config()
        summary = runtime.source_summary
        self.assertTrue(summary.startswith("config: "), summary)
        self.assertIn(" > ", summary)
        self.assertIn(str(claude_settings_path()), summary)
        self.assertTrue(summary.endswith("> process.env"), summary)


class PermissionsShapeTests(unittest.TestCase):
    """`/permissions`：原版四行 `label: value`。

    `get_summary()` 产出的就是 `label: value` 串——原版 `findValue()` 从中取值。
    之前渲染时把它塞进对齐表格，于是 `permission store:` 那一行（路径）直接
    消失了：用户连「去哪个文件改规则」都不知道。
    """

    def _render(self, summary: list[str], home: Path | None = None) -> tuple[str, Path]:
        """在给定的 home 下渲染 `/permissions`，返回 `(输出, 实际用的 home)`。

        把 home 返回出去是必须的：断言里要拿 `permissions_path()` 做期望值，
        而它依赖 `MINICODE_HOME`——在 patch 上下文**外**取值会得到真实家目录。
        """
        from minicode import cli_commands

        class _FakePermissions:
            def get_summary(_self) -> list[str]:
                return summary

        class _FakeHost:
            permissions = _FakePermissions()

        tmp = tempfile.mkdtemp(prefix="perm-shape-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        actual_home = home if home is not None else Path(tmp) / "home"
        actual_home.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"MINICODE_HOME": str(actual_home)}):
            os.environ.pop("MINI_CODE_HOME", None)
            return cli_commands._render_permissions(_FakeHost()), actual_home

    def test_four_lines_in_the_original_order(self) -> None:
        output, _home = self._render(
            ["cwd: /w", "extra allowed dirs: none", "dangerous allowlist: none"]
        )
        lines = output.splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(
            [line.split(":")[0] for line in lines],
            ["permission store", "cwd", "extra allowed dirs", "dangerous allowlist"],
        )

    def test_store_line_names_the_real_path(self) -> None:
        """`permission store:` 必须是**动态**访问器的值。

        期望值必须在**同一个 home 下**取——否则比的是真实家目录。
        """
        from minicode.permissions import permissions_path

        output, home = self._render(["cwd: /w"])
        with patch.dict(os.environ, {"MINICODE_HOME": str(home)}):
            os.environ.pop("MINI_CODE_HOME", None)
            expected = str(permissions_path())
        self.assertIn(f"permission store: {expected}", output)

    def test_values_come_from_the_summary(self) -> None:
        """四行里的值必须**从 summary 解析**，不能写死 `none`。"""
        output, _home = self._render(
            [
                "cwd: C:\\work",
                "extra allowed dirs: /a, /b",
                "dangerous allowlist: git push --force",
            ]
        )
        lines = output.splitlines()
        self.assertEqual(lines[1], "cwd: C:\\work")
        self.assertEqual(lines[2], "extra allowed dirs: /a, /b")
        self.assertEqual(lines[3], "dangerous allowlist: git push --force")

    def test_missing_labels_fall_back_to_none(self) -> None:
        output, _home = self._render([])
        lines = output.splitlines()
        self.assertEqual(lines[1], "cwd: none")
        self.assertEqual(lines[2], "extra allowed dirs: none")
        self.assertEqual(lines[3], "dangerous allowlist: none")

    def test_trusted_edit_targets_are_appended_when_present(self) -> None:
        """本实现多出的一类规则（D17）——不显示等于让人以为它不存在。"""
        output, _home = self._render(["cwd: /w", "trusted edit targets: /a.txt"])
        lines = output.splitlines()
        self.assertEqual(len(lines), 5, lines)
        self.assertEqual(lines[4], "trusted edit targets: /a.txt")

    def test_no_trusted_line_when_absent(self) -> None:
        output, _home = self._render(["cwd: /w"])
        self.assertEqual(len(output.splitlines()), 4, output)


class MemoryPreviewFallbackTests(unittest.TestCase):
    def test_empty_preview_fallback_is_unreachable(self) -> None:
        """`or "<empty>"` 那条回退**永远走不到**——这是刻意保留的。

        原版写的是 `trimmed.split('\\n')[0] || '<empty>'`。但 `trimmed` 非空时
        它的第一个字符必然不是空白，所以「第一行为空」这种情况不存在；
        而全空白的文件在读取阶段就被 `_read_if_present` 判为不存在。

        保留它是因为：行为与原版**完全一致**（都一样走不到），而删掉会让
        「逐字对照」时少一处对应。这条测试的作用是把这个判断**记下来**，
        免得日后有人以为它是活代码、为它写一个永远不成立的用例。
        """

        def preview(content: str) -> str:
            trimmed = content.strip()
            return (trimmed.split("\n")[0] if trimmed else "") or "<empty>"

        for content in ("正文", "\n正文\n", "正文\n第二行", "# 标题\n"):
            with self.subTest(content=content):
                self.assertNotEqual(preview(content), "<empty>")
        # 唯一会产出的情况是空内容，而那条路径在读文件时已被拦掉
        self.assertEqual(preview("   \n\n"), "<empty>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
