"""子 Agent 的行为测试。

重点在四条保证：
  1. **工具隔离** —— worker 注册表里根本不存在写工具；
  2. **并发上限** —— 第 4 个 spawn 必须被拒绝；
  3. **生命周期** —— running → completed / failed / closed；
  4. **关闭优先** —— 被 close 的 worker 不能被收尾逻辑改回 completed。
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
from corecode.agents.manager import SubAgentError, SubAgentManager
from corecode.agents.types import (
    MAX_SUB_AGENTS,
    PENDING_SUBAGENT_TOOLS,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SUBAGENT_TOOL_NAMES,
)
from corecode.agents.worker_prompt import build_subagent_prompt
from corecode.tooling import ToolContext, ToolRegistry
from corecode.tools import create_root_registry, create_worker_registry
from corecode.types import AgentStep

CWD = os.path.dirname(_HERE)


class BlockingAdapter:
    """一直阻塞，直到被取消。用来稳定地观察「运行中」状态。"""

    def __init__(self) -> None:
        self.started = threading.Semaphore(0)
        self.calls = 0

    def next(self, messages, *, tools=None, signal=None, on_delta=None):  # noqa: ANN001
        self.calls += 1
        self.started.release()
        if signal is not None:
            signal.wait(10.0)
        return AgentStep(type="assistant", content="", kind="final")


class ReportingAdapter:
    """固定回一份报告，用于验证 completed 路径。"""

    def __init__(self, content: str = "调研结论：A 文件第 12 行有问题。") -> None:
        self._content = content

    def next(self, messages, *, tools=None, signal=None, on_delta=None):  # noqa: ANN001
        return AgentStep(type="assistant", content=self._content, kind="final")


class FailingAdapter:
    """每次调用都抛异常，用于验证 failed 路径。"""

    def next(self, messages, *, tools=None, signal=None, on_delta=None):  # noqa: ANN001
        raise RuntimeError("模型调用失败")


class SubAgentManagerTests(unittest.TestCase):
    def _manager(self, model, registry: ToolRegistry | None = None) -> SubAgentManager:
        return SubAgentManager(
            model=model,
            tools=registry or create_worker_registry(create_root_registry()),
            cwd=CWD,
        )

    def _wait_for_start(self, adapter: BlockingAdapter, count: int = 1) -> None:
        for _ in range(count):
            self.assertTrue(
                adapter.started.acquire(timeout=5.0), "worker 未在预期时间内启动"
            )

    # -- 工具隔离 -----------------------------------------------------------

    def test_worker_registry_contains_only_read_only_tools(self) -> None:
        """隔离发生在注册表层：worker 看不到写工具，因此不可能越权。"""
        root = create_root_registry()
        worker = create_worker_registry(root)

        names = {t.name for t in worker.list()}
        self.assertEqual(names & set(SUBAGENT_TOOL_NAMES), names, "不该有白名单外的工具")
        for forbidden in ("write_file", "edit_file", "patch_file", "run_command"):
            self.assertIsNone(worker.find(forbidden), f"{forbidden} 不应可见")

    def test_every_whitelisted_tool_actually_exists(self) -> None:
        """白名单里的工具必须真的注册在根表里。

        否则 worker 拿到的是「Unknown tool」——它想调研却无工具可用，
        而这一点从「白名单看起来很正常」是看不出来的。
        """
        root = create_root_registry()
        available = {t.name for t in root.list()}
        missing = sorted(set(SUBAGENT_TOOL_NAMES) - available)
        self.assertEqual(missing, [], f"白名单里有未实现的工具：{missing}")

    def test_worker_has_real_research_capability(self) -> None:
        """worker 必须真的有调研能力，而不只是「没有写工具」。

        一个只有 list_files 的 worker 什么也查不出来——限制过严等于没有能力。
        """
        worker = create_worker_registry(create_root_registry())
        names = {t.name for t in worker.list()}
        self.assertIn("grep_files", names, "调研必须有搜索能力")
        self.assertIn("read_file", names, "调研必须有读取能力")

    def test_pending_tools_are_tracked_not_forgotten(self) -> None:
        """未实现的工具要显式记录，不能靠注释悄悄消失。"""
        available = {t.name for t in create_root_registry().list()}
        for name in PENDING_SUBAGENT_TOOLS:
            self.assertNotIn(
                name, available, f"{name} 已实现，应当加入 SUBAGENT_TOOL_NAMES"
            )

    def test_worker_registry_has_no_agent_tools(self) -> None:
        """worker 不能创建更多 worker —— 结构性保证，不是提示词约束。"""
        manager = self._manager(ReportingAdapter())
        worker = create_worker_registry(create_root_registry(manager))

        for forbidden in (
            "spawn_agent",
            "spawn_team",
            "list_agents",
            "wait_agent",
            "close_agent",
            "inspect_agent_changes",
            "apply_agent_changes",
        ):
            self.assertIsNone(worker.find(forbidden), f"{forbidden} 不应可见")

    def test_root_registry_includes_agent_tools_only_when_given(self) -> None:
        without = {t.name for t in create_root_registry().list()}
        self.assertNotIn("spawn_agent", without)

        manager = self._manager(ReportingAdapter())
        with_agents = {t.name for t in create_root_registry(manager).list()}
        self.assertIn("spawn_agent", with_agents)
        self.assertIn("spawn_team", with_agents)
        self.assertIn("wait_agent", with_agents)
        self.assertIn("close_agent", with_agents)
        self.assertIn("inspect_agent_changes", with_agents)
        self.assertIn("apply_agent_changes", with_agents)

    def test_coder_registry_has_write_tools_but_no_agent_or_user_tools(self) -> None:
        manager = self._manager(ReportingAdapter())
        root = create_root_registry(manager)
        from corecode.tools import create_coder_registry

        coder = create_coder_registry(root)
        names = {tool.name for tool in coder.list()}
        self.assertIn("write_file", names)
        self.assertIn("edit_file", names)
        self.assertIn("run_command", names)
        for forbidden in (
            "ask_user",
            "spawn_agent",
            "spawn_team",
            "apply_agent_changes",
        ):
            self.assertIsNone(coder.find(forbidden), f"{forbidden} 不应可见")

    # -- spawn --------------------------------------------------------------

    def test_empty_task_is_rejected(self) -> None:
        manager = self._manager(ReportingAdapter())
        with self.assertRaises(SubAgentError):
            manager.spawn("   ")

    def test_ids_are_sequential(self) -> None:
        manager = self._manager(ReportingAdapter())
        first = manager.spawn("任务一")
        second = manager.spawn("任务二")
        self.assertEqual(first.id, "agent_1")
        self.assertEqual(second.id, "agent_2")
        manager.close_all()

    def test_concurrency_limit_is_enforced(self) -> None:
        adapter = BlockingAdapter()
        manager = self._manager(adapter)

        for index in range(MAX_SUB_AGENTS):
            manager.spawn(f"任务 {index}")
        self._wait_for_start(adapter, MAX_SUB_AGENTS)
        self.assertEqual(manager.running_count, MAX_SUB_AGENTS)

        with self.assertRaises(SubAgentError) as ctx:
            manager.spawn("第四个")
        self.assertIn(str(MAX_SUB_AGENTS), str(ctx.exception))

        manager.close_all()

    def test_spawn_returns_immediately_without_waiting(self) -> None:
        """spawn 必须是「立即返回句柄」，否则根 Agent 无法并行干活。"""
        adapter = BlockingAdapter()
        manager = self._manager(adapter)

        started = time.time()
        snapshot = manager.spawn("慢任务")
        elapsed = time.time() - started

        self.assertEqual(snapshot.status, STATUS_RUNNING)
        self.assertLess(elapsed, 1.0, "spawn 不应阻塞等待 worker 结束")
        manager.close_all()

    # -- 生命周期 -----------------------------------------------------------

    def test_completed_records_the_report(self) -> None:
        manager = self._manager(ReportingAdapter("结论在此"))
        agent_id = manager.spawn("调研").id

        result = manager.wait([agent_id], timeout_ms=5_000)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.agents[0].status, STATUS_COMPLETED)
        self.assertEqual(result.agents[0].result, "结论在此")
        self.assertIsNotNone(result.agents[0].finished_at)

    def test_failure_is_recorded_not_raised(self) -> None:
        """`run_agent_turn` 把异常转成 outcome 而不是抛出。

        不检查 outcome 的话，失败会被当成成功——那是假的报告。
        """
        manager = self._manager(FailingAdapter())
        agent_id = manager.spawn("会失败的任务").id

        result = manager.wait([agent_id], timeout_ms=5_000)
        self.assertEqual(result.agents[0].status, STATUS_FAILED)
        self.assertIn("模型调用失败", result.agents[0].error or "")

    def test_wait_times_out_without_closing(self) -> None:
        """超时只返回最新状态，**不关闭** —— 让根 Agent 决定后续。"""
        adapter = BlockingAdapter()
        manager = self._manager(adapter)
        agent_id = manager.spawn("慢任务").id
        self._wait_for_start(adapter)

        result = manager.wait([agent_id], timeout_ms=100)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.agents[0].status, STATUS_RUNNING)
        self.assertEqual(manager.running_count, 1, "超时不应关闭 worker")

        manager.close_all()

    def test_close_transitions_to_closed(self) -> None:
        adapter = BlockingAdapter()
        manager = self._manager(adapter)
        agent_id = manager.spawn("慢任务").id
        self._wait_for_start(adapter)

        snapshot = manager.close(agent_id)
        self.assertEqual(snapshot.status, STATUS_CLOSED)
        self.assertEqual(manager.running_count, 0)

    def test_closed_worker_is_not_overwritten_by_completion(self) -> None:
        """关闭必须优先：被中断的 worker 不能被收尾逻辑改回 completed。

        少了这条，close_agent 的报告会显示「已完成」——而它其实是被中断的。
        """
        adapter = BlockingAdapter()
        manager = self._manager(adapter)
        agent_id = manager.spawn("慢任务").id
        self._wait_for_start(adapter)

        manager.close(agent_id)
        # 给收尾逻辑足够时间尝试改写状态
        time.sleep(0.3)

        snapshot = manager.get(agent_id)
        self.assertEqual(snapshot.status, STATUS_CLOSED)
        self.assertIsNone(snapshot.result)

    def test_close_unknown_agent_raises(self) -> None:
        manager = self._manager(ReportingAdapter())
        with self.assertRaises(SubAgentError):
            manager.close("agent_999")

    def test_wait_unknown_agent_raises(self) -> None:
        manager = self._manager(ReportingAdapter())
        with self.assertRaises(SubAgentError):
            manager.wait(["agent_999"])

    def test_close_all_stops_everything(self) -> None:
        adapter = BlockingAdapter()
        manager = self._manager(adapter)
        for index in range(MAX_SUB_AGENTS):
            manager.spawn(f"任务 {index}")
        self._wait_for_start(adapter, MAX_SUB_AGENTS)

        manager.close_all()
        self.assertEqual(manager.running_count, 0)
        self.assertTrue(
            all(a.status == STATUS_CLOSED for a in manager.list()),
            "close_all 后所有 worker 都应是 closed",
        )

    def test_list_reports_all_agents(self) -> None:
        manager = self._manager(ReportingAdapter())
        agent_id = manager.spawn("唯一的任务").id
        manager.wait([agent_id], timeout_ms=5_000)

        snapshots = manager.list()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].task, "唯一的任务")

    # -- 父信号传播 ---------------------------------------------------------

    def test_parent_cancellation_closes_the_worker(self) -> None:
        adapter = BlockingAdapter()
        manager = self._manager(adapter)
        parent = AbortSignal()

        agent_id = manager.spawn("会被父信号取消的任务", parent).id
        self._wait_for_start(adapter)

        parent.abort("根回合被取消")
        for _ in range(60):
            if manager.get(agent_id).status == STATUS_CLOSED:
                break
            time.sleep(0.05)

        self.assertEqual(
            manager.get(agent_id).status, STATUS_CLOSED, "父信号应当关掉 worker"
        )
        manager.close_all()

    def test_spawn_with_aborted_parent_is_rejected(self) -> None:
        manager = self._manager(ReportingAdapter())
        parent = AbortSignal()
        parent.abort("已经取消了")
        with self.assertRaises(Exception):
            manager.spawn("不该启动", parent)

    # -- 订阅 ---------------------------------------------------------------

    def test_subscribe_fires_on_state_change(self) -> None:
        seen: list[int] = []

        def on_change() -> None:
            seen.append(1)

        manager = self._manager(ReportingAdapter())
        manager.subscribe(on_change)
        agent_id = manager.spawn("任务").id
        manager.wait([agent_id], timeout_ms=5_000)
        self.assertGreaterEqual(len(seen), 2, "spawn 与结束都应通知")

    def test_unsubscribe_stops_notifications(self) -> None:
        seen: list[int] = []

        def on_change() -> None:
            seen.append(1)

        manager = self._manager(ReportingAdapter())
        unsubscribe = manager.subscribe(on_change)
        unsubscribe()
        agent_id = manager.spawn("任务").id
        manager.wait([agent_id], timeout_ms=5_000)
        self.assertEqual(seen, [])


class WorkerPromptTests(unittest.TestCase):
    def test_states_the_read_only_boundary(self) -> None:
        text = build_subagent_prompt("/tmp/work")
        self.assertIn("/tmp/work", text)
        self.assertIn("read-only sub-agent", text)
        # 必须明说不能改代码，否则模型会倾向于声称自己改了
        self.assertIn("cannot modify files", text)
        self.assertIn("Do not claim to have changed code", text)

    def test_memory_text_is_appended_when_present(self) -> None:
        text = build_subagent_prompt("/tmp/work", "项目约定：用 pytest")
        self.assertIn("项目约定：用 pytest", text)

    def test_memory_text_is_optional(self) -> None:
        self.assertNotIn("项目约定", build_subagent_prompt("/tmp/work"))


class SubAgentToolTests(unittest.TestCase):
    """四个管理工具本身的输入校验与输出形状。"""

    def setUp(self) -> None:
        from corecode.tools.subagents import create_subagent_tools

        self.manager = SubAgentManager(
            model=ReportingAdapter("报告内容"),
            tools=create_worker_registry(create_root_registry()),
            cwd=CWD,
        )
        self.tools = {t.name: t for t in create_subagent_tools(self.manager)}

    def tearDown(self) -> None:
        self.manager.close_all()

    def _context(self) -> ToolContext:
        return ToolContext(cwd=CWD)

    def test_spawn_tool_returns_a_handle(self) -> None:
        result = self.tools["spawn_agent"].run({"task": "调研 X"}, self._context())
        self.assertTrue(result.ok)
        self.assertIn("agent_1", result.output)
        self.assertIn("max_concurrent", result.output)

    def test_spawn_tool_reports_capacity_errors(self) -> None:
        from corecode.tools.subagents import create_subagent_tools

        blocking = SubAgentManager(
            model=BlockingAdapter(),
            tools=create_worker_registry(create_root_registry()),
            cwd=CWD,
        )
        tools = {t.name: t for t in create_subagent_tools(blocking)}
        for _ in range(MAX_SUB_AGENTS):
            tools["spawn_agent"].run({"task": "x"}, self._context())
        result = tools["spawn_agent"].run({"task": "y"}, self._context())
        self.assertFalse(result.ok)
        self.assertIn(str(MAX_SUB_AGENTS), result.output)
        blocking.close_all()

    def test_wait_tool_requires_nonempty_ids(self) -> None:
        for bad in ({}, {"agent_ids": []}, {"agent_ids": "not-a-list"}):
            with self.subTest(payload=bad):
                result = self.tools["wait_agent"].run(bad, self._context())
                self.assertFalse(result.ok)

    def test_wait_tool_rejects_negative_timeout(self) -> None:
        self.tools["spawn_agent"].run({"task": "x"}, self._context())
        result = self.tools["wait_agent"].run(
            {"agent_ids": ["agent_1"], "timeout_ms": -5}, self._context()
        )
        self.assertFalse(result.ok)

    def test_list_tool_reports_state(self) -> None:
        self.tools["spawn_agent"].run({"task": "x"}, self._context())
        result = self.tools["list_agents"].run({}, self._context())
        self.assertTrue(result.ok)
        self.assertIn("agents", result.output)

    def test_close_tool_on_unknown_agent_fails_cleanly(self) -> None:
        result = self.tools["close_agent"].run({"agent_id": "nope"}, self._context())
        self.assertFalse(result.ok)
        self.assertIn("未知", result.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
