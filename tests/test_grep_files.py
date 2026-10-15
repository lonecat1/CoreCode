"""`grep_files` 的测试。

这个工具此前**没有任何专属测试**（`test_tools.py` 只覆盖 `list_files`），
而它包含三段有实际逻辑的代码：ripgrep 探测、纯 Python 回退扫描、结果上限。
新建这个文件是为了把这几段钉住，其中第一条就是修复的真实缺陷。

## 一个真实缺陷：搜「单个文件」永远没有结果

工具的 `path` 参数可以是文件也可以是目录（`/grep <pattern>::<file>` 这个
文档化用法就是传文件）。但回退扫描用 `os.walk(target)`，而 `os.walk()` 遇到
**文件路径**时不报错、只是静默产出空序列。

于是「在一个明确的文件里搜一段你肉眼可见的文字」会得到 `(no matches)`。
这个失败方式特别糟：它看起来像「确实没有匹配」，而不是「我根本没搜」。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.tooling import ToolContext, ToolRegistry  # noqa: E402
from minicode.tools.grep_files import (  # noqa: E402
    MAX_MATCHES,
    SKIP_DIRECTORIES,
    _format_match,
    _iter_candidate_files,
    _search_with_python,
    grep_files_tool,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GrepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="minicode-grep-")
        self.context = ToolContext(cwd=self.root, permissions=None, signal=None)
        self.registry = ToolRegistry([grep_files_tool])

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, content: str) -> str:
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return path

    def run_grep(self, data: dict):  # noqa: ANN001
        return self.registry.execute("grep_files", data, self.context)


class CandidateFileTests(GrepTestCase):
    def test_file_target_yields_that_file(self) -> None:
        """**核心修复的护栏**：文件路径必须产出它自己。

        用 `os.walk` 的话这里是空的——那正是缺陷所在。
        """
        path = self.write("a.txt", "hello\n")
        self.assertEqual(list(_iter_candidate_files(path)), [path])

    def test_directory_target_yields_contained_files(self) -> None:
        self.write("a.txt", "a\n")
        self.write("sub/b.txt", "b\n")
        found = {os.path.basename(p) for p in _iter_candidate_files(self.root)}
        self.assertEqual(found, {"a.txt", "b.txt"})

    def test_skipped_directories_are_not_descended(self) -> None:
        self.write("keep.txt", "x\n")
        for skipped in sorted(SKIP_DIRECTORIES)[:3]:
            self.write(os.path.join(skipped, "inside.txt"), "y\n")
        found = {os.path.basename(p) for p in _iter_candidate_files(self.root)}
        self.assertEqual(found, {"keep.txt"})

    def test_missing_target_yields_nothing(self) -> None:
        self.assertEqual(list(_iter_candidate_files(os.path.join(self.root, "nope"))), [])


class PythonScannerTests(GrepTestCase):
    def test_finds_matches_in_a_directory(self) -> None:
        self.write("a.txt", "alpha\nbeta\n")
        output = _search_with_python("beta", self.root, self.root)
        self.assertIn("a.txt:2:beta", output)

    def test_finds_matches_in_a_single_file(self) -> None:
        """**这条以前会失败**：搜一个文件返回 `(no matches)`。"""
        path = self.write("a.txt", "alpha\nbeta\n")
        output = _search_with_python("beta", path, self.root)
        self.assertIn("a.txt:2:beta", output)
        self.assertNotIn("no matches", output)

    def test_no_matches_message(self) -> None:
        self.write("a.txt", "alpha\n")
        self.assertEqual(_search_with_python("zzz", self.root, self.root), "(no matches)")

    def test_invalid_regex_is_reported(self) -> None:
        output = _search_with_python("([unclosed", self.root, self.root)
        self.assertIn("正则表达式非法", output)

    def test_pattern_is_a_regex_not_a_literal(self) -> None:
        """`grep` 的语义是正则，不是字面串。"""
        self.write("a.txt", "abc123\n")
        self.assertIn("abc123", _search_with_python(r"abc\d+", self.root, self.root))
        self.assertEqual(
            _search_with_python(r"abc\d+", self.root, self.root).count("\n"), 0
        )

    def test_binary_files_are_skipped(self) -> None:
        self.write("image.png", "needle\n")
        self.assertEqual(_search_with_python("needle", self.root, self.root), "(no matches)")

    def test_oversized_files_are_skipped(self) -> None:
        self.write("big.txt", "needle\n")
        with patch("minicode.tools.grep_files.MAX_FILE_BYTES", 1):
            self.assertEqual(
                _search_with_python("needle", self.root, self.root), "(no matches)"
            )

    def test_matches_are_capped(self) -> None:
        self.write("many.txt", "hit\n" * (MAX_MATCHES + 50))
        output = _search_with_python("hit", self.root, self.root)
        self.assertIn("上限", output, "超限时应当注明被截断")
        # 上限 + 1 行截断说明
        self.assertLessEqual(output.count("\n") + 1, MAX_MATCHES + 1)

    def test_long_lines_are_truncated_in_output(self) -> None:
        self.write("long.txt", "needle" + "x" * 2000 + "\n")
        output = _search_with_python("needle", self.root, self.root)
        self.assertLess(len(output), 800, "单行过长应当被截断")

    def test_unreadable_file_does_not_break_the_scan(self) -> None:
        self.write("good.txt", "needle\n")
        binary = os.path.join(self.root, "bad.txt")
        with open(binary, "wb") as handle:
            handle.write(b"needle\xff\xfe\x00")
        output = _search_with_python("needle", self.root, self.root)
        self.assertIn("good.txt", output)

    def test_match_format_is_path_line_content(self) -> None:
        path = self.write("a.txt", "one\ntwo\n")
        output = _search_with_python("two", path, self.root)
        self.assertEqual(output, "a.txt:2:two")


class FormatMatchTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(_format_match("a.py", 7, "code\n"), "a.py:7:code")

    def test_trailing_newline_is_stripped(self) -> None:
        self.assertNotIn("\n", _format_match("a.py", 1, "x\n"))


class GrepToolTests(GrepTestCase):
    def test_empty_pattern_is_rejected(self) -> None:
        result = self.run_grep({"pattern": ""})
        self.assertFalse(result.ok)
        self.assertIn("pattern", result.output)

    def test_default_path_is_the_workspace(self) -> None:
        self.write("a.txt", "needle\n")
        result = self.run_grep({"pattern": "needle"})
        self.assertTrue(result.ok, result.output)
        self.assertIn("needle", result.output)

    def test_explicit_file_path_works(self) -> None:
        """**端到端护栏**：从工具入口传文件路径也要能搜到。"""
        self.write("a.txt", "needle\n")
        result = self.run_grep({"pattern": "needle", "path": "a.txt"})
        self.assertTrue(result.ok, result.output)
        self.assertIn("a.txt:1:needle", result.output)

    def test_fallback_is_announced(self) -> None:
        """没有 rg 时要注明用了内置扫描器，便于排查「结果为何不同」。"""
        self.write("a.txt", "needle\n")
        with patch("minicode.tools.grep_files._search_with_ripgrep", return_value=(False, "")):
            result = self.run_grep({"pattern": "needle"})
        self.assertTrue(result.ok)
        self.assertIn("内置扫描器", result.output)

    def test_ripgrep_output_is_passed_through(self) -> None:
        self.write("a.txt", "needle\n")
        with patch(
            "minicode.tools.grep_files._search_with_ripgrep",
            return_value=(True, "a.txt:1:needle"),
        ):
            result = self.run_grep({"pattern": "needle"})
        self.assertEqual(result.output, "a.txt:1:needle")

    def test_schema(self) -> None:
        schema = grep_files_tool.input_schema
        self.assertEqual(schema["required"], ["pattern"])
        self.assertEqual(sorted(schema["properties"]), ["path", "pattern"])

    def test_validator_requires_pattern(self) -> None:
        parsed = grep_files_tool.validator({"pattern": "x"})
        self.assertEqual(parsed, {"pattern": "x"})

    def test_validator_drops_empty_path(self) -> None:
        parsed = grep_files_tool.validator({"pattern": "x", "path": ""})
        self.assertNotIn("path", parsed)

    def test_validator_keeps_path(self) -> None:
        parsed = grep_files_tool.validator({"pattern": "x", "path": "src"})
        self.assertEqual(parsed["path"], "src")

    def test_tool_name(self) -> None:
        self.assertEqual(grep_files_tool.name, "grep_files")


class RealRepositoryTests(unittest.TestCase):
    """在真实仓库上跑一次，确认能搜到**已知存在**的内容。

    单元测试都用临时目录；这一条用意是「拿一个我知道内容的地方验证」，
    能抓到「扫描器整体失效」这类问题。
    """

    def test_finds_a_known_string_in_a_known_file(self) -> None:
        from minicode.tools.grep_files import _search_with_python

        target = os.path.join(REPO, "pyproject.toml")
        output = _search_with_python(r"^name", target, REPO)
        self.assertIn("name", output)
        self.assertNotIn("no matches", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
