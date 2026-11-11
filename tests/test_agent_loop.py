"""agent loop 的行为测试。

这些测试验证的不是「跑得通」，而是**语义正确**：
空响应会重试、进度消息不终结回合、thinking 截断能续跑、工具隔离生效、
致命错误按配置终止回合、取消能立刻生效。

运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from corecode.abort import AbortSignal
from corecode.adapters.mock import MockModelAdapter
from corecode.agent_loop import TurnHooks, run_agent_turn
from corecode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from corecode.tools import create_default_registry, subagent_registry
from corecode.types import AgentStep, StepDiagnostics, ToolCall

#: 测试用的临时工作区。刻意放在仓库内而不是系统临时目录：
#: 一来沙箱允许写这里，二来测试产生的文件可见、可清理。
_TMP_ROOT = os.path.join(_HERE, "_tmp")


class ScriptedAdapter:
    """按脚本逐个返回响应，并记录每次收到的消息。"""

    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = list(steps)
        self.seen_calls: list[list[dict]] = []
        #: 每次调用收到的 on_delta 回调（用于验证流式接线）。
        self.seen_deltas: list[object] = []

    def next(self, messages, *, tools=None, signal=None, on_delta=None):  # noqa: ANN001
        self.seen_calls.append(list(messages))
        self.seen_deltas.append(on_delta)
        if not self._steps:
            return AgentStep(type="assistant", content="（脚本耗尽）", kind="final")
        step = self._steps.pop(0)
        # 模拟流式：有正文且宿主提供了回调时逐段回调。
        if on_delta is not None and step.type == "assistant" and step.content:
            for index in range(0, len(step.content), 4):
                on_delta(step.content[index : index + 4])
        return step


def _explode_tool() -> ToolDefinition:
    def runner(data, context):  # noqa: ANN001
        raise RuntimeError("boom")

    return ToolDefinition(
        name="explode",
        description="总是抛异常，用于验证致命错误路径。",
        input_schema={"type": "object", "properties": {}},
        validator=lambda data: {},
        run=runner,
    )


def _echo_tool() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="回显输入。",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        validator=lambda data: {"text": str((data or {}).get("text", ""))},
        run=lambda data, context: ToolResult.success(f"echo:{data['text']}"),
    )


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(_TMP_ROOT, exist_ok=True)
        self.workdir = os.path.join(_TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.workdir, ignore_errors=True)
        os.makedirs(self.workdir, exist_ok=True)

    # -- 正常路径 -----------------------------------------------------------

    def test_full_model_tool_model_cycle(self) -> None:
        """完整走通 model -> tool -> model，且 tool_result 正确回填。"""
        registry = create_default_registry()
        with open(os.path.join(self.workdir, "hello.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi")

        result = run_agent_turn(
            model=MockModelAdapter(),
            tools=registry,
            messages=[
                {"role": "system", "content": "You are corecode."},
                {"role": "user", "content": "/ls"},
            ],
            cwd=self.workdir,
        )

        roles = [m["role"] for m in result.messages]
        self.assertIn("assistant_tool_call", roles)
        self.assertIn("tool_result", roles)
        self.assertEqual(result.outcome, "final")
        self.assertEqual(result.tool_calls, 1)

        tool_results = [m for m in result.messages if m["role"] == "tool_result"]
        self.assertFalse(tool_results[0]["isError"])
        self.assertIn("hello.txt", tool_results[0]["content"])

    def test_tool_call_and_result_stay_paired(self) -> None:
        """每个 tool_use 都必须有配对的 tool_result，否则下一次请求会被 API 拒绝。"""
        registry = ToolRegistry([_echo_tool()])
        model = ScriptedAdapter(
            [
                AgentStep(
                    type="tool_calls",
                    calls=[
                        ToolCall("a", "echo", {"text": "1"}),
                        ToolCall("b", "echo", {"text": "2"}),
                    ],
                ),
                AgentStep(type="assistant", content="done", kind="final"),
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=registry,
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )

        call_ids = {
            m["toolUseId"] for m in result.messages if m["role"] == "assistant_tool_call"
        }
        result_ids = {
            m["toolUseId"] for m in result.messages if m["role"] == "tool_result"
        }
        self.assertEqual(call_ids, {"a", "b"})
        self.assertEqual(result_ids, {"a", "b"})
        self.assertEqual(result.tool_calls, 2)

    def test_progress_message_does_not_end_turn(self) -> None:
        """<progress> 只是中途汇报，循环必须继续推到最终答复。"""
        model = ScriptedAdapter(
            [
                AgentStep(type="assistant", content="分析中…", kind="progress"),
                AgentStep(type="assistant", content="完成", kind="final"),
            ]
        )
        seen_progress: list[str] = []
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
            hooks=TurnHooks(on_progress_message=seen_progress.append),
        )

        self.assertEqual(result.outcome, "final")
        self.assertEqual(seen_progress, ["分析中…"])
        self.assertEqual(result.messages[-1]["content"], "完成")

    # -- 健壮性 -------------------------------------------------------------

    def test_empty_response_is_retried(self) -> None:
        """空响应先重试，重试后成功则回合成功。"""
        model = ScriptedAdapter(
            [
                AgentStep(type="assistant", content="", kind="final"),
                AgentStep(type="assistant", content="第二次成功", kind="final"),
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "final")
        self.assertEqual(result.messages[-1]["content"], "第二次成功")
        # 续跑提示必须被注入，否则模型不知道为什么被再次调用。
        self.assertTrue(
            any(m.get("internal") == "continuation" for m in result.messages)
        )

    def test_empty_response_exhausts_retries_then_fails(self) -> None:
        model = ScriptedAdapter(
            [AgentStep(type="assistant", content="", kind="final") for _ in range(5)]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "failed")
        self.assertIn("空响应", result.messages[-1]["content"])

    def test_recoverable_thinking_stop_resumes(self) -> None:
        """pause_turn + thinking 块导致的空响应应当续跑，而不是判失败。"""
        model = ScriptedAdapter(
            [
                AgentStep(
                    type="assistant",
                    content="",
                    diagnostics=StepDiagnostics(
                        stop_reason="pause_turn", block_types=["thinking"]
                    ),
                ),
                AgentStep(type="assistant", content="续跑成功", kind="final"),
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "final")
        self.assertEqual(result.messages[-1]["content"], "续跑成功")

    def test_empty_response_without_thinking_fails_fast(self) -> None:
        """没有 thinking 的 pause_turn 不该被当成可恢复，避免无限续跑。"""
        model = ScriptedAdapter(
            [
                AgentStep(
                    type="assistant",
                    content="",
                    diagnostics=StepDiagnostics(stop_reason="end_turn"),
                ),
                AgentStep(type="assistant", content="", kind="final"),
                AgentStep(type="assistant", content="", kind="final"),
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "failed")

    def test_max_steps_stops_the_loop(self) -> None:
        """模型无限要求调工具时，max_steps 必须兜住。"""
        model = ScriptedAdapter(
            [
                AgentStep(
                    type="tool_calls",
                    calls=[ToolCall(f"s{i}", "echo", {"text": str(i)})],
                )
                for i in range(20)
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([_echo_tool()]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
            max_steps=3,
        )
        self.assertEqual(result.outcome, "max_steps")
        self.assertEqual(result.tool_calls, 3)

    # -- 控制位 -------------------------------------------------------------

    def test_await_user_control_bit_suspends_turn(self) -> None:
        registry = create_default_registry()
        result = run_agent_turn(
            model=MockModelAdapter(),
            tools=registry,
            messages=[{"role": "user", "content": "/ask 你要改哪个文件？"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "awaiting_user")
        self.assertEqual(result.messages[-1]["content"], "你要改哪个文件？")

    def test_fatal_tool_error_only_stops_when_configured(self) -> None:
        registry = ToolRegistry([_explode_tool()])
        model_steps = lambda: [  # noqa: E731
            AgentStep(
                type="tool_calls", calls=[ToolCall("x", "explode", {})]
            ),
            AgentStep(type="assistant", content="我看到了错误", kind="final"),
        ]

        # 默认：致命错误回传给模型继续处理。
        lenient = run_agent_turn(
            model=ScriptedAdapter(model_steps()),
            tools=registry,
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(lenient.outcome, "final")
        self.assertTrue(
            any(m.get("isError") for m in lenient.messages if m["role"] == "tool_result")
        )

        # 开启 stop_on_fatal_tool_error：立即终止回合。
        strict = run_agent_turn(
            model=ScriptedAdapter(model_steps()),
            tools=registry,
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
            stop_on_fatal_tool_error=True,
        )
        self.assertEqual(strict.outcome, "failed")

    def test_unknown_tool_is_reported_not_raised(self) -> None:
        model = ScriptedAdapter(
            [
                AgentStep(type="tool_calls", calls=[ToolCall("x", "nope", {})]),
                AgentStep(type="assistant", content="ok", kind="final"),
            ]
        )
        result = run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "final")
        first = next(m for m in result.messages if m["role"] == "tool_result")
        self.assertTrue(first["isError"])
        self.assertIn("Unknown tool", first["content"])

    # -- 取消 ---------------------------------------------------------------

    def test_abort_signal_stops_the_turn(self) -> None:
        signal = AbortSignal()
        signal.abort("用户按了 Ctrl-C")
        result = run_agent_turn(
            model=MockModelAdapter(),
            tools=create_default_registry(),
            messages=[{"role": "user", "content": "/ls"}],
            cwd=self.workdir,
            signal=signal,
        )
        self.assertEqual(result.outcome, "aborted")
        self.assertIn("Ctrl-C", result.error or "")

    # -- 工具隔离 -----------------------------------------------------------

    def test_subagent_registry_is_read_only(self) -> None:
        """子 Agent 不能看到任何写工具——隔离发生在注册表层。"""
        from corecode.agents.types import SUBAGENT_TOOL_NAMES

        full = create_default_registry()
        restricted = subagent_registry(full)

        names = {entry.name for entry in restricted.list()}
        # 白名单是权威来源；这里断言「裁剪结果恰好是白名单 ∩ 根表」，
        # 而不是硬编码名单——否则以后加只读工具就会让这条测试假失败。
        self.assertEqual(names, set(SUBAGENT_TOOL_NAMES) & {e.name for e in full.list()})
        self.assertIsNone(restricted.find("write_file"))
        self.assertIsNone(restricted.find("ask_user"))
        # 原注册表不受影响（subset 返回新对象）。
        self.assertIsNotNone(full.find("write_file"))

    # -- 写前审查 -----------------------------------------------------------

    def test_write_file_creates_and_reports_change(self) -> None:
        result = run_agent_turn(
            model=MockModelAdapter(),
            tools=create_default_registry(),
            messages=[{"role": "user", "content": "/write notes.txt::你好"}],
            cwd=self.workdir,
        )
        self.assertEqual(result.outcome, "final")
        with open(os.path.join(self.workdir, "notes.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "你好")

    def test_write_file_skips_identical_content(self) -> None:
        """内容没变时必须走「无需修改」分支，不重复落盘。"""
        target = os.path.join(self.workdir, "same.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("不变")

        registry = create_default_registry()
        executed: list[str] = []
        result = registry.execute(
            "write_file",
            {"path": "same.txt", "content": "不变"},
            ToolContext(cwd=self.workdir),
        )
        executed.append(result.output)
        self.assertTrue(result.ok)
        self.assertIn("无需修改", result.output)

    def test_path_escape_is_rejected_without_permissions(self) -> None:
        """没有权限管理器时，越界路径必须直接拒绝。"""
        registry = create_default_registry()
        result = registry.execute(
            "read_file",
            {"path": "../../etc/passwd"},
            ToolContext(cwd=self.workdir),
        )
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
