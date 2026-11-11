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

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from corecode.tooling import ToolContext, ToolRegistry  # noqa: E402
from corecode.tools.files import list_files_tool  # noqa: E402

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

class GoalToolTests(unittest.TestCase):
    """`get_goal` / `update_goal` 的输出格式与 `stop` 控制位。

    ## 为什么输出格式重要

        get_goal    → JSON.stringify({ goal, plan })
        update_goal → JSON.stringify(updated)

    本实现一度只回几行人类可读摘要。对 `get_goal` 尤其致命：它**完全不含
    plan**，而模型问「当前目标和计划」正是为了看清 todo 的 id 与状态来决定
    下一步——少了 plan，这个工具几乎没用。
    """

    def setUp(self) -> None:
        from corecode.runtime.goal import GoalManager
        from corecode.runtime.plan import PlanManager
        from corecode.tools.goal import create_goal_tools

        self.plan = PlanManager("sess-1")
        self.goal = GoalManager(self.plan)
        self.tools = {tool.name: tool for tool in create_goal_tools(self.goal)}
        self.context = ToolContext(cwd=".")

    def _run(self, name: str, payload: dict):
        return self.tools[name].run(payload, self.context)

    def _json(self, name: str, payload: dict) -> dict:
        result = self._run(name, payload)
        self.assertTrue(result.ok, result.output)
        return json.loads(result.output)

    # -- get_goal -----------------------------------------------------------

    def test_get_goal_returns_goal_and_plan(self) -> None:
        """**必须同时返回 plan。**

        todo 的 id 与状态，也就无法决定下一步。
        """
        payload = self._json("get_goal", {})
        self.assertEqual(sorted(payload), ["goal", "plan"])

    def test_get_goal_without_a_goal_still_returns_the_plan(self) -> None:
        payload = self._json("get_goal", {})
        self.assertIsNone(payload["goal"])
        self.assertIn("todos", payload["plan"])

    def test_get_goal_includes_todo_ids(self) -> None:
        self.goal.create("目标")
        self.plan.update([{"title": "第一步", "status": "pending"}])
        payload = self._json("get_goal", {})
        todos = payload["plan"]["todos"]
        self.assertEqual(len(todos), 1)
        self.assertTrue(todos[0]["id"], "模型需要 id 才能精确更新")

    def test_get_goal_uses_camel_case_plan_fields(self) -> None:
        payload = self._json("get_goal", {})
        self.assertEqual(
            sorted(payload["plan"]),
            ["id", "schemaVersion", "sessionId", "todos", "updatedAt"],
        )

    # -- update_goal --------------------------------------------------------

    def test_update_goal_returns_the_goal_document(self) -> None:
        self.goal.create("目标")
        self.plan.update([{"title": "a", "status": "pending"}])
        payload = self._json("update_goal", {"action": "set_criteria", "criteria": ["能跑"]})
        self.assertEqual(
            sorted(payload),
            [
                "completionCriteria",
                "description",
                "id",
                "needsReplan",
                "planId",
                "schemaVersion",
                "sessionId",
                "status",
                "updatedAt",
            ],
        )
        self.assertEqual(payload["completionCriteria"], ["能跑"])

    def test_update_goal_does_not_stop_on_set_criteria(self) -> None:
        self.goal.create("目标")
        self.plan.update([{"title": "a", "status": "pending"}])
        result = self._run("update_goal", {"action": "set_criteria", "criteria": ["x"]})
        self.assertTrue(result.ok)
        self.assertFalse(getattr(result, "stop", False))

    def test_update_goal_completed_stops_the_turn(self) -> None:
        self.goal.create("目标")
        document = self.plan.update([{"title": "a", "status": "completed"}])
        self._run("update_goal", {"action": "set_criteria", "criteria": ["能跑"]})
        result = self._run(
            "update_goal",
            {"action": "completed", "summary": "好了", "checks": ["验证过"]},
        )
        self.assertTrue(result.ok)
        self.assertTrue(getattr(result, "stop", False), "完成意味着本回合有结论了")

    def test_completed_nests_summary_checks_and_checked_at(self) -> None:
        """

        本实现用三个平铺字段存储，但**发给模型时还原成嵌套形状**——
        """
        self.goal.create("目标")
        self.plan.update([{"title": "a", "status": "completed"}])
        self._run("update_goal", {"action": "set_criteria", "criteria": ["能跑"]})
        payload = self._json(
            "update_goal",
            {"action": "completed", "summary": "好了", "checks": ["验证过"]},
        )
        self.assertEqual(sorted(payload["completion"]), ["checkedAt", "checks", "summary"])
        self.assertEqual(payload["completion"]["summary"], "好了")
        self.assertEqual(payload["completion"]["checks"], ["验证过"])
        self.assertTrue(payload["completion"]["checkedAt"], "完成时刻必须有值")

    def test_blocked_stops_and_includes_the_reason(self) -> None:
        self.goal.create("目标")
        result = self._run("update_goal", {"action": "blocked", "reason": "缺权限"})
        self.assertTrue(result.ok)
        self.assertTrue(getattr(result, "stop", False))
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["stopReason"], "缺权限")

    def test_optional_fields_are_omitted_not_null(self) -> None:
        """`stopReason` / `completion` 无值时**省略**。

        写 `null` 会让模型以为「该字段有值但为空」。
        """
        self.goal.create("目标")
        self.plan.update([{"title": "a", "status": "pending"}])
        payload = self._json(
            "update_goal", {"action": "set_criteria", "criteria": ["x"]}
        )
        self.assertNotIn("stopReason", payload)
        self.assertNotIn("completion", payload)

    def test_unknown_action_is_a_failure(self) -> None:
        self.goal.create("目标")
        result = self._run("update_goal", {"action": "nonsense"})
        self.assertFalse(result.ok)

    def test_tool_descriptions_are_present(self) -> None:
        self.assertTrue(self.tools["get_goal"].description)
        self.assertTrue(self.tools["update_goal"].description)

class UpdatePlanToolTests(unittest.TestCase):
    """`update_plan` 的输出格式。

    ## 为什么这个格式重要

    （`JSON.stringify({ ...updated, explanation })`），本实现一度只回一句
    `"Plan updated: 1/3 completed."`。

    区别不只是「啰嗦与否」：模型下一次要**改已有条目**就必须知道它们的 id。
    只回「1/3 completed」时模型拿不到 id，于是只能靠「省略 id」新增——
    **想更新却静默变成了新增，ToDo 越改越多。**
    """

    def setUp(self) -> None:
        from corecode.runtime.plan import PlanManager
        from corecode.tools.goal import create_update_plan_tool

        self.plan = PlanManager("sess-1")
        self.tool = create_update_plan_tool(self.plan)
        self.context = ToolContext(cwd=".")

    def _run(self, payload: dict) -> str:
        result = self.tool.run(payload, self.context)
        self.assertTrue(result.ok, result.output)
        return result.output

    def test_output_is_json_with_camel_case_fields(self) -> None:
        """

        改成 snake_case 会让「同一条计划在两种实现下的表示不同」，而模型对
        字段名的预期来自它见过的训练数据。
        """
        output = self._run({"todos": [{"title": "a", "status": "pending"}]})
        payload = json.loads(output)
        self.assertEqual(
            sorted(payload),
            ["id", "schemaVersion", "sessionId", "todos", "updatedAt"],
        )
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["sessionId"], "sess-1")

    def test_output_includes_todo_ids(self) -> None:
        """**这条是核心**：模型必须能从输出里拿到 id。"""
        payload = json.loads(
            self._run({"todos": [{"title": "第一步", "status": "pending"}]})
        )
        self.assertEqual(sorted(payload["todos"][0]), ["id", "status", "title"])
        self.assertTrue(payload["todos"][0]["id"])

    def test_ids_from_the_output_can_be_used_to_update(self) -> None:
        """往返：用返回的 id 改状态，id 必须保持不变。"""
        first = json.loads(
            self._run({"todos": [{"title": "a", "status": "pending"}]})
        )
        todo_id = first["todos"][0]["id"]

        second = json.loads(
            self._run({"todos": [{"id": todo_id, "title": "a", "status": "completed"}]})
        )
        self.assertEqual(second["todos"][0]["id"], todo_id)
        self.assertEqual(second["todos"][0]["status"], "completed")

    def test_explanation_is_echoed(self) -> None:
        """schema 里有这个字段，模型传了就该收到反馈。

        静默丢弃它等于让模型以为写进去了。
        """
        payload = json.loads(
            self._run(
                {"todos": [{"title": "a", "status": "pending"}], "explanation": "先摸底"}
            )
        )
        self.assertEqual(payload["explanation"], "先摸底")

    def test_explanation_is_omitted_when_absent(self) -> None:
        payload = json.loads(self._run({"todos": [{"title": "a", "status": "pending"}]}))
        self.assertNotIn("explanation", payload)

    def test_explanation_is_omitted_when_blank(self) -> None:
        payload = json.loads(
            self._run({"todos": [{"title": "a", "status": "pending"}], "explanation": "   "})
        )
        self.assertNotIn("explanation", payload)

    def test_empty_todos_clears_the_plan(self) -> None:
        self._run({"todos": [{"title": "a", "status": "pending"}]})
        payload = json.loads(self._run({"todos": []}))
        self.assertEqual(payload["todos"], [])

    def test_invalid_todos_is_a_failure_not_an_exception(self) -> None:
        result = self.tool.run({"todos": "not a list"}, self.context)
        self.assertFalse(result.ok)
        self.assertIn("数组", result.output)

    def test_unknown_id_is_a_failure(self) -> None:
        result = self.tool.run(
            {"todos": [{"id": "nope", "title": "x", "status": "pending"}]}, self.context
        )
        self.assertFalse(result.ok)
        self.assertIn("未知", result.output)

    def test_empty_string_id_is_a_failure(self) -> None:
        """错误消息要说清「id 必须非空、新增请省略」，而不只是「未知 id」。

        只断言 `ok=False` 的话，把空 id 的校验删掉测试依然通过——它会落到
        「未知 id」那条分支上，同样失败，但原因说错了。
        """
        result = self.tool.run(
            {"todos": [{"id": "", "title": "x", "status": "pending"}]}, self.context
        )
        self.assertFalse(result.ok)
        self.assertIn("非空", result.output)
        self.assertIn("省略", result.output)

    def test_two_in_progress_is_a_failure(self) -> None:
        result = self.tool.run(
            {
                "todos": [
                    {"title": "a", "status": "in_progress"},
                    {"title": "b", "status": "in_progress"},
                ]
            },
            self.context,
        )
        self.assertFalse(result.ok)

    def test_schema_accepts_explanation(self) -> None:
        properties = self.tool.input_schema["properties"]
        self.assertIn("explanation", properties)
        self.assertEqual(self.tool.input_schema["required"], ["todos"])

    def test_schema_requires_title_and_status_per_todo(self) -> None:
        item = self.tool.input_schema["properties"]["todos"]["items"]
        self.assertEqual(item["required"], ["title", "status"])
        self.assertNotIn("id", item["required"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
