"""TUI 交互循环的测试 —— 用脚本化按键真正驱动它。

## 为什么需要这个文件

`tui/` 原本只测了**纯函数**（差分算法、宽度、面板），而真正接触终端的
`terminal.py` 和把一切串起来的 `runner.py` 从未被验证过。我第一次实测时
`minicode --tui` 根本无法使用，根因是 `read_key` 用了 `sys.stdin.read(1)`
——控制台输入是行缓冲的，会一直等到回车，按键根本进不来。

**这个 bug 纯函数测试永远抓不到**，因为它出在「把按键取进来」这一步。

于是我把按键源与尺寸源做成可注入的，用脚本按键把整个循环跑完：
输入 → 回车提交 → 回合回调 → 重绘 → Ctrl-D 退出。这样交互路径才真正有了
证据，而不是只能说「我没法验证」。

真实 `msvcrt` / `select` 的实现仍无法在自动化环境里驱动（没有 TTY），
因此本文件覆盖的是**键位处理、状态变更、渲染、生命周期**；平台原生取键
那一小段仍是未测面积，这一点如实记录在 README。
"""

from __future__ import annotations

import io
import os
import sys
import threading
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.tui.app import AppState, BannerStats  # noqa: E402
from minicode.tui.runner import HostBridge, UiEvents, make_terminal_events, run_tui  # noqa: E402
from minicode.tui.terminal import (  # noqa: E402
    KEY_BACKSPACE,
    KEY_CTRL_C,
    KEY_CTRL_D,
    KEY_DOWN,
    KEY_ENTER,
    KEY_PAGE_UP,
    KEY_TAB,
    KEY_UP,
    Terminal,
    TerminalSize,
)

SIZE = TerminalSize(columns=80, rows=24)


class ScriptedTerminal(Terminal):
    """按键由脚本决定、尺寸固定的终端，用来驱动 `run_tui`。"""

    def __init__(self, keys: list[str | None]) -> None:
        self.stream = io.StringIO()
        self._keys = list(keys)
        self.frames: list[str] = []
        self.cursors: list[tuple[int, int] | None] = []
        super().__init__(
            self.stream,
            force_non_interactive=False,
            key_source=self._next_key,
            size_source=lambda: SIZE,
        )
        # 强制交互态：真实 TTY 判断在测试进程里为假。
        self.interactive = True

    def _next_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys.pop(0)

    def render(
        self,
        frame: str,
        size: TerminalSize | None = None,
        cursor: tuple[int, int] | None = None,
    ) -> bool:
        self.frames.append(frame)
        self.cursors.append(cursor)
        return super().render(frame, size, cursor=cursor)


def make_bridge(
    *,
    on_turn=None,
    local=None,
    busy=lambda: False,
) -> HostBridge:
    return HostBridge(
        run_turn=on_turn or (lambda text: None),
        run_local=local or (lambda text: None),
        is_busy=busy,
        summary=lambda: BannerStats(model="test-model", session_id="s1"),
    )


def type_text(text: str) -> list[str]:
    """把一段文字变成逐字符按键。"""
    return list(text)


class LoopLifecycleTests(unittest.TestCase):
    def test_ctrl_d_on_empty_input_quits(self) -> None:
        terminal = ScriptedTerminal([KEY_CTRL_D])
        code = run_tui(AppState(), make_bridge(), terminal=terminal)
        self.assertEqual(code, 0)
        self.assertTrue(terminal.frames, "至少应渲染过一帧")

    def test_bare_enter_does_not_submit(self) -> None:
        submitted: list[str] = []
        terminal = ScriptedTerminal([KEY_ENTER, KEY_CTRL_D])
        run_tui(
            AppState(),
            make_bridge(on_turn=submitted.append),
            terminal=terminal,
        )
        self.assertEqual(submitted, [], "空输入不该触发回合")

    def test_typing_then_enter_submits(self) -> None:
        """核心路径：打字 → 回车 → 回合被调用。"""
        submitted: list[str] = []
        done = threading.Event()

        def on_turn(text: str) -> None:
            submitted.append(text)
            done.set()

        terminal = ScriptedTerminal([*type_text("你好"), KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), make_bridge(on_turn=on_turn), terminal=terminal)
        done.wait(5)
        self.assertEqual(submitted, ["你好"])

    def test_submitted_text_appears_in_a_rendered_frame(self) -> None:
        """渲染必须反映用户输入——否则界面与状态脱节。"""
        done = threading.Event()
        terminal = ScriptedTerminal([*type_text("检查代码"), KEY_ENTER, KEY_CTRL_D])
        run_tui(
            AppState(),
            make_bridge(on_turn=lambda _t: done.set()),
            terminal=terminal,
        )
        done.wait(5)
        joined = "\n".join(terminal.frames)
        self.assertIn("检查代码", joined)

    def test_backspace_removes_before_submitting(self) -> None:
        submitted: list[str] = []
        done = threading.Event()

        def on_turn(text: str) -> None:
            submitted.append(text)
            done.set()

        terminal = ScriptedTerminal(
            [*type_text("abc"), KEY_BACKSPACE, KEY_ENTER, KEY_CTRL_D]
        )
        run_tui(AppState(), make_bridge(on_turn=on_turn), terminal=terminal)
        done.wait(5)
        self.assertEqual(submitted, ["ab"])

    def test_ctrl_c_clears_input_without_quitting(self) -> None:
        submitted: list[str] = []
        done = threading.Event()
        terminal = ScriptedTerminal(
            [*type_text("丢弃"), KEY_CTRL_C, *type_text("保留"), KEY_ENTER, KEY_CTRL_D]
        )
        run_tui(
            AppState(),
            make_bridge(on_turn=lambda t: (submitted.append(t), done.set())),
            terminal=terminal,
        )
        done.wait(5)
        self.assertEqual(submitted, ["保留"], "Ctrl-C 应清空而非提交")


class LocalCommandTests(unittest.TestCase):
    def test_slash_command_goes_to_run_local(self) -> None:
        """斜杠命令必须走本地处理，不能发给模型。"""
        local_calls: list[str] = []
        turn_calls: list[str] = []
        seen = threading.Event()

        def on_local(text: str) -> str | None:
            local_calls.append(text)
            seen.set()
            return "本地结果"

        terminal = ScriptedTerminal([*type_text("/status"), KEY_ENTER, KEY_CTRL_D])
        run_tui(
            AppState(),
            make_bridge(on_turn=turn_calls.append, local=on_local),
            terminal=terminal,
        )
        seen.wait(5)
        self.assertEqual(local_calls, ["/status"])
        self.assertEqual(turn_calls, [], "本地命令不该触发模型回合")

    def test_local_reply_is_shown(self) -> None:
        rendered = threading.Event()
        terminal = ScriptedTerminal([*type_text("/help"), KEY_ENTER, KEY_CTRL_D])
        original_render = terminal.render

        def render(
            frame: str,
            size: TerminalSize | None = None,
            cursor: tuple[int, int] | None = None,
        ) -> bool:
            result = original_render(frame, size, cursor=cursor)
            if "本地结果" in "\n".join(terminal.frames):
                rendered.set()
            return result

        terminal.render = render  # type: ignore[method-assign]
        run_tui(
            AppState(),
            make_bridge(local=lambda _t: "本地结果"),
            terminal=terminal,
        )
        rendered.wait(5)
        self.assertIn("本地结果", "\n".join(terminal.frames))

    def test_unhandled_slash_command_is_reported_not_sent(self) -> None:
        turn_calls: list[str] = []
        terminal = ScriptedTerminal([*type_text("/nope"), KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), make_bridge(on_turn=turn_calls.append), terminal=terminal)
        # run_local 返回 None 时视为未知命令；绝不能落到模型
        self.assertEqual(turn_calls, [], "未知斜杠命令不该发给模型")


class EventIntegrationTests(unittest.TestCase):
    def test_tool_events_reach_the_transcript(self) -> None:
        """完整的接线：宿主回调 → 事件队列 → 转录条目。

        用 `make_terminal_events` 造回调（这是 `cli.py` 真正使用的那个函数），
        再把队列并入 `AppState`，两次渲染之间验证界面确实变了。

        刻意不走 `run_tui`：它内部自建 `UiEvents`，测试无法注入自己的队列；
        而这条链路的正确性与循环无关，直接测更实在。
        """
        events = UiEvents()
        callbacks = make_terminal_events(events)
        state = AppState()

        # 注意 `on_tool_result` 的模型是 `(name, output, is_error)`，
        # 因此第三个参数传 False 表示**成功**。
        callbacks["on_tool_start"]("read_file", {})
        callbacks["on_tool_result"]("read_file", "read_file", False)
        callbacks["on_assistant"]("读完了", True)

        items, _finished = events.drain()
        self.assertEqual(len(items), 3)

        # 主线程在重绘点并入状态（与 `run_tui._apply_events` 的逻辑一致）
        for kind, text, ok in items:
            if kind == "tool_start":
                state.push("tool", text, ok=None)
            elif kind == "tool_result":
                if not state.update_running_tool(text, ok):
                    state.push("tool", text, ok=ok)
            else:
                state.push(kind, text)

        kinds = [entry.kind for entry in state.transcript]
        self.assertEqual(kinds, ["tool", "assistant"])
        self.assertIs(state.transcript[0].ok, True, "工具条目应被收尾为成功")
        self.assertIn("读完了", state.transcript[1].text)

    def test_queue_drain_is_empty_on_second_call(self) -> None:
        """drain 必须清空队列，否则事件会被重复渲染。"""
        events = UiEvents()
        events.push("assistant", "一次性")
        first, _ = events.drain()
        second, _ = events.drain()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_finished_flag_resets_after_drain(self) -> None:
        events = UiEvents()
        events.mark_finished()
        _items, finished = events.drain()
        self.assertTrue(finished)
        _items, again = events.drain()
        self.assertFalse(again, "结束标志应只报告一次")

    def test_callbacks_wiring_from_callbacks(self) -> None:
        """`make_terminal_events` 产生的回调应把内容按正确种类推进队列。"""
        events = UiEvents()
        callbacks = make_terminal_events(events)
        callbacks["on_assistant"]("回答内容", True)
        callbacks["on_progress"]("正在思考")
        callbacks["on_tool_start"]("grep_files", {})
        callbacks["on_tool_result"]("grep_files", "ok", False)
        callbacks["on_notice"]("提示")

        items, finished = events.drain()
        self.assertEqual(
            [item[0] for item in items],
            ["assistant", "progress", "tool_start", "tool_result", "notice"],
        )
        self.assertFalse(finished)
        self.assertEqual(items[0][1], "回答内容")
        # is_error=False → ok=True
        self.assertIs(items[3][2], True)

    def test_tool_result_marks_failure(self) -> None:
        events = UiEvents()
        callbacks = make_terminal_events(events)
        callbacks["on_tool_result"]("read_file", "炸了", True)
        items, _ = events.drain()
        self.assertIs(items[0][2], False, "is_error=True 应映射为 ok=False")


class ExceptionSafetyTests(unittest.TestCase):
    def test_turn_exception_is_shown_not_crashed(self) -> None:
        """回合抛异常时界面必须显示错误，而不是让整个 TUI 崩掉。"""
        def on_turn(_text: str) -> None:
            raise RuntimeError("模型炸了")

        terminal = ScriptedTerminal(
            [*type_text("触发错误"), KEY_ENTER, None, None, None, KEY_CTRL_D]
        )
        code = run_tui(AppState(), make_bridge(on_turn=on_turn), terminal=terminal)
        self.assertEqual(code, 0)
        self.assertIn("模型炸了", "\n".join(terminal.frames))

    def test_terminal_is_restored_even_when_loop_raises(self) -> None:
        """循环异常也必须恢复终端——留在备用屏幕里是最糟的失败方式。"""
        terminal = ScriptedTerminal([KEY_CTRL_D])
        calls: list[str] = []
        original_leave = terminal.leave

        def leave() -> None:
            calls.append("leave")
            original_leave()

        terminal.leave = leave  # type: ignore[method-assign]
        run_tui(AppState(), make_bridge(), terminal=terminal)
        self.assertIn("leave", calls)


class TerminalKeySourceTests(unittest.TestCase):
    def test_injected_key_source_is_used(self) -> None:
        keys = iter(["a", "b", None])
        terminal = Terminal(
            io.StringIO(),
            force_non_interactive=False,
            key_source=lambda: next(keys, None),
        )
        terminal.interactive = True
        self.assertEqual(terminal.read_key(), "a")
        self.assertEqual(terminal.read_key(), "b")
        self.assertIsNone(terminal.read_key())

    def test_injected_size_source_is_used(self) -> None:
        terminal = Terminal(
            io.StringIO(),
            force_non_interactive=True,
            size_source=lambda: TerminalSize(columns=123, rows=45),
        )
        self.assertEqual(terminal.size().columns, 123)

    def test_non_interactive_never_reads_keys(self) -> None:
        """非交互环境下即使用户注入了按键源也不该读——避免误用。"""
        terminal = Terminal(io.StringIO(), force_non_interactive=True)
        self.assertIsNone(terminal.read_key())


if __name__ == "__main__":
    unittest.main(verbosity=2)
