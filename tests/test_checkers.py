"""给**检查器本身**写测试。

## 为什么检查器也要被测试

这套工具是「其它代码是否可信」的依据。如果它们自己判断错了，结论就是错的，
而且**错得很安静**——一个永远返回「全部通过」的检查器看起来和健康的一样。

三件事驱动了这个文件：

1. `check_constants.py` 的「伪引用」检测（注释/docstring 里出现不算引用）是它
   存在的全部理由。我自己被它救过一次（`MIN_KEEP_TOKENS` 只在 docstring 里
   被提到，扫一眼像「有人用」）。
2. 那个检查刚从「每查一个常量就把全部模块重新 `ast.parse`」改成「每文件只算
   一次」——**158 秒 → 2.3 秒**。性能改了，结论必须一模一样。
3. `check_duplicates.py` 是新加的，它抓的是「同名定义被遮蔽」，这一类 bug
   现有其它四个检查器全都看不见。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _path in (os.path.join(_REPO, "src"), os.path.join(_REPO, "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import check_constants  # noqa: E402
import check_duplicates  # noqa: E402


class FakeReferenceTests(unittest.TestCase):
    """`find_occurrences` 必须把「注释里的提及」与「代码里的引用」分开。"""

    #: 常量只在 docstring 里出现——这是**伪引用**。
    FAKE = '''"""模块说明：这里提到 LIMIT 但只是说说。"""


def use():
    """函数 docstring 里也提到 LIMIT。"""
    return 1
'''

    #: 常量在代码里真的被引用。
    REAL = '''"""模块说明。"""

LIMIT_HOLDER = None


def use():
    return LIMIT_HOLDER
'''

    #: 常量只在 `#` 注释里出现。
    COMMENT = """# 这里提到 LIMIT，但只是注释
VALUE = 1
"""

    def _occurrences(self, text: str, name: str, *, precomputed: bool):
        sources = {"m.py": text}
        ranges = check_constants.docstring_ranges(sources) if precomputed else None
        return check_constants.find_occurrences(name, sources, "", ranges)

    def test_docstring_only_mention_is_flagged_as_comment(self) -> None:
        for precomputed in (True, False):
            with self.subTest(precomputed=precomputed):
                hits = self._occurrences(self.FAKE, "LIMIT", precomputed=precomputed)
                self.assertEqual(len(hits), 2, hits)
                self.assertTrue(all(hit.is_comment for hit in hits))

    def test_hash_comment_is_flagged_as_comment(self) -> None:
        hits = self._occurrences(self.COMMENT, "LIMIT", precomputed=True)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].is_comment)

    def test_real_reference_is_not_a_comment(self) -> None:
        hits = self._occurrences(self.REAL, "LIMIT", precomputed=True)
        # `LIMIT_HOLDER` 含子串 `LIMIT`，会命中两行（定义行 + return 行）
        self.assertTrue(hits)
        self.assertTrue(all(not hit.is_comment for hit in hits), hits)

    def test_precomputed_ranges_give_identical_results(self) -> None:
        """**性能优化不能改变结论。**

        重构把 docstring 区间从「每个常量算一遍」改成「每个文件算一次」，
        158 秒 → 2.3 秒。这条断言两种调用方式结果完全相同。
        """
        for text, name in (
            (self.FAKE, "LIMIT"),
            (self.REAL, "LIMIT"),
            (self.COMMENT, "LIMIT"),
            (self.FAKE, "NOTHING"),
        ):
            with self.subTest(name=name):
                with_cache = self._occurrences(text, name, precomputed=True)
                without_cache = self._occurrences(text, name, precomputed=False)
                self.assertEqual(
                    [(h.path, h.line, h.text, h.is_comment) for h in with_cache],
                    [(h.path, h.line, h.text, h.is_comment) for h in without_cache],
                )

    def test_syntax_error_does_not_crash(self) -> None:
        """半成品文件（语法错）不该让检查器崩掉。"""
        ranges = check_constants.docstring_ranges({"bad.py": "def broken(:\n"})
        self.assertEqual(ranges["bad.py"], [])
        hits = check_constants.find_occurrences("X", {"bad.py": "X = 1\n"}, "", ranges)
        self.assertEqual(len(hits), 1)


class DuplicateDefinitionTests(unittest.TestCase):
    """`check_duplicates` 抓「同名定义被遮蔽」。

    这一类 bug 其它四个检查器**全都看不见**：名字确实被引用了
    （`check_orphans` 满意）、导入没问题（`check_imports` 满意）、
    改中已被遮蔽的那份不影响行为（`mutation_check` 看不出问题）。
    """

    def _bindings(self, source: str):
        import ast

        tree = ast.parse(source)
        return [(name, kind) for name, _line, kind in
                check_duplicates.module_level_bindings(tree)]

    def test_duplicate_function_is_visible(self) -> None:
        source = (
            "def f():\n    return 1\n\n\n"
            "def f():\n    return 2\n"
        )
        names = [name for name, kind in self._bindings(source) if kind == "def"]
        self.assertEqual(names, ["f", "f"])

    def test_duplicate_class_is_visible(self) -> None:
        source = "class C:\n    pass\n\n\nclass C:\n    pass\n"
        names = [name for name, kind in self._bindings(source) if kind == "class"]
        self.assertEqual(names, ["C", "C"])

    def test_duplicate_assignment_is_visible(self) -> None:
        source = "X = 1\nX = 2\n"
        names = [name for name, kind in self._bindings(source) if kind == "assign"]
        self.assertEqual(names, ["X", "X"])

    def test_definition_inside_function_is_not_a_duplicate(self) -> None:
        """函数体内的重绑定是正常代码，不属于「遮蔽」。"""
        source = (
            "def outer():\n"
            "    def f():\n        return 1\n"
            "    def f():\n        return 2\n"
            "    return f\n"
        )
        bindings = self._bindings(source)
        self.assertEqual([name for name, _kind in bindings], ["outer"])

    def test_annotation_without_value_is_not_a_binding(self) -> None:
        """`x: int` 只是注解，没有绑定；`x: int = 1` 才算。"""
        self.assertEqual(self._bindings("x: int\n"), [])
        self.assertEqual(
            [name for name, _kind in self._bindings("x: int = 1\n")], ["x"]
        )

    def test_the_repository_is_clean(self) -> None:
        """当前仓库里没有同名遮蔽——这是那条 bug 的回归门。"""
        problems = check_duplicates.scan()
        self.assertEqual(problems, [], "\n".join(problems))


class FileMapTests(unittest.TestCase):
    """`file_map` 的覆盖度审计。

    它替代了一句**没有依据的说法**：文档曾写「77 个文件 → 68 映射 + 9 TUI，
    0 未映射」，但当时并没有那张表，而且 68+9=77 漏掉了 `ui.ts` 那个转出桶。
    """

    def test_every_ts_file_is_covered(self) -> None:
        import file_map

        ts = file_map.ts_files()
        covered = set(file_map.MAPPING) | set(file_map.TUI_EXCLUDED)
        self.assertEqual(sorted(ts - covered), [], "有 TS 文件没被覆盖")

    def test_every_mapped_python_file_exists(self) -> None:
        import file_map

        for source, targets in sorted(file_map.MAPPING.items()):
            for target in targets:
                with self.subTest(source=source, target=target):
                    self.assertTrue(
                        (file_map.PY_ROOT / target).exists(),
                        f"{source} → {target} 不存在",
                    )

    def test_audit_catches_a_mapping_to_a_missing_file(self) -> None:
        """**这条测的是 `audit()` 自己的存在性检查**，不是重做一遍断言。

        早先这里只有一个「遍历 `MAPPING` 断言文件存在」的测试——它确实能发现
        坏映射，但它**绕过了 `audit()`**。于是把 `audit()` 里的存在性检查
        改成 `if False:` 也不会变红：检查器废了，测试照样绿。

        现在临时往 `MAPPING` 里塞一个不存在的目标，断言 `audit()` 报出来。
        """
        import file_map

        original = file_map.MAPPING["abort.ts"]
        try:
            file_map.MAPPING["abort.ts"] = ("definitely_not_here.py",)
            problems = file_map.audit()
        finally:
            file_map.MAPPING["abort.ts"] = original
        joined = "\n".join(problems)
        self.assertIn("definitely_not_here.py", joined, joined)
        self.assertIn("文件不存在", joined, joined)

    def test_audit_catches_an_uncovered_ts_file(self) -> None:
        """表里漏一个 TS 文件时 `audit()` 要报。"""
        import file_map

        original = file_map.MAPPING.pop("abort.ts")
        try:
            problems = file_map.audit()
        finally:
            file_map.MAPPING["abort.ts"] = original
        joined = "\n".join(problems)
        self.assertIn("abort.ts", joined, joined)

    def test_audit_catches_py_only_overlapping_the_mapping(self) -> None:
        """同一个文件不能既被映射又登记为「独有」——二选一。"""
        import file_map

        original = file_map.MAPPING["abort.ts"]
        try:
            file_map.MAPPING["abort.ts"] = ("stats.py",)
            problems = file_map.audit()
        finally:
            file_map.MAPPING["abort.ts"] = original
        joined = "\n".join(problems)
        self.assertIn("既被映射又登记为独有", joined, joined)

    def test_py_only_entries_exist_and_carry_a_reason(self) -> None:
        import file_map

        for name, reason in sorted(file_map.PY_ONLY.items()):
            with self.subTest(name=name):
                self.assertTrue((file_map.PY_ROOT / name).exists(), f"{name} 不存在")
                self.assertGreater(len(reason.strip()), 10, f"{name} 的理由太短")

    def test_the_audit_passes(self) -> None:
        import file_map

        self.assertEqual(file_map.audit(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
