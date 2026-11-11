"""运行时编排的行为测试。

覆盖四块：
  1. Plan —— 两条约束（id 必须已知、最多一个 in_progress）；
  2. Goal —— 状态机，尤其是「模型不能改目标」这条边界与 completed 的严格校验；
  3. Loop —— 固定延迟语义、generation 作废、失败暂停；
  4. TurnRunner / SessionRuntime —— 单飞闸门与四道互斥。

Goal 与 Loop 都会起后台线程，因此测试里用可注入的 clock / 显式 join 来避免竞态。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from corecode.abort import AbortSignal
from corecode.runtime.context import (
    build_goal_context,
    build_loop_context,
    format_goal,
    inject_runtime_context,
)
from corecode.runtime.goal import (
    ACTION_BLOCKED,
    ACTION_COMPLETED,
    ACTION_SET_CRITERIA,
    GoalError,
    GoalManager,
)
from corecode.runtime.goal_controller import (
    MAX_AUTOMATIC_TURNS,
    MAX_EMPTY_CONTINUATION_TURNS,
)
from corecode.runtime.loop import (
    DEFAULT_LOOP_INTERVAL_MS,
    MAX_LOOP_INTERVAL_MS,
    LoopClock,
    LoopError,
    LoopScheduler,
    parse_loop_request,
)
from corecode.runtime.plan import (
    PlanError,
    PlanManager,
    format_plan,
    inject_plan_context,
)
from corecode.runtime.session import RuntimeError_, SessionRuntime
from corecode.runtime.turn import TurnBusyError, TurnRequest, TurnRunner
from corecode.tooling import ToolRegistry
from corecode.tools.goal import create_goal_tools
from corecode.types import AgentTurnResult

unittest.TestCase.maxDiff = None

def _result(outcome: str = "final", tool_calls: int = 0) -> AgentTurnResult:
    return AgentTurnResult(messages=[], outcome=outcome, tool_calls=tool_calls)

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager("s1")

    def test_starts_empty(self) -> None:
        self.assertEqual(self.plan.get_snapshot().todos, [])
        self.assertEqual(format_plan(self.plan.get_snapshot()), "Plan is empty.")

    def test_new_todos_get_generated_ids(self) -> None:
        document = self.plan.update(
            [{"title": "第一步", "status": "pending"}]
        )
        self.assertEqual(len(document.todos), 1)
        self.assertTrue(document.todos[0].id)
        self.assertEqual(document.todos[0].title, "第一步")

    def test_existing_ids_are_preserved_on_update(self) -> None:
        first = self.plan.update([{"title": "A", "status": "pending"}])
        todo_id = first.todos[0].id
        second = self.plan.update(
            [{"id": todo_id, "title": "A（改过）", "status": "completed"}]
        )
        self.assertEqual(second.todos[0].id, todo_id)
        self.assertEqual(second.todos[0].status, "completed")

    def test_unknown_id_is_rejected(self) -> None:
        """否则模型会以为在更新、实际在新增，计划悄悄膨胀。"""
        with self.assertRaises(PlanError):
            self.plan.update([{"id": "nope", "title": "x", "status": "pending"}])

    def test_empty_string_id_is_rejected(self) -> None:
        """**空字符串 id 必须拒绝，不能当成「省略 id」。**

        「完全不传」。早先这里把 `""` 也当成「省略」，于是模型想**更新**
        某个条目却写了个空 id 时，会**静默变成新增**，ToDo 越改越多。

        **断言消息而不只是异常类型。** 若不校验空 id，代码会落到「未知 id」
        那条分支上——同样抛 PlanError，但原因说错了。只断言类型的话，
        把校验删掉测试依然通过。
        """
        with self.assertRaises(PlanError) as caught:
            self.plan.update([{"id": "", "title": "x", "status": "pending"}])
        message = str(caught.exception)
        self.assertIn("非空", message, f"应指出「id 必须非空」，实际：{message}")
        self.assertIn("省略", message, f"应指引「新增请省略 id」，实际：{message}")

        with self.assertRaises(PlanError) as caught:
            self.plan.update([{"id": "   ", "title": "x", "status": "pending"}])
        self.assertIn("非空", str(caught.exception))

    def test_non_string_id_is_rejected(self) -> None:
        with self.assertRaises(PlanError) as caught:
            self.plan.update([{"id": 123, "title": "x", "status": "pending"}])
        self.assertIn("非空", str(caught.exception))

    def test_omitting_the_id_still_creates_a_new_todo(self) -> None:
        """拒绝空字符串不等于拒绝「省略」——后者是新增的正确写法。"""
        document = self.plan.update([{"title": "新的", "status": "pending"}])
        self.assertEqual(len(document.todos), 1)
        self.assertTrue(document.todos[0].id)

    def test_duplicate_id_is_rejected(self) -> None:
        first = self.plan.update([{"title": "A", "status": "pending"}])
        todo_id = first.todos[0].id
        with self.assertRaises(PlanError):
            self.plan.update(
                [
                    {"id": todo_id, "title": "A", "status": "pending"},
                    {"id": todo_id, "title": "A2", "status": "pending"},
                ]
            )

    def test_only_one_in_progress_allowed(self) -> None:
        """防止模型「并行开工」然后什么都不做完。"""
        with self.assertRaises(PlanError):
            self.plan.update(
                [
                    {"title": "A", "status": "in_progress"},
                    {"title": "B", "status": "in_progress"},
                ]
            )

    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.plan.update([{"title": "A", "status": "done"}])

    def test_empty_title_is_rejected(self) -> None:
        with self.assertRaises(PlanError):
            self.plan.update([{"title": "   ", "status": "pending"}])

    def test_full_replacement_semantics(self) -> None:
        """提交的是完整清单：没提到的条目会被移除。"""
        self.plan.update(
            [{"title": "A", "status": "pending"}, {"title": "B", "status": "pending"}]
        )
        document = self.plan.update([{"title": "只留这个", "status": "pending"}])
        self.assertEqual(len(document.todos), 1)

    def test_snapshot_is_a_copy(self) -> None:
        self.plan.update([{"title": "A", "status": "pending"}])
        snapshot = self.plan.get_snapshot()
        snapshot.todos[0].title = "被改了"
        self.assertEqual(self.plan.get_snapshot().todos[0].title, "A")

    def test_reset_clears_and_changes_plan_id(self) -> None:
        self.plan.update([{"title": "A", "status": "pending"}])
        before = self.plan.get_snapshot().id
        self.plan.reset("s2")
        self.assertEqual(self.plan.get_snapshot().todos, [])
        self.assertNotEqual(self.plan.get_snapshot().id, before)

    def test_format_plan_shows_progress_and_markers(self) -> None:
        self.plan.update(
            [
                {"title": "做完的", "status": "completed"},
                {"title": "在做的", "status": "in_progress"},
                {"title": "还没做", "status": "pending"},
            ]
        )
        text = format_plan(self.plan.get_snapshot())
        self.assertIn("1/3 completed", text)
        self.assertIn("[x] 做完的", text)
        self.assertIn("[>] 在做的", text)
        self.assertIn("[ ] 还没做", text)

    def test_subscribe_fires_on_update(self) -> None:
        seen: list[int] = []
        self.plan.subscribe(lambda: seen.append(1))
        self.plan.update([{"title": "A", "status": "pending"}])
        self.assertEqual(len(seen), 1)

class PlanContextTests(unittest.TestCase):
    def test_context_is_appended_to_existing_system_message(self) -> None:
        """必须追加到**已有** system 消息，否则每回合都会新增一条。"""
        plan = PlanManager("s1")
        plan.update([{"title": "A", "status": "pending"}])
        messages = [
            {"role": "system", "content": "原始系统提示"},
            {"role": "user", "content": "hi"},
        ]
        result = inject_plan_context(messages, plan.get_snapshot())
        self.assertEqual(len(result), 2, "不应新增消息")
        self.assertTrue(result[0]["content"].startswith("原始系统提示"))
        self.assertIn("Current session Plan / Todo:", result[0]["content"])

    def test_context_creates_system_message_when_absent(self) -> None:
        plan = PlanManager("s1")
        result = inject_plan_context([{"role": "user", "content": "hi"}], plan.get_snapshot())
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(len(result), 2)

# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

class GoalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager("s1")
        self.goal = GoalManager(self.plan)

    def _plan_and_criteria(self, criteria=("全部测试通过",)) -> None:
        self.plan.update([{"title": "跑测试", "status": "completed"}])
        self.goal.update({"action": ACTION_SET_CRITERIA, "criteria": list(criteria)})

    def test_create_starts_active_and_needs_replan(self) -> None:
        document = self.goal.create("重构这个模块")
        self.assertEqual(document.status, "active")
        self.assertTrue(document.needs_replan)

    def test_cannot_create_two_goals(self) -> None:
        self.goal.create("第一个")
        with self.assertRaises(GoalError):
            self.goal.create("第二个")

    def test_empty_description_is_rejected(self) -> None:
        with self.assertRaises(GoalError):
            self.goal.create("   ")

    def test_set_criteria_requires_a_nonempty_plan_first(self) -> None:
        """强制模型先规划再声明标准，否则标准会凭空而来。"""
        self.goal.create("做点事")
        with self.assertRaises(GoalError):
            self.goal.update({"action": ACTION_SET_CRITERIA, "criteria": ["x"]})

    def test_set_criteria_clears_needs_replan(self) -> None:
        self.goal.create("做点事")
        self._plan_and_criteria(("标准一", "标准二"))
        document = self.goal.get_snapshot()
        self.assertFalse(document.needs_replan)
        self.assertEqual(document.completion_criteria, ["标准一", "标准二"])

    def test_blocked_records_reason(self) -> None:
        self.goal.create("做点事")
        document = self.goal.update({"action": ACTION_BLOCKED, "reason": "缺少 API key"})
        self.assertEqual(document.status, "blocked")
        self.assertEqual(document.stop_reason, "缺少 API key")

    def test_blocked_requires_reason(self) -> None:
        self.goal.create("做点事")
        with self.assertRaises(GoalError):
            self.goal.update({"action": ACTION_BLOCKED, "reason": "  "})

    def test_completed_requires_all_todos_done(self) -> None:
        self.goal.create("做点事")
        self.plan.update([{"title": "还没做完", "status": "pending"}])
        self.goal.update({"action": ACTION_SET_CRITERIA, "criteria": ["标准"]})
        with self.assertRaises(GoalError) as ctx:
            self.goal.update(
                {"action": ACTION_COMPLETED, "summary": "做完了", "checks": ["已确认"]}
            )
        self.assertIn("未完成的 Todo", str(ctx.exception))

    def test_completed_requires_one_check_per_criterion(self) -> None:
        """不许用「整体感觉完成了」糊弄过去。"""
        self.goal.create("做点事")
        self._plan_and_criteria(("标准一", "标准二"))
        with self.assertRaises(GoalError) as ctx:
            self.goal.update(
                {"action": ACTION_COMPLETED, "summary": "完成", "checks": ["只有一条"]}
            )
        self.assertIn("2 条标准", str(ctx.exception))

    def test_completed_succeeds_with_matching_checks(self) -> None:
        self.goal.create("做点事")
        self._plan_and_criteria(("标准一", "标准二"))
        document = self.goal.update(
            {
                "action": ACTION_COMPLETED,
                "summary": "两件事都做完了",
                "checks": ["实测通过", "回归通过"],
            }
        )
        self.assertEqual(document.status, "completed")
        self.assertEqual(document.completion_checks, ["实测通过", "回归通过"])

    def test_completed_requires_summary(self) -> None:
        self.goal.create("做点事")
        self._plan_and_criteria()
        with self.assertRaises(GoalError):
            self.goal.update({"action": ACTION_COMPLETED, "summary": " ", "checks": ["x"]})

    def test_cannot_update_non_active_goal(self) -> None:
        self.goal.create("做点事")
        self.goal.pause("用户暂停")
        with self.assertRaises(GoalError):
            self.goal.update({"action": ACTION_BLOCKED, "reason": "x"})

    def test_unknown_action_is_rejected(self) -> None:
        """模型不能创建或改目标：只有三个动作。"""
        self.goal.create("做点事")
        with self.assertRaises(GoalError) as ctx:
            self.goal.update({"action": "change_objective", "description": "换个目标"})
        # 必须是被**动作白名单**拒绝，而不是碰巧因为别的条件失败——否则把白名单
        # 放开后这条测试依然会通过（变异检查发现的空洞）。
        self.assertIn("未知的 action", str(ctx.exception))

    def test_every_action_outside_the_whitelist_is_rejected(self) -> None:
        """逐个验证非法动作：任何新增动作都必须被白名单挡住。"""
        self.goal.create("做点事")
        for action in (
            "change_objective",
            "edit_objective",
            "set_goal",
            "pause",
            "resume",
            "clear",
            "create",
            "",
            None,
        ):
            with self.subTest(action=action):
                with self.assertRaises(GoalError) as ctx:
                    self.goal.update({"action": action})
                self.assertIn("未知的 action", str(ctx.exception))

    def test_pause_then_resume(self) -> None:
        self.goal.create("做点事")
        self.goal.pause("稍后")
        self.assertEqual(self.goal.get_snapshot().status, "paused")
        self.goal.resume()
        self.assertEqual(self.goal.get_snapshot().status, "active")

    def test_resume_rejects_completed_goal(self) -> None:
        self.goal.create("做点事")
        self._plan_and_criteria()
        self.goal.update({"action": ACTION_COMPLETED, "summary": "完成", "checks": ["已确认"]})
        with self.assertRaises(GoalError):
            self.goal.resume()

    def test_clear_removes_goal(self) -> None:
        self.goal.create("做点事")
        self.goal.clear()
        self.assertIsNone(self.goal.get_snapshot())

    def test_plan_replacement_clears_goal(self) -> None:
        """计划被整体替换 → 目标失去依据 → 清除而不是悄悄沿用。"""
        self.goal.create("做点事")
        self.assertIsNotNone(self.goal.get_snapshot())
        self.plan.reset("s2")
        self.assertIsNone(self.goal.get_snapshot())

    def test_plan_edit_while_paused_forces_replan(self) -> None:
        self.goal.create("做点事")
        self.plan.update([{"title": "A", "status": "pending"}])
        self.goal.update({"action": ACTION_SET_CRITERIA, "criteria": ["标准"]})
        self.goal.pause("暂停")

        # 同一份计划上继续修改：Goal 必须重新规划。
        self.plan.update(
            [{"title": "A", "status": "pending"}, {"title": "B", "status": "pending"}]
        )
        document = self.goal.get_snapshot()
        self.assertEqual(document.status, "paused")
        self.assertTrue(document.needs_replan)

    def test_complete_requires_not_needs_replan(self) -> None:
        self.goal.create("做点事")
        self.plan.update([{"title": "A", "status": "completed"}])
        # 故意跳过 set_criteria，needs_replan 仍为真
        with self.assertRaises(GoalError):
            self.goal.update(
                {"action": ACTION_COMPLETED, "summary": "完成", "checks": ["x"]}
            )

class GoalContextTests(unittest.TestCase):
    def test_context_states_the_ownership_boundary(self) -> None:
        plan = PlanManager("s1")
        goal = GoalManager(plan)
        goal.create("重构模块")
        text = build_goal_context(goal)
        self.assertIn("<current_goal>", text)
        self.assertIn("重构模块", text)
        # 关键约束必须在上下文里明说，否则模型会自己定义完成
        self.assertIn("Never change the original objective", text)

    def test_format_goal_without_goal(self) -> None:
        plan = PlanManager("s1")
        self.assertIn("No Goal", format_goal(GoalManager(plan), plan.get_snapshot()))

    def test_format_goal_includes_criteria_and_plan(self) -> None:
        plan = PlanManager("s1")
        goal = GoalManager(plan)
        goal.create("做点事")
        plan.update([{"title": "A", "status": "pending"}])
        goal.update({"action": ACTION_SET_CRITERIA, "criteria": ["标准一"]})
        text = format_goal(goal, plan.get_snapshot())
        self.assertIn("1. 标准一", text)
        self.assertIn("Plan:", text)

# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

class ParseLoopRequestTests(unittest.TestCase):
    """

                           旧版 → 间隔 10 分钟、prompt "1.5h do it"
                           旧版 → 间隔 10 分钟、prompt "5x hello"

    用户想要 90 分钟的循环，实际拿到 10 分钟——不报错，只是不对。
    """

    def test_prompt_only_uses_default_interval(self) -> None:
        prompt, interval = parse_loop_request("检查构建状态")
        self.assertEqual(prompt, "检查构建状态")
        self.assertEqual(interval, DEFAULT_LOOP_INTERVAL_MS)

    def test_minutes_and_hours(self) -> None:
        self.assertEqual(parse_loop_request("5m 跑测试")[1], 5 * 60_000)
        self.assertEqual(parse_loop_request("2h 跑测试")[1], 2 * 60 * 60_000)

    def test_decimal_intervals_are_supported(self) -> None:
        """

        `1.5h` 是 90 分钟。旧文法不认它，于是把它当成 prompt 并用默认的
        10 分钟间隔——用户要 90 分钟却得到 10 分钟。
        """
        self.assertEqual(parse_loop_request("1.5h 跑测试"), ("跑测试", 90 * 60_000))
        self.assertEqual(parse_loop_request("2.5m 跑测试"), ("跑测试", 150_000))

    def test_combined_units_are_rejected_like_the_original(self) -> None:
        """

        本实现一度额外支持它，但那与「数字前缀就报错」这条规则冲突——
        """
        with self.assertRaises(LoopError):
            parse_loop_request("1h30m 跑测试")

    def test_numeric_looking_prefixes_are_rejected(self) -> None:
        """**「像数字但格式不对」必须报错，不能当成 prompt。**

        少了这条规则，`/loop 5x hello` 会变成「10 分钟循环、prompt 是
        `5x hello`」——用户以为设了间隔，实际拿到默认值，而且 prompt 里
        混进了一段他没想发出去的文本。
        """
        for body in ("5x hello", "-5m x", "+3m x", "Infinity h x", "NaN h x", ".5m x"):
            with self.subTest(body=body):
                with self.assertRaises(LoopError):
                    parse_loop_request(body)

    def test_prompt_is_preserved(self) -> None:
        self.assertEqual(parse_loop_request("10m 检查 构建 状态")[0], "检查 构建 状态")

    def test_interval_below_minimum_is_rejected(self) -> None:
        with self.assertRaises(LoopError):
            parse_loop_request("0m 太快了")
        # 0.5 分钟 = 30 秒，也不够
        with self.assertRaises(LoopError):
            parse_loop_request("0.5m 太快了")

    def test_interval_above_the_timer_limit_is_rejected(self) -> None:
        hard_limit_minutes = MAX_LOOP_INTERVAL_MS // 60_000 + 1
        with self.assertRaises(LoopError):
            parse_loop_request(f"{hard_limit_minutes}m 太久")

    def test_missing_prompt_after_interval_is_rejected(self) -> None:
        with self.assertRaises(LoopError):
            parse_loop_request("5m")

    def test_empty_body_is_rejected(self) -> None:
        with self.assertRaises(LoopError):
            parse_loop_request("   ")

    def test_boundary_intervals_are_accepted(self) -> None:
        """正好 1 分钟可用；正好等于上限也可用。"""
        self.assertEqual(parse_loop_request("1m 跑")[1], 60_000)
        exact = MAX_LOOP_INTERVAL_MS // 60_000
        self.assertEqual(parse_loop_request(f"{exact}m 跑")[1], exact * 60_000)

    def test_non_numeric_prefix_is_a_prompt(self) -> None:
        """不以数字开头就不是间隔——整段都是 prompt。"""
        self.assertEqual(
            parse_loop_request("检查 状态"), ("检查 状态", DEFAULT_LOOP_INTERVAL_MS)
        )
        self.assertEqual(
            parse_loop_request("v2 跑测试"), ("v2 跑测试", DEFAULT_LOOP_INTERVAL_MS)
        )

class ManualClock(LoopClock):
    """记录被安排的回调，由测试手动触发。"""

    def __init__(self) -> None:
        self.timers: list[tuple[object, float]] = []
        self._now = 1000.0
        super().__init__(
            now=lambda: self._now,
            set_timer=self._set_timer,
            clear_timer=self._clear_timer,
        )

    def _set_timer(self, callback, delay_ms):  # noqa: ANN001
        handle = object()
        self.timers.append((handle, delay_ms, callback))
        return handle

    def _clear_timer(self, handle) -> None:  # noqa: ANN001
        self.timers = [t for t in self.timers if t[0] is not handle]

    def fire(self) -> None:
        handle, _delay, callback = self.timers.pop(0)
        callback()

class LoopSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.turns: list[tuple[dict, str]] = []
        self.outcomes: list[str] = ["final"]
        self.notices: list[str] = []
        self.busy_flag = False

    def _build(self) -> LoopScheduler:
        def run_turn(payload, mode, signal):  # noqa: ANN001
            self.turns.append((payload, mode))
            outcome = self.outcomes.pop(0) if self.outcomes else "final"
            return _result(outcome, tool_calls=1)

        return LoopScheduler(
            run_turn=run_turn,
            busy=lambda: self.busy_flag,
            notice=self.notices.append,
            settle_workers=lambda: None,
            clock=self.clock,
        )

    def test_create_runs_first_turn_immediately(self) -> None:
        loop = self._build()
        loop.create("检查一下", 60_000)
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.turns[0][1], "loop")
        loop.stop()

    def test_next_run_is_scheduled_after_completion(self) -> None:
        """固定延迟：从本轮结束后起算，而不是固定周期触发。"""
        loop = self._build()
        loop.create("检查", 60_000)
        self.assertEqual(len(self.clock.timers), 1)
        self.assertEqual(self.clock.timers[0][1], 60_000)
        loop.stop()

    def test_firing_the_timer_starts_the_next_turn(self) -> None:
        loop = self._build()
        loop.create("检查", 60_000)
        loop.stop()
        self.assertEqual(len(self.turns), 1)

    def test_busy_prevents_kick_and_retries_later(self) -> None:
        self.busy_flag = True
        loop = self._build()
        loop.create("检查", 60_000)
        self.assertEqual(len(self.turns), 0, "忙碌时不应起跑")
        self.assertTrue(loop.has_pending_trigger)

        self.busy_flag = False
        loop.kick()
        self.assertEqual(len(self.turns), 1)
        loop.stop()

    def test_awaiting_user_suspends_and_answer_resumes(self) -> None:
        self.outcomes = ["awaiting_user", "final"]
        loop = self._build()
        loop.create("需要确认", 60_000)
        self.assertTrue(loop.waiting_for_answer)

        loop.answer("继续吧")
        self.assertFalse(loop.waiting_for_answer)
        self.assertEqual(len(self.turns), 2)
        loop.stop()

    def test_failed_turn_pauses_the_loop(self) -> None:
        self.outcomes = ["failed"]
        loop = self._build()
        loop.create("检查", 60_000)
        self.assertFalse(loop.enabled, "失败后应暂停而不是继续跑")
        self.assertTrue(self.notices)
        loop.stop()

    def test_stop_cancels_timer_and_clears_document(self) -> None:
        loop = self._build()
        loop.create("检查", 60_000)
        loop.stop()
        self.assertIsNone(loop.get_snapshot())
        self.assertEqual(self.clock.timers, [])

    def test_generation_invalidates_stale_timer_callbacks(self) -> None:
        """stop 之后旧定时器仍然醒来时，必须什么都不做。"""
        loop = self._build()
        loop.create("检查", 60_000)
        stale = self.clock.timers[0][2]
        loop.stop()

        self.turns.clear()
        stale()  # 模拟旧定时器醒来
        self.assertEqual(len(self.turns), 0, "作废的回调不应起跑")

    def test_cannot_create_two_loops(self) -> None:
        loop = self._build()
        loop.create("第一个", 60_000)
        with self.assertRaises(LoopError):
            loop.create("第二个", 60_000)
        loop.stop()

    def test_format_reports_state(self) -> None:
        loop = self._build()
        self.assertIn("没有 Loop", loop.format())
        loop.create("检查构建", 5 * 60_000)
        text = loop.format()
        self.assertIn("active", text)
        self.assertIn("5m", text)
        self.assertIn("检查构建", text)
        loop.stop()

    def test_loop_context_mentions_no_goal(self) -> None:
        """Loop 模式下必须明说「不要创建 Goal」，否则模型会越权。"""
        text = build_loop_context({"prompt": "x", "status": "active"})
        self.assertIn("<current_loop>", text)
        self.assertIn("Do not create or update a Goal", text)

# ---------------------------------------------------------------------------
# TurnRunner
# ---------------------------------------------------------------------------

class TurnRunnerTests(unittest.TestCase):
    def test_single_flight_rejects_second_run(self) -> None:
        runner = TurnRunner()
        started = threading.Event()
        release = threading.Event()

        def slow(signal):  # noqa: ANN001
            started.set()
            release.wait(2.0)
            return _result()

        thread = threading.Thread(target=lambda: runner.run(slow), daemon=True)
        thread.start()
        self.assertTrue(started.wait(2.0))

        with self.assertRaises(TurnBusyError):
            runner.run(lambda signal: _result())

        release.set()
        thread.join(2.0)
        self.assertFalse(runner.busy)

    def test_records_awaiting_user(self) -> None:
        runner = TurnRunner()
        runner.run(lambda signal: _result("awaiting_user"))
        self.assertTrue(runner.awaiting_user)
        runner.run(lambda signal: _result("final"))
        self.assertFalse(runner.awaiting_user)

    def test_stop_aborts_the_running_turn(self) -> None:
        runner = TurnRunner()
        seen: list[bool] = []
        started = threading.Event()

        def body(signal):  # noqa: ANN001
            started.set()
            signal.wait(2.0)
            seen.append(signal.is_set())
            return _result("aborted")

        thread = threading.Thread(target=lambda: runner.run(body), daemon=True)
        thread.start()
        self.assertTrue(started.wait(2.0))
        runner.stop("用户取消")
        thread.join(2.0)
        self.assertEqual(seen, [True], "取消信号应传到回合内部")

    def test_parent_signal_propagates(self) -> None:
        runner = TurnRunner()
        parent = AbortSignal()

        def body(signal):  # noqa: ANN001
            parent.abort("父信号")
            signal.wait(1.0)
            return _result("aborted" if signal.is_set() else "final")

        result = runner.run(body, parent)
        self.assertEqual(result.outcome, "aborted")

# ---------------------------------------------------------------------------
# SessionRuntime：四道互斥闸门
# ---------------------------------------------------------------------------

class SessionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executed: list[TurnRequest] = []
        self.notices: list[str] = []
        self.plan = PlanManager("s1")
        self.runtime = SessionRuntime(
            plan=self.plan,
            tools=ToolRegistry([]),
            execute=self._execute,
            record_answer=lambda payload: None,
            notice=self.notices.append,
            settle_workers=lambda: None,
            loop_clock=ManualClock(),
        )

    def _execute(self, request: TurnRequest) -> AgentTurnResult:
        self.executed.append(request)
        # 模拟一个「真在干活」的 Goal 回合：它一直在调用工具，因此不会触发
        # 空转暂停；同时它会一直跑到被取消为止——这正是真实的长目标行为。
        #
        # 用阻塞而非快速返回来模拟，是因为快速返回会让控制器在毫秒内烧掉
        # 自动回合预算，测试还没来得及断言就看到 Goal 被暂停了。
        if request.mode == "goal":
            signal = request.signal
            if signal is not None:
                signal.wait(20.0)
            return _result("aborted" if (signal and signal.is_set()) else "final", tool_calls=1)
        return _result("final", tool_calls=1)

    def _stop_goal(self, timeout: float = 8.0) -> None:
        """中止 Goal 控制器线程并等它结束。

        比 `when_idle(timeout)` 更有用：后者超时后测试会继续跑，
        症状是「整个套件莫名变慢」，很难定位。
        """
        controller = self.runtime.goal._controller  # noqa: SLF001 - 测试需观察内部状态
        if controller is not None:
            controller.abort("test cleanup")
        thread = self.runtime.goal._thread  # noqa: SLF001
        if thread is not None:
            thread.join(timeout)
            self.assertFalse(
                thread.is_alive(), "Goal 线程未在预期时间内结束（可能死锁）"
            )

    # -- 闸门 1：submit -----------------------------------------------------

    def test_plain_submit_runs_a_turn(self) -> None:
        self.runtime.submit("你好")
        self.assertEqual(len(self.executed), 1)
        self.assertIsNone(self.executed[0].mode)

    def test_submit_rejected_while_goal_is_active(self) -> None:
        self.runtime.command("/goal 做点事")
        self._stop_goal()
        # 此时 Goal 已 active（即使控制器已结束循环）
        with self.assertRaises(RuntimeError_):
            self.runtime.submit("插一句话")

    # -- 闸门 2：command ---------------------------------------------------

    def test_loop_cannot_be_created_while_goal_active(self) -> None:
        self.runtime.command("/goal 做点事")
        self._stop_goal()
        with self.assertRaises(RuntimeError_) as ctx:
            self.runtime.command("/loop 5m 检查")
        self.assertIn("Goal", str(ctx.exception))

    # -- 闸门 3：assert_idle ------------------------------------------------

    def test_assert_idle_rejects_when_loop_enabled(self) -> None:
        self.runtime.command("/loop 5m 检查")
        with self.assertRaises(RuntimeError_):
            self.runtime.assert_idle()
        self.runtime.command("/loop stop")

    def test_assert_idle_passes_when_free(self) -> None:
        self.runtime.assert_idle()  # 不抛异常即通过

    # -- 闸门 4：Loop 的 busy 包含 Goal --------------------------------

    def test_loop_busy_includes_goal_enabled(self) -> None:
        """决定性的一条：Goal 活跃期间 Loop 永远不会被 kick。"""
        self.assertFalse(self.runtime._loop_busy())
        self.runtime.command("/goal 做点事")
        self.assertTrue(self.runtime._loop_busy())
        self._stop_goal()

    # -- 命令分派 -----------------------------------------------------------

    def test_unknown_command_returns_none(self) -> None:
        self.assertIsNone(self.runtime.command("/tools"))
        self.assertIsNone(self.runtime.command("普通输入"))

    def test_goal_status_without_goal(self) -> None:
        self.assertIn("No Goal", self.runtime.command("/goal") or "")

    def test_goal_clear_reports_plan_retained(self) -> None:
        self.runtime.command("/goal 做点事")
        self._stop_goal()
        self.assertIn("Plan 保留", self.runtime.command("/goal clear") or "")

    def test_goal_update_is_refused(self) -> None:
        """MVP 不允许改目标——目标的所有权在用户手里。"""
        text = self.runtime.command("/goal update 换个目标") or ""
        self.assertIn("保持原始目标不变", text)

    def test_loop_stop_when_none(self) -> None:
        self.assertIn("Loop", self.runtime.command("/loop") or "")

    # -- 工具与上下文 -------------------------------------------------------

    def test_goal_tools_only_visible_in_goal_mode(self) -> None:
        normal = {t.name for t in self.runtime.tools_for(None).list()}
        goal = {t.name for t in self.runtime.tools_for("goal").list()}
        self.assertNotIn("update_goal", normal)
        self.assertIn("update_goal", goal)
        self.assertIn("get_goal", goal)

    def test_context_for_each_mode(self) -> None:
        self.assertEqual(self.runtime.context_for(None), "")
        self.runtime.command("/goal 做点事")
        self.assertIn("<current_goal>", self.runtime.context_for("goal"))
        self._stop_goal()
        self.runtime.command("/goal clear")
        self.runtime.command("/loop 5m 检查")
        self.assertIn("<current_loop>", self.runtime.context_for("loop"))
        self.runtime.command("/loop stop")

    # -- 停止与重置 ---------------------------------------------------------

    def test_stop_clears_loop_and_pauses_goal(self) -> None:
        self.runtime.command("/loop 5m 检查")
        self.runtime.stop()
        self.assertFalse(self.runtime.loop.enabled)

    def test_reset_clears_goal(self) -> None:
        self.runtime.command("/goal 做点事")
        self._stop_goal()
        self.runtime.reset()
        self.assertIsNone(self.runtime.goal.manager.get_snapshot())

# ---------------------------------------------------------------------------
# Goal 自动推进
# ---------------------------------------------------------------------------

class GoalContinuationTests(unittest.TestCase):
    def test_final_does_not_end_the_goal(self) -> None:
        """核心语义：一个 final 只结束回合，Goal 会继续起下一轮。"""
        calls: list[str] = []
        plan = PlanManager("s1")
        results = ["final", "final", "final"]

        def run_turn(request: TurnRequest) -> AgentTurnResult:
            calls.append(str(request.input.get("content")))
            outcome = results.pop(0) if results else "final"
            # 第二轮起让 Goal 变成 completed，终止循环
            if len(calls) >= 3:
                goal_manager.update(
                    {"action": ACTION_SET_CRITERIA, "criteria": ["标准"]}
                )
            return _result(outcome, tool_calls=1)

        goal_manager = GoalManager(plan)
        from corecode.runtime.goal_controller import GoalController

        controller = GoalController(
            goal_manager,
            turns=TurnRunner(),
            run_turn=run_turn,
            record_answer=lambda payload: None,
            notice=lambda text: None,
            settle_workers=lambda: None,
        )
        goal_manager.create("做点事")
        controller._start({"role": "user", "content": "/goal 做点事"})
        controller.when_idle(timeout=5.0)

        self.assertGreaterEqual(len(calls), 2, "final 之后应当继续起下一轮")
        self.assertIn("Continue the active Goal", calls[1])

    def test_ask_user_suspends_the_goal(self) -> None:
        plan = PlanManager("s1")
        goal_manager = GoalManager(plan)
        from corecode.runtime.goal_controller import GoalController

        controller = GoalController(
            goal_manager,
            turns=TurnRunner(),
            run_turn=lambda request: _result("awaiting_user"),
            record_answer=lambda payload: None,
            notice=lambda text: None,
            settle_workers=lambda: None,
        )
        goal_manager.create("做点事")
        controller._start({"role": "user", "content": "/goal 做点事"})
        controller.when_idle(timeout=5.0)
        self.assertTrue(controller.waiting_for_answer)

    def test_empty_continuations_pause_the_goal(self) -> None:
        """连续空转必须自动停下，否则会永远重复「我继续努力」。"""
        plan = PlanManager("s1")
        goal_manager = GoalManager(plan)
        notices: list[str] = []
        from corecode.runtime.goal_controller import GoalController

        controller = GoalController(
            goal_manager,
            turns=TurnRunner(),
            run_turn=lambda request: _result("final", tool_calls=0),
            record_answer=lambda payload: None,
            notice=notices.append,
            settle_workers=lambda: None,
        )
        goal_manager.create("做点事")
        controller._start({"role": "user", "content": "/goal 做点事"})
        controller.when_idle(timeout=5.0)

        snapshot = goal_manager.get_snapshot()
        self.assertEqual(snapshot.status, "paused")
        self.assertIn(str(MAX_EMPTY_CONTINUATION_TURNS), snapshot.stop_reason or "")

    def test_completed_goal_stops_the_loop(self) -> None:
        plan = PlanManager("s1")
        goal_manager = GoalManager(plan)
        calls: list[int] = []
        from corecode.runtime.goal_controller import GoalController

        def run_turn(request: TurnRequest) -> AgentTurnResult:
            calls.append(1)
            if len(calls) == 1:
                plan.update([{"title": "做", "status": "completed"}])
                goal_manager.update({"action": ACTION_SET_CRITERIA, "criteria": ["标准"]})
                return _result("final", tool_calls=1)
            goal_manager.update(
                {"action": ACTION_COMPLETED, "summary": "完成", "checks": ["已确认"]}
            )
            return _result("controlled_stop", tool_calls=1)

        controller = GoalController(
            goal_manager,
            turns=TurnRunner(),
            run_turn=run_turn,
            record_answer=lambda payload: None,
            notice=lambda text: None,
            settle_workers=lambda: None,
        )
        goal_manager.create("做点事")
        controller._start({"role": "user", "content": "/goal 做点事"})
        controller.when_idle(timeout=5.0)

        self.assertEqual(goal_manager.get_snapshot().status, "completed")
        self.assertEqual(len(calls), 2, "完成后不应再起新回合")

    def test_automatic_turn_budget_bounds_the_loop(self) -> None:
        """兜底闸门：模型每轮都在调工具、又永不宣布完成时，必须停下来。

        空转检测挡不住这种情形——它只看「有没有工具调用」。没有这个预算，
        Goal 会无限跑下去（而且一直在花钱）。
        """
        plan = PlanManager("s1")
        goal_manager = GoalManager(plan)
        calls: list[int] = []
        from corecode.runtime.goal_controller import GoalController

        def run_turn(request: TurnRequest) -> AgentTurnResult:
            calls.append(1)
            # 每轮都有工具调用 → 空转检测永远不触发。
            return _result("final", tool_calls=1)

        controller = GoalController(
            goal_manager,
            turns=TurnRunner(),
            run_turn=run_turn,
            record_answer=lambda payload: None,
            notice=lambda text: None,
            settle_workers=lambda: None,
        )
        goal_manager.create("做点事")
        controller._start({"role": "user", "content": "/goal 做点事"})
        controller.when_idle(timeout=10.0)

        snapshot = goal_manager.get_snapshot()
        self.assertEqual(snapshot.status, "paused", "必须被回合预算暂停")
        self.assertIn(str(MAX_AUTOMATIC_TURNS), snapshot.stop_reason or "")
        self.assertLessEqual(
            len(calls), MAX_AUTOMATIC_TURNS + 1, "不应超过预算继续跑"
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
