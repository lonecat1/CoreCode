"""记忆文件（`memory.ts`）的测试。

## 这个模块此前没有专属测试

`memory.py` 承载「全局指令 / 项目指令 / `@` 引用展开」——它决定模型每回合
看到哪些规则。没有测试意味着这类缺陷不会被发现，而它恰好就藏了两处：

**① 引用行判定用了字面空格而不是 `\\s`。**
`@foo\\tbar`（制表符分隔）在 Python 里会被当成引用、在 TS 里只是普通文本。
症状很隐蔽：产出一行 `include skipped: not found`，看起来像「文件不存在」，
而实际是「这行根本不是引用」。

**② `visited` 从不移除。**
各被包含一次；只有真正回到自己祖先时才判环。早先这里从不移除，于是第二次
包含同一个文件会报 `cycle detected`——**原因说错了**（那不是环），而且
**内容静默缺失**。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.memory import (  # noqa: E402
    CANDIDATES_PER_DIR,
    MAX_INCLUDE_DEPTH,
    MAX_PER_FILE_CHARS,
    MAX_TOTAL_CHARS,
    _resolve_includes,
)

class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="corecode-memory-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

class ConstantsTests(unittest.TestCase):
    def test_candidate_order_matches_the_original(self) -> None:
        """候选清单的顺序就是优先级——先命中者胜，不叠加。"""
        self.assertEqual(
            CANDIDATES_PER_DIR,
            (
                "CORECODE.md",
                "CORECODE.local.md",
                os.path.join(".corecode", "CORECODE.md"),
                "CLAUDE.md",
                "CLAUDE.local.md",
                os.path.join(".claude", "CLAUDE.md"),
            ),
        )

    def test_char_limits_match_the_original(self) -> None:
        self.assertEqual(MAX_PER_FILE_CHARS, 8_000)
        self.assertEqual(MAX_TOTAL_CHARS, 20_000)

    def test_local_variants_come_right_after_their_base_name(self) -> None:
        """`CORECODE.local.md` 紧跟 `CORECODE.md`、`CLAUDE.local.md` 紧跟 `CLAUDE.md`。

        顺序错了会改变「谁覆盖谁」——local 变体存在的意义就是覆盖基础文件。
        """
        for base, local in (("CORECODE.md", "CORECODE.local.md"), ("CLAUDE.md", "CLAUDE.local.md")):
            with self.subTest(base=base):
                self.assertEqual(
                    CANDIDATES_PER_DIR.index(local),
                    CANDIDATES_PER_DIR.index(base) + 1,
                )

class IncludeParsingTests(MemoryTestCase):
    """`@path` 引用行的判定。"""

    def _resolve(self, content: str) -> str:
        origin = self.write("origin.md", content)
        return _resolve_includes(content, origin, {origin.resolve()})

    def test_plain_include_is_expanded(self) -> None:
        self.write("target.md", "TARGET CONTENT")
        result = self._resolve("before\n@target.md\nafter")
        self.assertIn("TARGET CONTENT", result)
        self.assertIn("<!-- included from target.md -->", result)
        self.assertIn("<!-- end include target.md -->", result)

    def test_line_with_trailing_space_is_still_an_include(self) -> None:
        self.write("target.md", "TARGET CONTENT")
        result = self._resolve("@target.md   ")
        self.assertIn("TARGET CONTENT", result)

    def test_line_with_leading_space_is_still_an_include(self) -> None:
        self.write("target.md", "TARGET CONTENT")
        result = self._resolve("   @target.md")
        self.assertIn("TARGET CONTENT", result)

    def test_tab_separated_line_is_not_an_include(self) -> None:
        """**

        只判断字面空格时，`@foo\\tbar` 会被误当作引用，产出
        `include skipped: not found`——看起来像「文件不存在」，
        而实际是「这行根本不是引用」。
        """
        result = self._resolve("@foo\tbar")
        self.assertNotIn("included from", result)
        self.assertNotIn("include skipped", result)
        self.assertIn("@foo\tbar", result)

    def test_space_separated_line_is_not_an_include(self) -> None:
        result = self._resolve("@foo bar")
        self.assertNotIn("included from", result)
        self.assertNotIn("include skipped", result)

    def test_bare_at_sign_is_not_an_include(self) -> None:
        result = self._resolve("@")
        self.assertNotIn("include skipped", result)
        self.assertIn("@", result)

    def test_at_sign_in_the_middle_is_not_an_include(self) -> None:
        result = self._resolve("see @target.md for details")
        self.assertNotIn("included from", result)
        self.assertNotIn("include skipped", result)

    def test_missing_target_reports_not_found(self) -> None:
        result = self._resolve("@nope.md")
        self.assertIn("include skipped: not found nope.md", result)

    def test_include_keeps_surrounding_lines(self) -> None:
        self.write("target.md", "MIDDLE")
        result = self._resolve("FIRST\n@target.md\nLAST")
        self.assertLess(result.index("FIRST"), result.index("MIDDLE"))
        self.assertLess(result.index("MIDDLE"), result.index("LAST"))

class IncludeSafetyTests(MemoryTestCase):
    def _resolve(self, content: str) -> str:
        origin = self.write("origin.md", content)
        return _resolve_includes(content, origin, {origin.resolve()})

    def test_absolute_path_is_refused(self) -> None:
        result = self._resolve("@/etc/passwd")
        self.assertIn("include skipped: unsafe path", result)

    def test_parent_traversal_is_refused(self) -> None:
        result = self._resolve("@../secret.md")
        self.assertIn("include skipped: unsafe path", result)

    def test_nested_traversal_is_refused(self) -> None:
        result = self._resolve("@a/../../secret.md")
        self.assertIn("include skipped: unsafe path", result)

    def test_dot_slash_is_allowed(self) -> None:
        self.write("sub/target.md", "SUB CONTENT")
        result = self._resolve("@./sub/target.md")
        self.assertIn("SUB CONTENT", result)

    def test_subdirectory_include_is_allowed(self) -> None:
        self.write("sub/target.md", "SUB CONTENT")
        result = self._resolve("@sub/target.md")
        self.assertIn("SUB CONTENT", result)

class IncludeCycleTests(MemoryTestCase):
    """环判定必须是「回到祖先」而不是「见过一次」。"""

    def test_self_include_is_detected_as_a_cycle(self) -> None:
        origin = self.write("self.md", "@self.md")
        result = _resolve_includes("@self.md", origin, {origin.resolve()})
        self.assertIn("include skipped: cycle detected self.md", result)

    def test_two_file_cycle_is_detected(self) -> None:
        self.write("a.md", "@b.md")
        self.write("b.md", "@a.md")
        origin = self.write("origin.md", "@a.md")
        result = _resolve_includes("@a.md", origin, {origin.resolve()})
        self.assertIn("cycle detected", result)

    def test_same_file_from_two_branches_is_not_a_cycle(self) -> None:
        """**这条钉住一处真实差异。**

        各被包含一次。早先本实现从不移除，于是第二次会报 `cycle detected`
        ——原因说错了（那不是环），而且**内容静默缺失**。
        """
        self.write("shared.md", "SHARED CONTENT")
        self.write("left.md", "@shared.md")
        self.write("right.md", "@shared.md")
        origin = self.write("origin.md", "@left.md\n@right.md")

        result = _resolve_includes("@left.md\n@right.md", origin, {origin.resolve()})

        self.assertNotIn("cycle detected", result)
        self.assertEqual(result.count("SHARED CONTENT"), 2, result)

    def test_diamond_include_expands_both_branches(self) -> None:
        self.write("leaf.md", "LEAF")
        self.write("mid1.md", "@leaf.md")
        self.write("mid2.md", "@leaf.md")
        origin = self.write("origin.md", "@mid1.md\n@mid2.md")
        result = _resolve_includes("@mid1.md\n@mid2.md", origin, {origin.resolve()})
        self.assertEqual(result.count("LEAF"), 2, result)

class IncludeDepthTests(MemoryTestCase):
    def test_deep_chain_is_capped(self) -> None:
        # 造一条比上限更长的链：f0 -> f1 -> ... -> fN
        depth = MAX_INCLUDE_DEPTH + 5
        for index in range(depth):
            nxt = f"@f{index + 1}.md" if index + 1 < depth else "BOTTOM"
            self.write(f"f{index}.md", nxt)
        origin = self.write("origin.md", "@f0.md")
        result = _resolve_includes("@f0.md", origin, {origin.resolve()})
        self.assertIn("too deep", result)

    def test_shallow_chain_is_not_capped(self) -> None:
        self.write("f1.md", "BOTTOM")
        origin = self.write("origin.md", "@f1.md")
        result = _resolve_includes("@f1.md", origin, {origin.resolve()})
        self.assertIn("BOTTOM", result)
        self.assertNotIn("too deep", result)

    def test_depth_cap_is_reported_not_silent(self) -> None:
        """截断必须留下痕迹——静默丢弃内容会让人以为文件本来就是空的。"""
        depth = MAX_INCLUDE_DEPTH + 2
        for index in range(depth):
            nxt = f"@f{index + 1}.md" if index + 1 < depth else "BOTTOM"
            self.write(f"f{index}.md", nxt)
        origin = self.write("origin.md", "@f0.md")
        result = _resolve_includes("@f0.md", origin, {origin.resolve()})
        self.assertIn("<!-- include skipped: too deep", result)

if __name__ == "__main__":
    unittest.main(verbosity=2)
