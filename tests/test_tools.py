"""内置工具的行为测试。

重点是 `list_files`，因为给它加递归 glob 是为了解决一个**实测出来的**
问题：任务「统计某目录下有多少个 .py 文件」原本无解——`list_files` 只列
一层，`grep_files` 返回匹配行而非文件名，于是较弱的模型连续调用 11 次
`list_files` 暴力枚举。加了 glob 之后一次调用即可。

因此这里同时钉住两件事：
  1. 单层列举的**原有行为不变**（向后兼容）；
  2. 递归 glob 正确、且不把 `__pycache__` 之类算进来。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.tooling import ToolContext, ToolRegistry  # noqa: E402
from minicode.tools.files import list_files_tool  # noqa: E402

unittest.TestCase.maxDiff = None

_TMP = Path(_HERE) / "_tmp" / "tools"


def build_tree(root: Path) -> None:
    """造一棵有嵌套、有干扰目录的树。"""
    (root / "src" / "core").mkdir(parents=True, exist_ok=True)
    (root / "src" / "util").mkdir(parents=True, exist_ok=True)
    (root / "src" / "core" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)

    (root / "src" / "a.py").write_text("a", encoding="utf-8")
    (root / "src" / "b.py").write_text("b", encoding="utf-8")
    (root / "src" / "core" / "c.py").write_text("c", encoding="utf-8")
    (root / "src" / "core" / "notes.md").write_text("d", encoding="utf-8")
    (root / "src" / "util" / "d.py").write_text("e", encoding="utf-8")

    # 干扰文件必须也是 **.py**。
    #
    # 早先这里只放了 .pyc，于是「跳过 __pycache__」这条保证**测不出来**：
    # glob 本来就是 *.py，.pyc 不匹配，跳不跳过结果一样。变异检查抓到了
    # 这一点（去掉 __pycache__ 跳过之后全部测试照样通过）。
    (root / "src" / "core" / "__pycache__" / "c.cpython-312.py").write_text("z", encoding="utf-8")
    (root / "node_modules" / "pkg" / "x.py").write_text("x", encoding="utf-8")


class ListFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)
        self.root = _TMP / "tree"
        self.root.mkdir(parents=True, exist_ok=True)
        build_tree(self.root)
        self.registry = ToolRegistry([list_files_tool])
        self.context = ToolContext(cwd=str(self.root))

    def tearDown(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def _run(self, **payload: object) -> str:
        result = self.registry.execute("list_files", payload, self.context)
        self.assertTrue(result.ok, result.output)
        return result.output

    # -- 原有行为 -----------------------------------------------------------

    def test_single_level_lists_only_direct_children(self) -> None:
        """不传 glob 时只列一层——原有行为必须保持不变。"""
        output = self._run(path="src")
        self.assertIn("a.py", output)
        self.assertIn("b.py", output)
        # 子目录内容不该出现
        self.assertNotIn("c.py", output)
        self.assertNotIn("d.py", output)

    def test_single_level_marks_directories_with_slash(self) -> None:
        output = self._run(path="src")
        self.assertIn("core/", output)
        self.assertIn("util/", output)

    def test_single_level_shows_file_size(self) -> None:
        self.assertIn("bytes)", self._run(path="src"))

    def test_missing_directory_is_a_clean_failure(self) -> None:
        result = self.registry.execute("list_files", {"path": "nope"}, self.context)
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.output)

    def test_default_path_is_workspace_root(self) -> None:
        self.assertIn("src/", self._run())

    def test_empty_directory(self) -> None:
        (self.root / "empty").mkdir()
        self.assertIn("空目录", self._run(path="empty"))

    # -- 递归 glob ----------------------------------------------------------

    def test_glob_finds_nested_matches(self) -> None:
        output = self._run(path="src", glob="*.py")
        for name in ("a.py", "b.py", "core/c.py", "util/d.py"):
            self.assertIn(name, output.replace(os.sep, "/"))

    def test_glob_excludes_non_matching(self) -> None:
        output = self._run(path="src", glob="*.py")
        self.assertNotIn("notes.md", output)

    def test_glob_reports_the_count(self) -> None:
        """计数是这条增强的核心用途——模型据此直接回答「有多少个」。"""
        output = self._run(path="src", glob="*.py")
        self.assertIn("共 4 个文件", output)

    def test_glob_skips_pycache_and_node_modules(self) -> None:
        """干扰目录必须排除，否则统计偏大。

        fixture 里在 `__pycache__` 与 `node_modules` 下**各放了一个 .py**，
        这样「跳过」这件事才是可观测的——若只放 .pyc，glob 本来就不匹配，
        测不出跳过是否生效（这个疏漏曾被变异检查抓到）。
        """
        output = self._run(path=".", glob="*.py")
        self.assertNotIn("__pycache__", output)
        self.assertNotIn("node_modules", output)
        self.assertIn("共 4 个文件", output)

    def test_recursive_without_glob_lists_everything(self) -> None:
        output = self._run(path="src", recursive=True)
        self.assertIn("notes.md", output)
        self.assertIn("c.py", output)

    def test_glob_case_insensitive(self) -> None:
        output = self._run(path="src", glob="*.PY")
        self.assertIn("共 4 个文件", output)

    def test_glob_no_match_says_so(self) -> None:
        output = self._run(path="src", glob="*.rs")
        self.assertIn("没有匹配", output)

    def test_glob_matches_only_file_names_not_paths(self) -> None:
        """`core/*.py` 不该被当成路径通配——本实现只匹配文件名。"""
        output = self._run(path="src", glob="core/*.py")
        self.assertIn("没有匹配", output)

    def test_results_are_relative_and_slash_normalised(self) -> None:
        output = self._run(path="src", glob="*.py")
        for line in output.splitlines():
            if line.endswith(".py"):
                self.assertNotIn("\\", line, f"路径应使用正斜杠: {line}")
                self.assertFalse(
                    os.path.isabs(line), f"路径应为相对路径: {line}"
                )


class ListFilesSchemaTests(unittest.TestCase):
    def test_schema_documents_the_new_parameters(self) -> None:
        """模型靠 schema 里的描述决定怎么用；不写就等于没有。"""
        schema = list_files_tool.input_schema
        properties = schema["properties"]
        self.assertIn("glob", properties)
        self.assertIn("recursive", properties)
        self.assertTrue(list_files_tool.description)

    def test_validator_fills_defaults(self) -> None:
        validator = list_files_tool.validator
        self.assertEqual(
            validator({}),
            {"path": ".", "glob": "", "recursive": False},
        )

    def test_validator_coerces_non_string_glob(self) -> None:
        """模型可能给 null 或数字，不该因此抛异常。"""
        validator = list_files_tool.validator
        self.assertEqual(validator({"glob": None})["glob"], "")
        self.assertEqual(validator({"recursive": None})["recursive"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
