"""编辑工具测试：`edit_file` / `patch_file` / `modify_file`。

对应原版 `edit-file.ts` · `patch-file.ts` · `modify-file.ts`。

## 测试为什么要建**仓库外**的临时目录

这些工具会**真的写文件**。历史上我因为在仓库内建临时工作区，被自己测试里的
`git reset --hard` 清空过两次未提交改动（git 会向上查找 `.git`，作用于整个
仓库）。这里一律用 `tempfile.mkdtemp()`。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.permissions import PermissionManager, PermissionPromptResult  # noqa: E402
from minicode.tooling import ToolContext  # noqa: E402
from minicode.tools.edit import (  # noqa: E402
    apply_replacement,
    edit_file_tool,
    modify_file_tool,
    patch_file_tool,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EditToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="minicode-edit-")
        self.context = ToolContext(cwd=self.root, permissions=None, signal=None)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, content: str) -> str:
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return path

    def read(self, name: str) -> str:
        with open(os.path.join(self.root, name), encoding="utf-8", newline="") as handle:
            return handle.read()


# ---------------------------------------------------------------------------
# 替换语义
# ---------------------------------------------------------------------------


class ApplyReplacementTests(unittest.TestCase):
    """替换语义是最容易搞错的地方，单独测。"""

    def test_replaces_first_occurrence_only_by_default(self) -> None:
        """**`replaceAll` 缺省是「只替换第一处」**，与 JS `String.replace` 一致。

        很多模型会以为默认替换全部——这个区别如果不测，错了也没人发现。
        """
        self.assertEqual(apply_replacement("a a a", "a", "b", False), "b a a")

    def test_replace_all_replaces_every_occurrence(self) -> None:
        self.assertEqual(apply_replacement("a a a", "a", "b", True), "b b b")

    def test_empty_replacement_deletes(self) -> None:
        self.assertEqual(apply_replacement("abc", "b", "", False), "ac")

    def test_dollar_signs_are_literal(self) -> None:
        """**D33 的护栏。**

        原版用 JS `String.replace`，它会把 `$&` 解释成「匹配到的原文」。
        本实现刻意**不做**这个展开：含 `$` 的替换（f-string、shell 变量、
        正则）必须原样写入，否则代码会被悄悄改坏。
        """
        for replacement in ("$&", "$1", "$$", "${x}", "a$&b"):
            result = apply_replacement("KEY", "KEY", replacement, False)
            self.assertEqual(
                result, replacement, f"替换内容 {replacement!r} 被特殊展开了"
            )

    def test_no_match_returns_original(self) -> None:
        self.assertEqual(apply_replacement("abc", "zzz", "x", False), "abc")

    def test_search_is_literal_not_regex(self) -> None:
        """搜索串是**字面量**，不当正则。"""
        self.assertEqual(apply_replacement("a.c", ".", "-", False), "a-c")
        self.assertEqual(apply_replacement("abc", ".", "-", True), "abc")

    def test_multiline_replacement(self) -> None:
        self.assertEqual(
            apply_replacement("x\ny\n", "x", "a\nb", False), "a\nb\ny\n"
        )

    def test_empty_search_would_corrupt_but_is_rejected_upstream(self) -> None:
        """空 search 在 Python 里会变成插入——因此校验层必须挡住它。

        这条同时说明为什么 `search` 的 schema 是 `.min(1)`。
        """
        self.assertEqual(apply_replacement("ab", "", "-", False), "-ab")


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


class EditFileTests(EditToolTestCase):
    def test_replaces_text(self) -> None:
        self.write("a.txt", "hello world\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "world", "replace": "there"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("a.txt"), "hello there\n")

    def test_search_not_found_is_a_failure(self) -> None:
        self.write("a.txt", "hello\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "zzz", "replace": "x"}, self.context
        )
        self.assertFalse(result.ok)
        self.assertIn("Text not found in a.txt", result.output)

    def test_missing_file_is_a_failure(self) -> None:
        """`edit_file` 直接读文件，因此对不存在的文件**失败**。

        这是与原版一致的行为（它用 `readFile`）。与 `modify_file` 不同——
        后者走审查路径，不存在的文件被视为空内容，因此可以创建文件。
        """
        result = edit_file_tool.run(
            {"path": "nope.txt", "search": "a", "replace": "b"}, self.context
        )
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.output)

    def test_default_replaces_only_first(self) -> None:
        self.write("a.txt", "x x x\n")
        edit_file_tool.run(
            {"path": "a.txt", "search": "x", "replace": "y"}, self.context
        )
        self.assertEqual(self.read("a.txt"), "y x x\n")

    def test_replace_all_flag(self) -> None:
        self.write("a.txt", "x x x\n")
        edit_file_tool.run(
            {"path": "a.txt", "search": "x", "replace": "y", "replaceAll": True},
            self.context,
        )
        self.assertEqual(self.read("a.txt"), "y y y\n")

    def test_identical_content_reports_no_changes(self) -> None:
        self.write("a.txt", "same\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "same", "replace": "same"}, self.context
        )
        self.assertTrue(result.ok)
        self.assertIn("无需修改", result.output)

    def test_dollar_in_replacement_is_written_literally(self) -> None:
        """D33：替换内容里的 `$` 必须原样落盘。"""
        self.write("a.txt", "PRICE = 0\n")
        edit_file_tool.run(
            {"path": "a.txt", "search": "0", "replace": "${amount}"}, self.context
        )
        self.assertEqual(self.read("a.txt"), "PRICE = ${amount}\n")


# ---------------------------------------------------------------------------
# patch_file
# ---------------------------------------------------------------------------


class PatchFileTests(EditToolTestCase):
    def test_applies_multiple_replacements_in_order(self) -> None:
        self.write("a.txt", "one two three\n")
        result = patch_file_tool.run(
            {
                "path": "a.txt",
                "replacements": [
                    {"search": "one", "replace": "1"},
                    {"search": "two", "replace": "2"},
                    {"search": "three", "replace": "3"},
                ],
            },
            self.context,
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("a.txt"), "1 2 3\n")

    def test_success_message_lists_each_replacement(self) -> None:
        """**输出格式与原版一致**：`#1 replaceOnce, #2 replaceAll`。"""
        self.write("a.txt", "a b b\n")
        result = patch_file_tool.run(
            {
                "path": "a.txt",
                "replacements": [
                    {"search": "a", "replace": "A"},
                    {"search": "b", "replace": "B", "replaceAll": True},
                ],
            },
            self.context,
        )
        self.assertTrue(result.ok)
        self.assertIn("Patched a.txt with 2 replacement(s)", result.output)
        self.assertIn("#1 replaceOnce", result.output)
        self.assertIn("#2 replaceAll", result.output)

    def test_later_replacements_see_earlier_results(self) -> None:
        """替换是**顺序**应用的：后面的 search 能匹配到前面产生的内容。"""
        self.write("a.txt", "start\n")
        result = patch_file_tool.run(
            {
                "path": "a.txt",
                "replacements": [
                    {"search": "start", "replace": "middle"},
                    {"search": "middle", "replace": "end"},
                ],
            },
            self.context,
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("a.txt"), "end\n")

    def test_missing_replacement_aborts_the_whole_patch(self) -> None:
        """**原子性**：任一替换找不到就整体放弃，不写盘。

        部分应用会留下一个半成品文件——比完全不改更难排查，因为用户看到的
        是一个「看起来改对了一半」的状态。
        """
        self.write("a.txt", "one two\n")
        result = patch_file_tool.run(
            {
                "path": "a.txt",
                "replacements": [
                    {"search": "one", "replace": "1"},
                    {"search": "zzz", "replace": "2"},
                ],
            },
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertIn("Replacement 2 not found in a.txt", result.output)
        self.assertEqual(self.read("a.txt"), "one two\n", "文件不该被改动")

    def test_missing_file_is_a_failure(self) -> None:
        result = patch_file_tool.run(
            {"path": "nope.txt", "replacements": [{"search": "a", "replace": "b"}]},
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.output)

    def test_identical_result_reports_no_changes(self) -> None:
        self.write("a.txt", "same\n")
        result = patch_file_tool.run(
            {"path": "a.txt", "replacements": [{"search": "same", "replace": "same"}]},
            self.context,
        )
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# modify_file
# ---------------------------------------------------------------------------


class ModifyFileTests(EditToolTestCase):
    def test_replaces_file_content(self) -> None:
        self.write("a.txt", "old\n")
        result = modify_file_tool.run(
            {"path": "a.txt", "content": "new\n"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("a.txt"), "new\n")

    def test_creates_a_new_file(self) -> None:
        """**与 `edit_file` 的关键差别**：不存在的文件被视为空内容。

        因此 `modify_file` 可以创建文件——这是原版就有的行为（它走
        `applyReviewedFileChange`，那里对 ENOENT 返回空串）。
        """
        result = modify_file_tool.run(
            {"path": "sub/new.txt", "content": "created\n"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("sub/new.txt"), "created\n")

    def test_identical_content_reports_no_changes(self) -> None:
        self.write("a.txt", "same\n")
        result = modify_file_tool.run(
            {"path": "a.txt", "content": "same\n"}, self.context
        )
        self.assertTrue(result.ok)
        self.assertIn("无需修改", result.output)


# ---------------------------------------------------------------------------
# 权限集成
# ---------------------------------------------------------------------------


class EditPermissionTests(EditToolTestCase):
    """权限拒绝时的行为。

    这些测试走 `ToolRegistry.execute` 而不是直接调 `tool.run`：权限拒绝会
    **抛 `PermissionDenied`**，而这个异常是在 registry 那一层被转成
    `ToolResult` 的（`tooling.py` 的 `execute`）。直接调 `run` 会让异常
    穿透到测试里——那是测试写错了，不是工具坏了。
    """

    def _manager(self, decision: str):
        from pathlib import Path

        store = Path(REPO) / "tests" / "_tmp" / "edit-permissions.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.unlink(missing_ok=True)

        def prompt(request, signal=None):  # noqa: ANN001
            return PermissionPromptResult(decision=decision)

        return PermissionManager(self.root, prompt, store_path=store)

    def _execute(self, tool, data: dict, manager):  # noqa: ANN001
        from minicode.tooling import ToolRegistry

        context = ToolContext(cwd=self.root, permissions=manager, signal=None)
        registry = ToolRegistry([tool])
        return registry.execute(tool.name, data, context)

    def test_denied_edit_does_not_write(self) -> None:
        """**底线**：审批被拒时文件绝不能被改。"""
        self.write("a.txt", "original\n")
        result = self._execute(
            edit_file_tool,
            {"path": "a.txt", "search": "original", "replace": "changed"},
            self._manager("deny_once"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.read("a.txt"), "original\n", "被拒绝却写盘了")

    def test_allowed_edit_writes(self) -> None:
        self.write("a.txt", "original\n")
        result = self._execute(
            edit_file_tool,
            {"path": "a.txt", "search": "original", "replace": "changed"},
            self._manager("allow_once"),
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self.read("a.txt"), "changed\n")

    def test_denied_patch_does_not_write(self) -> None:
        self.write("a.txt", "original\n")
        result = self._execute(
            patch_file_tool,
            {"path": "a.txt", "replacements": [{"search": "original", "replace": "x"}]},
            self._manager("deny_once"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.read("a.txt"), "original\n")

    def test_denied_modify_does_not_write(self) -> None:
        self.write("a.txt", "original\n")
        result = self._execute(
            modify_file_tool,
            {"path": "a.txt", "content": "changed\n"},
            self._manager("deny_once"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.read("a.txt"), "original\n")


class LineEndingFidelityTests(EditToolTestCase):
    """换行必须按字节保真。

    Python 的文本模式默认会转换换行（Windows 上 `\\n` → `\\r\\n`），而原版
    Node 的 `readFile`/`writeFile` 是字节级的。不显式关掉转换，一次编辑就会
    把整个 LF 文件改写成 CRLF——diff 里「每一行都变了」，用户看到文件被无端
    重写。这几条测试钉住 `newline=""`。
    """

    def _write_bytes(self, name: str, data: bytes) -> None:
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def _read_bytes(self, name: str) -> bytes:
        with open(os.path.join(self.root, name), "rb") as handle:
            return handle.read()

    def test_lf_file_stays_lf_after_edit(self) -> None:
        """**核心回归**：LF 文件编辑后仍是 LF。"""
        self._write_bytes("a.txt", b"one\ntwo\nthree\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "two", "replace": "2"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self._read_bytes("a.txt"), b"one\n2\nthree\n")

    def test_crlf_file_stays_crlf_after_edit(self) -> None:
        """CRLF 文件也不该被改写成 LF。"""
        self._write_bytes("a.txt", b"one\r\ntwo\r\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "two", "replace": "2"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self._read_bytes("a.txt"), b"one\r\n2\r\n")

    def test_lf_file_stays_lf_after_modify(self) -> None:
        self._write_bytes("a.txt", b"old\n")
        result = modify_file_tool.run({"path": "a.txt", "content": "new\n"}, self.context)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self._read_bytes("a.txt"), b"new\n")

    def test_lf_file_stays_lf_after_patch(self) -> None:
        self._write_bytes("a.txt", b"a\nb\n")
        result = patch_file_tool.run(
            {"path": "a.txt", "replacements": [{"search": "a", "replace": "A"}]},
            self.context,
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self._read_bytes("a.txt"), b"A\nb\n")

    def test_crlf_search_matches(self) -> None:
        """CRLF 文件里的多行 search 也要能匹配——说明内容确实没被归一化。"""
        self._write_bytes("a.txt", b"line1\r\nline2\r\n")
        result = edit_file_tool.run(
            {"path": "a.txt", "search": "line2", "replace": "L2"}, self.context
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(self._read_bytes("a.txt"), b"line1\r\nL2\r\n")


# ---------------------------------------------------------------------------
# 校验与 schema
# ---------------------------------------------------------------------------


class EditSchemaTests(unittest.TestCase):
    def test_edit_file_schema(self) -> None:
        schema = edit_file_tool.input_schema
        self.assertEqual(schema["required"], ["path", "search", "replace"])
        self.assertEqual(
            sorted(schema["properties"]),
            ["path", "replace", "replaceAll", "search"],
        )

    def test_edit_file_rejects_empty_search(self) -> None:
        """`search` 必须是**非空**字符串。

        空 search 在 Python 里会变成「插入到开头」，几乎不会是用户想要的。
        原版的 zod schema 也是 `.min(1)`。
        """
        with self.assertRaises(ValueError):
            edit_file_tool.validator({"path": "a", "search": "", "replace": "x"})

    def test_edit_file_rejects_empty_path(self) -> None:
        with self.assertRaises(ValueError):
            edit_file_tool.validator({"path": "", "search": "a", "replace": "b"})

    def test_edit_file_allows_empty_replace(self) -> None:
        """`replace` 可以为空——那就是删除。"""
        parsed = edit_file_tool.validator(
            {"path": "a", "search": "x", "replace": ""}
        )
        self.assertEqual(parsed["replace"], "")

    def test_edit_file_defaults_replace_all_to_false(self) -> None:
        parsed = edit_file_tool.validator({"path": "a", "search": "x", "replace": "y"})
        self.assertFalse(parsed["replaceAll"])

    def test_patch_file_requires_non_empty_replacements(self) -> None:
        with self.assertRaises(ValueError):
            patch_file_tool.validator({"path": "a", "replacements": []})

    def test_patch_file_rejects_empty_search_in_entry(self) -> None:
        with self.assertRaises(ValueError):
            patch_file_tool.validator(
                {"path": "a", "replacements": [{"search": "", "replace": "x"}]}
            )

    def test_patch_file_drops_absent_replace_all(self) -> None:
        """未提供 `replaceAll` 时不该凭空塞一个键进去。"""
        parsed = patch_file_tool.validator(
            {"path": "a", "replacements": [{"search": "s", "replace": "r"}]}
        )
        self.assertNotIn("replaceAll", parsed["replacements"][0])

    def test_patch_file_keeps_explicit_replace_all(self) -> None:
        parsed = patch_file_tool.validator(
            {
                "path": "a",
                "replacements": [{"search": "s", "replace": "r", "replaceAll": True}],
            }
        )
        self.assertTrue(parsed["replacements"][0]["replaceAll"])

    def test_modify_file_schema(self) -> None:
        schema = modify_file_tool.input_schema
        self.assertEqual(schema["required"], ["path", "content"])

    def test_modify_file_allows_empty_content(self) -> None:
        """`content` 可以为空——那是清空文件。"""
        parsed = modify_file_tool.validator({"path": "a", "content": ""})
        self.assertEqual(parsed["content"], "")

    def test_tool_names_match_the_original(self) -> None:
        self.assertEqual(edit_file_tool.name, "edit_file")
        self.assertEqual(patch_file_tool.name, "patch_file")
        self.assertEqual(modify_file_tool.name, "modify_file")

    def test_descriptions_match_the_original(self) -> None:
        """描述是给模型看的 API 契约，改动会改变模型的行为倾向。"""
        self.assertEqual(
            edit_file_tool.description, "Edit a text file by replacing exact text."
        )
        self.assertEqual(
            patch_file_tool.description,
            "Apply multiple exact-text replacements to one file in a single operation.",
        )
        self.assertEqual(
            modify_file_tool.description,
            "Replace a file with reviewed content so the user can approve the diff first.",
        )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_all_three_tools_are_registered(self) -> None:
        from minicode.tools import create_default_registry

        names = {t.name for t in create_default_registry(REPO).list()}
        self.assertIn("edit_file", names)
        self.assertIn("patch_file", names)
        self.assertIn("modify_file", names)

    def test_update_plan_is_registered_when_plan_is_given(self) -> None:
        """**这条钉住一个真实缺口。**

        `create_update_plan_tool` 早就定义了，但**从来没有被调用**——工具没
        注册，而系统提示每个回合都在让模型「用 update_plan 跟踪多步工作」。
        模型一调就得到「Unknown tool: update_plan」，Plan 功能实际不可用。
        """
        from minicode.runtime.plan import PlanManager
        from minicode.tools import create_default_registry

        registry = create_default_registry(REPO, PlanManager("probe"))
        self.assertIn("update_plan", {t.name for t in registry.list()})

    def test_update_plan_is_absent_without_a_plan(self) -> None:
        """没给 plan 时不该凭空多出一个写工具（与原版一致）。"""
        from minicode.tools import create_default_registry

        registry = create_default_registry(REPO)
        self.assertNotIn("update_plan", {t.name for t in registry.list()})

    def test_root_registry_forwards_plan(self) -> None:
        from minicode.runtime.plan import PlanManager
        from minicode.tools import create_root_registry

        registry = create_root_registry(cwd=REPO, plan=PlanManager("probe"))
        self.assertIn("update_plan", {t.name for t in registry.list()})

    def test_subagents_still_cannot_see_edit_tools(self) -> None:
        """**子 Agent 只能拿到只读子集**——编辑工具绝不能出现在里面。"""
        from minicode.tools import create_default_registry, create_worker_registry

        registry = create_default_registry(REPO)
        worker = create_worker_registry(registry)
        names = {t.name for t in worker.list()}
        for forbidden in ("edit_file", "patch_file", "modify_file", "write_file", "run_command"):
            self.assertNotIn(forbidden, names, f"子 Agent 不该看到 {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
