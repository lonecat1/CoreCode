"""运行时上下文的注入文本：Goal / Loop 两段。

## 为什么这两段需要钉住

它们被**拼进 system 消息**，是模型判断「我现在在什么模式里、该做什么」的
全部依据。而它的内容和形状都容易走样：

1. **说明文案**（7 条 Goal 指引 / 3 条 Loop 指引）是逐字抄自原版的。改一个字
   都可能改变模型的取舍，而且不会有任何测试变红——除非真的钉住全文。
2. **JSON 快照的形状**。原版是 `JSON.stringify(snapshot)`，它对 `undefined`
   的行为是**省略该键**。本实现若写成 `null`，模型看到的形状就不同了：`null`
   读起来像「有值但为空」，而不是「这件事还没发生」。

## 两个序列化器必须一致

同一份 Goal 会出现在两个地方：

    · 注入 system 消息（`build_goal_context`）
    · `update_plan` / `get_goal` 工具的输出（`goal_to_json`）

早先这是**两份手写的字段映射**，于是 `stopReason` / `completion` 在一处是
`null`、在另一处被省略，`completion` 还少了 `checkedAt`。同一份数据在模型
眼里有两种形状——那是漂移的起点。现在前者委托给后者，这条测试守住它。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from minicode.runtime.context import (  # noqa: E402
    GOAL_CONTEXT_LINES,
    LOOP_CONTEXT_LINES,
    build_goal_context,
    build_loop_context,
    format_goal,
)
from minicode.runtime.goal import GoalManager, goal_to_json  # noqa: E402
from minicode.runtime.loop import LoopScheduler  # noqa: E402
from minicode.runtime.plan import PlanManager  # noqa: E402

#: 原版 `goal/context.ts` 的 `goalContext()` 全文（7 条）。
TS_GOAL_LINES = [
    "This is a user-created Goal. Follow its original description and the latest Plan.",
    "If needsReplan is true, call update_plan with a nonempty plan, then update_goal set_criteria.",
    "An ordinary final ends this turn, not the Goal. Active Goals continue in another turn.",
    "Use ask_user for missing information. Use update_goal blocked with a reason when unable to proceed.",
    "When all Todos and criteria are satisfied, use update_goal completed with a summary and one "
    "concrete check explanation per criterion.",
    "Check explanations must describe actual results; this MVP checks structure, not evidence references.",
    "Never change the original objective, invent user approval, or treat Todo completion alone as "
    "Goal completion.",
]

#: 原版 `runtime/session-runtime.ts` 的 loop 上下文三条。
TS_LOOP_LINES = [
    "Execute this recurring prompt once, then return a final. The next run is scheduled after the interval.",
    "Use the shared Plan if useful. Do not create or update a Goal. Ask the user when input is needed.",
    "The user can stop this Loop with /loop stop. There is no model-side stop tool in this MVP.",
]


def make_loop() -> LoopScheduler:
    return LoopScheduler(
        run_turn=lambda request: None,  # noqa: ARG005
        busy=lambda: False,
        notice=lambda message: None,  # noqa: ARG005
        settle_workers=lambda: None,
    )


def payload_of(text: str) -> dict:
    """取 `<current_goal>` / `<current_loop>` 之间那一行 JSON。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("<current_"):
            return json.loads(lines[index + 1])
    raise AssertionError(f"没有找到上下文块：{text!r}")


class ContextTextTests(unittest.TestCase):
    def test_goal_lines_match_the_original_word_for_word(self) -> None:
        self.assertEqual(list(GOAL_CONTEXT_LINES), TS_GOAL_LINES)

    def test_loop_lines_match_the_original_word_for_word(self) -> None:
        self.assertEqual(list(LOOP_CONTEXT_LINES), TS_LOOP_LINES)

    def test_goal_block_has_the_original_envelope(self) -> None:
        goal = GoalManager(PlanManager("s"))
        lines = build_goal_context(goal).splitlines()
        self.assertEqual(lines[0], "<current_goal>")
        self.assertEqual(lines[2], "</current_goal>")
        self.assertEqual(lines[3:], TS_GOAL_LINES)

    def test_loop_block_has_the_original_envelope(self) -> None:
        lines = build_loop_context(None).splitlines()
        self.assertEqual(lines[0], "<current_loop>")
        self.assertEqual(lines[2], "</current_loop>")
        self.assertEqual(lines[3:], TS_LOOP_LINES)


class OptionalFieldTests(unittest.TestCase):
    """`JSON.stringify` 省略 `undefined`；本实现也必须**省略**而不是写 `null`。"""

    def test_goal_without_a_stop_reason_omits_the_key(self) -> None:
        goal = GoalManager(PlanManager("s"))
        goal.create("x")
        payload = payload_of(build_goal_context(goal))
        self.assertNotIn("stopReason", payload)
        self.assertNotIn("completion", payload)

    def test_goal_without_a_goal_serializes_to_null(self) -> None:
        """没有 Goal 时原版 `JSON.stringify(undefined)` → `null`。"""
        goal = GoalManager(PlanManager("s"))
        self.assertEqual(build_goal_context(goal).splitlines()[1], "null")

    def test_completed_goal_nests_completion_with_checked_at(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("x")
        plan.update([{"title": "a", "status": "completed"}])
        goal.update({"action": "set_criteria", "criteria": ["能跑"]})
        goal.update({"action": "completed", "summary": "好了", "checks": ["验证过"]})

        payload = payload_of(build_goal_context(goal))
        completion = payload["completion"]
        self.assertEqual(sorted(completion), ["checkedAt", "checks", "summary"])
        self.assertEqual(completion["summary"], "好了")
        self.assertEqual(completion["checks"], ["验证过"])
        self.assertTrue(completion["checkedAt"])

    def test_blocked_goal_includes_the_reason(self) -> None:
        goal = GoalManager(PlanManager("s"))
        goal.create("x")
        goal.update({"action": "blocked", "reason": "缺权限"})
        payload = payload_of(build_goal_context(goal))
        self.assertEqual(payload["stopReason"], "缺权限")

    def test_loop_without_next_run_or_stop_reason_omits_them(self) -> None:
        loop = make_loop()
        loop.create("hi", 600_000)
        payload = payload_of(build_loop_context(loop.get_snapshot()))
        self.assertIn("intervalMs", payload, "快照必须是驼峰")
        self.assertEqual(payload["intervalMs"], 600_000)
        # `nextRunAt` / `stopReason` 没有值时不该出现
        for key in ("nextRunAt", "stopReason"):
            if payload.get(key) is None:
                with self.subTest(key=key):
                    self.assertNotIn(key, payload)

    def test_no_loop_serializes_to_an_empty_object(self) -> None:
        """原版 `JSON.stringify(undefined)` 在这里被 `snapshot ?? {}` 兜成 `{}`。"""
        self.assertEqual(build_loop_context(None).splitlines()[1], "{}")
        self.assertEqual(build_loop_context(make_loop().get_snapshot()).splitlines()[1], "{}")


class SerializerAgreementTests(unittest.TestCase):
    """注入用的形状与工具输出用的形状**必须一致**。"""

    def test_context_and_tool_use_the_same_shape(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("x")
        plan.update([{"title": "a", "status": "completed"}])
        goal.update({"action": "set_criteria", "criteria": ["能跑"]})
        goal.update({"action": "completed", "summary": "好了", "checks": ["验证过"]})

        snapshot = goal.get_snapshot()
        from_context = payload_of(build_goal_context(goal))
        from_tool = goal_to_json(snapshot)
        self.assertEqual(from_context, from_tool)

    def test_without_a_goal_both_return_none(self) -> None:
        self.assertIsNone(goal_to_json(None))
        self.assertEqual(build_goal_context(GoalManager(PlanManager("s"))).splitlines()[1], "null")


class FormatGoalTests(unittest.TestCase):
    """`formatGoal` 的行结构（用户可见）。"""

    def test_no_goal_message(self) -> None:
        goal = GoalManager(PlanManager("s"))
        self.assertEqual(
            format_goal(goal, PlanManager("s").get_snapshot()),
            "No Goal. Use /goal <description> to create one.",
        )

    def test_lines_are_in_the_original_order(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("把项目跑起来")
        plan.update([{"title": "装依赖", "status": "pending"}])
        goal.update({"action": "set_criteria", "criteria": ["能启动", "测试通过"]})

        lines = format_goal(goal, plan.get_snapshot()).splitlines()
        self.assertEqual(lines[0], "Goal: active")
        self.assertEqual(lines[1], "把项目跑起来")
        self.assertEqual(lines[2], "1. 能启动")
        self.assertEqual(lines[3], "2. 测试通过")
        # 计划快照是**多行**的，从 `Plan: ` 抬头开始
        self.assertTrue(
            any(line.startswith("Plan: ") for line in lines), lines
        )
        self.assertEqual(lines[-1], "[ ] 装依赖 "
                         f"({plan.get_snapshot().todos[0].id})")

    def test_waiting_suffix(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("x")
        self.assertIn(
            "Goal: active (waiting for your answer)",
            format_goal(goal, plan.get_snapshot(), waiting=True),
        )

    def test_needs_replan_shows_planning_required(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("x")
        self.assertIn("Planning required.", format_goal(goal, plan.get_snapshot()))
        plan.update([{"title": "a", "status": "pending"}])
        goal.update({"action": "set_criteria", "criteria": ["c"]})
        self.assertNotIn("Planning required.", format_goal(goal, plan.get_snapshot()))

    def test_empty_plan_shows_plan_is_empty(self) -> None:
        plan = PlanManager("s")
        goal = GoalManager(plan)
        goal.create("x")
        self.assertIn("Plan is empty.", format_goal(goal, plan.get_snapshot()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
