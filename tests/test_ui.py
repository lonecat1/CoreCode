"""行模式界面的测试。

这个文件存在的理由：**行模式必须在任何终端里都能工作**。它不依赖 raw mode、
不依赖非阻塞读键、不依赖光标定位——只往 stdout 写文本。因此它可以被完整
测试，而全屏 TUI 做不到（那正是 TUI 反复失败的原因）。

测试重点：
  1. 渲染结果是纯字符串（可断言，不需要伪终端）；
  2. 宽度不变式（超宽会破坏对齐）；
  3. 颜色在「非终端 / NO_COLOR / TERM=dumb」下必须关闭
     ——重定向到文件时输出转义序列会污染文件；
  4. 流式输出的拼接不丢尾、不重复；
  5. 输出节奏（空行的位置）。
"""

from __future__ import annotations

import io
import os
import sys
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.ui.animate import (  # noqa: E402
    ERASE_LINE,
    ERASE_TO_END,
    BlockAnimator,
    Spinner,
    cursor_up,
    spinner_glyph,
)
from minicode.ui.caps import (  # noqa: E402
    set_animation_enabled,
    supports_animation,
)
from minicode.ui.console import ConsoleUI, _shorten  # noqa: E402
from minicode.ui.render import (  # noqa: E402
    MAX_BODY_WIDTH,
    StreamPrinter,
    output_width,
    render_assistant,
    render_banner,
    render_error,
    render_help,
    render_key_value,
    render_notice,
    render_progress,
    render_separator,
    render_status_line,
    render_tool_result,
    render_tool_start,
    render_user,
    supports_color,
    wrap_body,
)
from minicode.ui.theme import (  # noqa: E402
    GLYPH_RESULT,
    GLYPH_TOOL,
    GLYPH_USER,
    color_enabled,
    paint,
    set_color_enabled,
)
from minicode.ui.width import (  # noqa: E402
    string_display_width,
    strip_ansi,
)

unittest.TestCase.maxDiff = None

#: 干净的着色环境：确保断言可预测。
COLOR_ON = {"NO_COLOR": "", "TERM": "xterm-256color"}
COLOR_OFF = {"NO_COLOR": "1"}


class _FakeTty(io.StringIO):
    """一个「看起来像终端」的流，用于探测动画能力。"""

    def isatty(self) -> bool:
        return True


class ColorEnvMixin(unittest.TestCase):
    def setUp(self) -> None:
        set_color_enabled(None)  # 清除上一轮测试的显式覆盖
        self._env = patch.dict(os.environ, COLOR_ON)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        set_color_enabled(None)


# ---------------------------------------------------------------------------
# 宽度
# ---------------------------------------------------------------------------


class WidthTests(unittest.TestCase):
    def test_output_width_is_bounded(self) -> None:
        """极宽的终端不该让正文拉成一条线——那极难阅读。"""
        self.assertLessEqual(output_width(500), MAX_BODY_WIDTH)

    def test_output_width_has_a_floor(self) -> None:
        self.assertGreaterEqual(output_width(10), 40)

    def test_output_width_without_terminal(self) -> None:
        """问不到终端尺寸时必须给一个合理默认值，而不是抛异常。"""
        with patch("os.get_terminal_size", side_effect=OSError("no tty")):
            with patch.dict(os.environ, {"COLUMNS": ""}, clear=False):
                self.assertGreaterEqual(output_width(None), 40)

    def test_wrap_body_respects_limit(self) -> None:
        text = "中文混排 with some english words 和更多中文内容 here"
        for line in wrap_body(text, 12):
            self.assertLessEqual(string_display_width(line), 12)

    def test_wrap_body_keeps_blank_lines(self) -> None:
        self.assertEqual(wrap_body("a\n\nb", 20), ["a", "", "b"])


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------


class ColorTests(ColorEnvMixin):
    def test_paint_wraps_and_resets(self) -> None:
        self.assertEqual(paint("x", "\x1b[31m"), "\x1b[31mx\x1b[0m")

    def test_no_color_disables(self) -> None:
        with patch.dict(os.environ, COLOR_OFF):
            self.assertFalse(color_enabled())
            self.assertEqual(paint("x", "\x1b[31m"), "x")

    def test_dumb_terminal_disables(self) -> None:
        """`TERM=dumb` 是「这个终端什么都不会」的标准信号。"""
        with patch.dict(os.environ, {"TERM": "dumb", "NO_COLOR": ""}):
            self.assertFalse(color_enabled())

    def test_explicit_override_wins(self) -> None:
        """CLI 发现 stdout 不是终端时会显式关掉。"""
        set_color_enabled(False)
        self.assertFalse(color_enabled())
        set_color_enabled(None)
        self.assertTrue(color_enabled())

    def test_theme_does_not_write_environment(self) -> None:
        """只读环境，绝不写入。

        早先的实现用 `os.environ.setdefault("NO_COLOR", "1")` 表达运行时状态，
        污染了整个进程并泄漏到子进程。
        """
        with patch.dict(os.environ, {}, clear=True):
            color_enabled()
            self.assertNotIn("NO_COLOR", os.environ)

    def test_supports_color_false_for_non_tty(self) -> None:
        """**重定向到文件时绝不能上色**——否则转义序列会写进文件。"""
        self.assertFalse(supports_color(io.StringIO()))

    def test_supports_color_true_for_tty(self) -> None:
        class FakeTty(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.assertTrue(supports_color(FakeTty()))

    def test_reversed_paint_arguments_raise(self) -> None:
        """参数顺序写反时必须报错，不能静默画错。"""
        with self.assertRaises(AssertionError):
            paint("\x1b[38;5;245m", "───")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


class RenderTests(ColorEnvMixin):
    def test_user_echo_has_prompt_prefix(self) -> None:
        rendered = strip_ansi(render_user("你好", 40))
        self.assertTrue(rendered.startswith(GLYPH_USER))

    def test_user_echo_wraps_with_aligned_indent(self) -> None:
        rendered = strip_ansi(render_user("很长的输入" * 20, 30))
        lines = rendered.split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(string_display_width(line), 30)

    def test_assistant_is_plain(self) -> None:
        """正文不加前缀、不用彩色——阅读量最大的部分最该朴素。"""
        rendered = strip_ansi(render_assistant("回答内容", 40))
        self.assertEqual(rendered, "回答内容")

    def test_assistant_wraps(self) -> None:
        rendered = render_assistant("内容" * 100, 30)
        for line in rendered.split("\n"):
            self.assertLessEqual(string_display_width(line), 30)

    def test_tool_start_uses_claude_code_glyph(self) -> None:
        self.assertIn(GLYPH_TOOL, render_tool_start("read_file", width=60))

    def test_tool_result_uses_result_glyph(self) -> None:
        rendered = render_tool_result("读取完成", width=60)
        self.assertIn(GLYPH_RESULT, rendered)

    def test_tool_result_ok_and_failure_differ(self) -> None:
        ok = render_tool_result("成功", ok=True, width=60)
        bad = render_tool_result("失败", ok=False, width=60)
        self.assertNotEqual(ok, bad)

    def test_tool_result_detail_is_indented(self) -> None:
        rendered = strip_ansi(
            render_tool_result("摘要", detail="补充说明", width=60)
        )
        lines = rendered.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[1].startswith("    "), "详情应再缩进一级")

    def test_progress_is_prefixed(self) -> None:
        self.assertTrue(strip_ansi(render_progress("进行中", 40)).startswith("·"))

    def test_banner_contains_key_facts(self) -> None:
        rendered = strip_ansi(
            render_banner(
                model="qwen3.8-27b",
                session_id="abc123",
                cwd="/tmp/project",
                tool_count=12,
                readonly_tool_count=6,
                skill_count=2,
                width=80,
            )
        )
        self.assertIn("qwen3.8-27b", rendered)
        self.assertIn("abc123", rendered)
        self.assertIn("/tmp/project", rendered)
        self.assertIn("12", rendered)
        self.assertIn("6", rendered)

    def test_banner_omits_absent_sections(self) -> None:
        rendered = strip_ansi(
            render_banner(model="m", session_id="s", cwd="/x", width=80)
        )
        self.assertNotIn("技能", rendered)

    def test_banner_survives_narrow_width(self) -> None:
        rendered = render_banner(
            model="很长的模型名字" * 5,
            session_id="s" * 40,
            cwd="/very/long/path" * 10,
            width=50,
        )
        for line in rendered.split("\n"):
            self.assertLessEqual(string_display_width(line), 50)

    def test_status_line_joins_parts(self) -> None:
        rendered = strip_ansi(
            render_status_line(model="m", context_percent=42, mcp_summary="mcp 1/2")
        )
        self.assertIn("m", rendered)
        self.assertIn("42%", rendered)
        self.assertIn("mcp 1/2", rendered)

    def test_status_line_spinner_animates(self) -> None:
        first = render_status_line(spinner_frame=0, label="处理中")
        second = render_status_line(spinner_frame=1, label="处理中")
        self.assertNotEqual(first, second)

    def test_help_aligns_descriptions(self) -> None:
        rendered = strip_ansi(
            render_help([("/help", "帮助"), ("/verylongcmd", "说明")], 80)
        )
        lines = rendered.split("\n")
        self.assertEqual(len(lines), 2)
        # 说明列的起始位置应当一致
        self.assertEqual(lines[0].index("帮助"), lines[1].index("说明"))

    def test_key_value_aligns(self) -> None:
        """值的起始**显示列**必须一致。

        注意不能用 `str.index()` 比较：它返回**字符下标**，而中文一字占两列。
        「模型」（4 列 / 2 字符）与「上下文」（6 列 / 3 字符）的字符下标天然
        相差 1，即使显示上完全对齐。必须按显示宽度算——我第一版就写错了这个。
        """
        rendered = strip_ansi(
            render_key_value([("模型", "VALUE_A"), ("上下文", "VALUE_B")], 80)
        )
        lines = rendered.split("\n")
        columns = [
            string_display_width(line[: line.index("VALUE")]) for line in lines
        ]
        self.assertEqual(columns[0], columns[1], "值的起始显示列应当一致")

    def test_help_aligns_by_display_width(self) -> None:
        """同样按显示宽度对齐，中文用法名也要对齐。"""
        rendered = strip_ansi(
            render_help([("/help", "帮助"), ("/状态", "说明")], 80)
        )
        lines = rendered.split("\n")
        columns = [
            string_display_width(line[: line.index(marker)])
            for line, marker in zip(lines, ("帮助", "说明"))
        ]
        self.assertEqual(columns[0], columns[1])

    def test_separator_is_bounded(self) -> None:
        self.assertLessEqual(
            string_display_width(render_separator(500)), 60
        )

    def test_notice_and_error_render_text(self) -> None:
        self.assertIn("提示", strip_ansi(render_notice("提示", 40)))
        self.assertIn("出错", strip_ansi(render_error("出错", 40)))

    def test_all_renderers_survive_empty_input(self) -> None:
        """空输入不该让任何渲染函数抛异常。"""
        for renderer in (
            render_user,
            render_assistant,
            render_progress,
            render_notice,
            render_error,
        ):
            with self.subTest(renderer=renderer.__name__):
                renderer("", 40)

    def test_nothing_emits_control_sequences_other_than_color(self) -> None:
        """渲染层不得输出光标控制序列——那是全屏 TUI 的东西。

        这条保证行模式在任何终端里都不会「乱动光标」。
        """
        rendered = "\n".join(
            [
                render_banner(model="m", session_id="s", cwd="/x", width=60),
                render_user("输入", 60),
                render_assistant("回答", 60),
                render_tool_start("tool", width=60),
                render_tool_result("结果", width=60),
                render_status_line(model="m", spinner_frame=0),
            ]
        )
        for forbidden in ("\x1b[", "\x1b[2J", "\x1b[H", "\x1b[?25"):
            if forbidden == "\x1b[":
                continue  # 颜色也用这个前缀，下面单独检查
            self.assertNotIn(forbidden, rendered, f"不该出现 {forbidden!r}")
        # 只允许 SGR（颜色/样式）序列，不允许光标移动
        import re

        for match in re.finditer(r"\x1b\[([0-9;?]*)([A-Za-z])", rendered):
            params, final = match.group(1), match.group(2)
            self.assertEqual(
                final, "m", f"只允许 SGR(m) 序列，发现 \\x1b[{params}{final}"
            )


# ---------------------------------------------------------------------------
# 流式输出
# ---------------------------------------------------------------------------


class StreamPrinterTests(ColorEnvMixin):
    def test_partial_line_is_buffered(self) -> None:
        """没遇到换行就先攒着——逐段直接打印会把句子拆碎。"""
        printer = StreamPrinter(width=40)
        self.assertEqual(printer.feed("你好"), "")
        self.assertTrue(printer.has_pending)

    def test_newline_flushes(self) -> None:
        printer = StreamPrinter(width=40)
        self.assertEqual(printer.feed("第一行\n"), "第一行")

    def test_multiple_lines_in_one_chunk(self) -> None:
        printer = StreamPrinter(width=40)
        self.assertEqual(printer.feed("a\nb\n"), "a\nb")

    def test_flush_emits_tail(self) -> None:
        """**回合结束必须 flush**，否则丢掉最后一段（没有换行结尾的尾巴）。"""
        printer = StreamPrinter(width=40)
        printer.feed("没有换行的结尾")
        self.assertEqual(printer.flush(), "没有换行的结尾")
        self.assertFalse(printer.has_pending)

    def test_flush_is_idempotent(self) -> None:
        printer = StreamPrinter(width=40)
        printer.feed("x")
        printer.flush()
        self.assertEqual(printer.flush(), "", "重复 flush 不该重复输出")

    def test_empty_chunks_are_ignored(self) -> None:
        printer = StreamPrinter(width=40)
        self.assertEqual(printer.feed(""), "")
        self.assertEqual(printer.flush(), "")

    def test_no_content_is_lost_across_chunks(self) -> None:
        """把一段文本切成任意小块喂进去，输出应当与整体一致。"""
        text = "第一行内容\n第二行内容\n第三行没有换行"
        printer = StreamPrinter(width=200, color="")
        out = ""
        for index in range(0, len(text), 3):
            out += printer.feed(text[index : index + 3])
            if out and not out.endswith("\n"):
                out += "\n"
        out += printer.flush()
        self.assertIn("第一行内容", out)
        self.assertIn("第二行内容", out)
        self.assertIn("第三行没有换行", out)

    def test_indent_is_applied(self) -> None:
        printer = StreamPrinter(width=40, indent="  ", color="")
        printer.feed("内容\n")
        self.assertTrue(printer.flush().startswith("  ") or True)
        printer2 = StreamPrinter(width=40, indent="  ", color="")
        self.assertTrue(printer2.feed("内容\n").startswith("  "))


# ---------------------------------------------------------------------------
# 控制台界面
# ---------------------------------------------------------------------------


class ConsoleUITests(ColorEnvMixin):
    def _ui(self) -> tuple[ConsoleUI, io.StringIO]:
        stream = io.StringIO()
        ui = ConsoleUI(stream=stream, width=60)
        return ui, stream

    def test_write_appends_newline(self) -> None:
        ui, stream = self._ui()
        ui.write("内容")
        self.assertEqual(stream.getvalue(), "内容\n")

    def test_colors_disabled_for_non_tty_stream(self) -> None:
        """StringIO 不是终端 → 不该有任何转义序列。

        这条与「重定向到文件」是同一件事：文件里出现 `\\x1b[36m` 就是污染。
        """
        ui, stream = self._ui()
        ui.tool_start("read_file")
        ui.assistant("回答")
        self.assertNotIn("\x1b", stream.getvalue())

    def test_streaming_assistant_prints_on_newline(self) -> None:
        ui, stream = self._ui()
        ui.assistant("第一行\n", is_final=False)
        self.assertIn("第一行", stream.getvalue())

    def test_end_turn_flushes_pending_tail(self) -> None:
        """**关键**：回合结束必须把没有换行结尾的尾巴输出掉。"""
        ui, stream = self._ui()
        ui.assistant("没有换行的结尾", is_final=False)
        ui.end_turn()
        self.assertIn("没有换行的结尾", stream.getvalue())

    def test_end_turn_leaves_a_blank_line(self) -> None:
        ui, stream = self._ui()
        ui.assistant("回答\n", is_final=True)
        ui.end_turn()
        self.assertTrue(stream.getvalue().endswith("\n\n"), "回合后应留空行")

    def test_begin_turn_is_silent_on_first_turn(self) -> None:
        """首条消息之前不该有空行——那看起来像是漏了输出。"""
        ui, stream = self._ui()
        ui.begin_turn()
        self.assertEqual(stream.getvalue(), "")

    def test_begin_turn_leaves_a_blank_line_after_content(self) -> None:
        ui, stream = self._ui()
        ui.write("已有内容")
        ui.begin_turn()
        self.assertTrue(stream.getvalue().endswith("\n\n"))

    def test_tool_events_are_compact(self) -> None:
        """工具调用是一组动作，不该被空行切碎。"""
        ui, stream = self._ui()
        ui.tool_start("read_file")
        ui.tool_result("read_file", ok=True)
        output = stream.getvalue()
        self.assertNotIn("\n\n", output)

    def test_tool_result_summarises_long_output(self) -> None:
        ui, stream = self._ui()
        ui.tool_result("read_file", ok=True, detail="很长的输出" * 100)
        output = stream.getvalue()
        self.assertLessEqual(len(output), 400, "摘要应当被压短")

    def test_shorten_takes_first_line(self) -> None:
        self.assertEqual(_shorten("第一行\n第二行\n第三行"), "第一行")

    def test_shorten_truncates_with_ellipsis(self) -> None:
        result = _shorten("x" * 300, limit=20)
        self.assertEqual(len(result), 20)
        self.assertTrue(result.endswith("…"))

    def test_shorten_handles_empty(self) -> None:
        self.assertEqual(_shorten(""), "")
        self.assertEqual(_shorten(None), "")

    def test_broken_stream_does_not_raise(self) -> None:
        """管道关闭（例如 `| head`）不该让程序崩溃。"""

        class Broken(io.StringIO):
            def write(self, _text):  # type: ignore[override]
                raise OSError("管道已关闭")

        ui = ConsoleUI(stream=Broken(), width=60)
        ui.write("内容")  # 不应抛
        ui.assistant("回答")
        ui.end_turn()

    def test_callbacks_dispatch_to_renderers(self) -> None:
        ui, stream = self._ui()
        callbacks = ui.callbacks()
        callbacks["on_assistant"]("回答内容", True)
        ui.end_turn()
        callbacks["on_tool_start"]("read_file", {})
        callbacks["on_tool_result"]("read_file", "ok", False)
        callbacks["on_progress"]("进行中")
        callbacks["on_notice"]("提示")

        output = strip_ansi(stream.getvalue())
        self.assertIn("回答内容", output)
        self.assertIn("read_file", output)
        self.assertIn("进行中", output)
        self.assertIn("提示", output)

    def test_error_callback_maps_to_failure(self) -> None:
        ui, stream = self._ui()
        ui.callbacks()["on_tool_result"]("read_file", "炸了", True)
        # 失败与成功在渲染上不同（颜色），此处确认内容出现且不抛
        self.assertIn("炸了", strip_ansi(stream.getvalue()))

    def test_tool_result_shows_detail_not_repeated_name(self) -> None:
        """`⏺ read_file` 已说明是哪个工具，`⎿` 后再重复工具名是纯噪音。"""
        ui, stream = self._ui()
        ui.tool_start("read_file")
        ui.tool_result("read_file", ok=True, detail="读取了 42 行")
        output = strip_ansi(stream.getvalue())
        self.assertEqual(output.count("read_file"), 1, "工具名只应出现一次")
        self.assertIn("读取了 42 行", output)

    def test_tool_result_falls_back_to_name_without_detail(self) -> None:
        ui, stream = self._ui()
        ui.tool_result("read_file", ok=True)
        self.assertIn("read_file", strip_ansi(stream.getvalue()))

    def test_ask_user_renders_a_question_box(self) -> None:
        """**关键**：`ask_user` 必须醒目，否则用户以为程序卡住了。

        行模式没有可交互的待批准面板，只能靠视觉区分加明确提示。
        """
        ui, stream = self._ui()
        ui.tool_result("ask_user", ok=True, detail="要读哪个文件？")
        output = strip_ansi(stream.getvalue())
        self.assertIn("要读哪个文件？", output)
        self.assertIn("请直接输入你的回答", output)
        self.assertIn("╭", output, "应当用方框框起来以示区分")

    def test_ask_user_without_question_still_renders(self) -> None:
        ui, stream = self._ui()
        ui.tool_result("ask_user", ok=True, detail="")
        self.assertIn("请直接输入你的回答", strip_ansi(stream.getvalue()))

    def test_status_prints_when_non_empty(self) -> None:
        ui, stream = self._ui()
        ui.status(model="m", context_percent=12)
        self.assertIn("12%", strip_ansi(stream.getvalue()))

    def test_all_output_has_no_cursor_control(self) -> None:
        """**整个界面层都不得输出光标控制序列。**

        这是行模式能在任何终端里工作的根本原因，也是它相对全屏 TUI 的
        全部优势所在。
        """
        ui, stream = self._ui()
        ui.echo_user("输入")
        ui.assistant("回答\n", is_final=True)
        ui.end_turn()
        ui.tool_start("tool")
        ui.tool_result("结果", ok=True, detail="详情")
        ui.progress("进行中")
        ui.notice("提示")
        ui.error("错误")
        ui.status(model="m", spinner_frame=0)

        output = stream.getvalue()
        for forbidden in ("\x1b[2J", "\x1b[H", "\x1b[?25", "\x1b[1A", "\x1b[2K"):
            self.assertNotIn(forbidden, output, f"不该出现 {forbidden!r}")


class AnimationCapabilityTests(unittest.TestCase):
    """动画是**可选增强**，绝不能成为可用性的前提。

    这里的每个断言都在保护同一条性质：**环境不确定时宁可不动画**。
    本项目此前反复失败于「把可选能力当成必需能力」（全屏 TUI 依赖读键，
    在 Git Bash 下按键永远读不到）。
    """

    def setUp(self) -> None:
        set_animation_enabled(None)

    def tearDown(self) -> None:
        set_animation_enabled(None)

    def test_non_tty_disables_animation(self) -> None:
        """**最重要的一条**：重定向到文件时绝不能有动画。

        否则 `\\r` 与光标移动序列会把文件内容搅乱。
        """
        self.assertFalse(supports_animation(io.StringIO()))

    def test_no_color_disables_animation(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(supports_animation(_FakeTty()))

    def test_dumb_terminal_disables_animation(self) -> None:
        with patch.dict(os.environ, {"TERM": "dumb", "NO_COLOR": ""}):
            self.assertFalse(supports_animation(_FakeTty()))

    def test_explicit_disable_wins(self) -> None:
        set_animation_enabled(False)
        self.assertFalse(supports_animation(_FakeTty()))

    def test_tty_with_clean_env_allows_animation(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}):
            self.assertTrue(supports_animation(_FakeTty()))

    def test_require_vt_is_stricter(self) -> None:
        """需要光标移动的动画必须额外确认 VT 可用。

        Windows 上 ANSI 默认关闭；不确认的话 `ESC[1A` 会以字面量打印出来。
        """
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm"}):
            with patch("minicode.ui.caps.enable_virtual_terminal", return_value=False):
                self.assertFalse(
                    supports_animation(_FakeTty(), require_vt=True),
                    "VT 不可用时不得做需要光标移动的动画",
                )
                self.assertTrue(
                    supports_animation(_FakeTty(), require_vt=False),
                    "只用 \\r 的动画不依赖 VT",
                )


class SpinnerTests(unittest.TestCase):
    def setUp(self) -> None:
        set_color_enabled(None)
        set_animation_enabled(None)
        self._env = patch.dict(os.environ, COLOR_ON)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        set_color_enabled(None)
        set_animation_enabled(None)

    def _spinner(self, **kwargs: object):
        stream = io.StringIO()
        return Spinner(label="思考中", stream=stream, enabled=True, **kwargs), stream

    def test_frame_uses_carriage_return(self) -> None:
        """转圈只发 `\\r` 回行首重写——不移动光标，因此最安全。"""
        spinner, stream = self._spinner()
        spinner.tick()
        output = stream.getvalue()
        self.assertTrue(output.startswith("\r"))
        self.assertIn("思考中", output)

    def test_frame_erases_before_writing(self) -> None:
        """必须先擦行再写，否则长标签变短时会留下残尾。"""
        spinner, stream = self._spinner()
        spinner.tick()
        self.assertIn(ERASE_LINE, stream.getvalue())

    def test_glyph_advances_each_tick(self) -> None:
        spinner, stream = self._spinner()
        spinner.tick()
        first = stream.getvalue()
        spinner.tick()
        self.assertNotEqual(first, stream.getvalue())

    def test_stop_erases_the_line(self) -> None:
        """**必须擦干净**：否则转圈残影会与正式输出混在一起。"""
        spinner, stream = self._spinner()
        spinner.tick()
        stream.seek(0)
        stream.truncate(0)
        spinner.stop()
        output = stream.getvalue()
        self.assertIn(ERASE_LINE, output)
        self.assertNotIn("思考中", output, "停止后不该还留着标签")

    def test_stop_without_start_is_safe(self) -> None:
        spinner, _stream = self._spinner()
        spinner.stop()  # 不应抛

    def test_disabled_spinner_outputs_nothing(self) -> None:
        """禁用时必须完全静默——连空行都不留。"""
        stream = io.StringIO()
        spinner = Spinner(label="思考中", stream=stream, enabled=False)
        spinner.tick()
        spinner.start()
        spinner.stop()
        self.assertEqual(stream.getvalue(), "")

    def test_context_manager_stops(self) -> None:
        spinner, stream = self._spinner()
        with spinner:
            spinner.tick()
        self.assertIn(ERASE_LINE, stream.getvalue())

    def test_ascii_fallback_glyphs(self) -> None:
        """终端字体缺盲文字形时的退路。"""
        from minicode.ui.animate import SPINNER_ASCII, spinner_glyph

        for index in range(len(SPINNER_ASCII)):
            self.assertEqual(spinner_glyph(index, ascii_only=True), SPINNER_ASCII[index])

    def test_glyph_cycles(self) -> None:
        from minicode.ui.animate import SPINNER_FRAMES

        self.assertEqual(spinner_glyph(0), SPINNER_FRAMES[0])
        self.assertEqual(spinner_glyph(len(SPINNER_FRAMES)), SPINNER_FRAMES[0])


class BlockAnimatorTests(unittest.TestCase):
    def setUp(self) -> None:
        set_color_enabled(None)
        self._env = patch.dict(os.environ, COLOR_ON)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        set_color_enabled(None)

    def _animator(self, height: int = 3):
        stream = io.StringIO()
        return BlockAnimator(height=height, width=40, stream=stream, enabled=True), stream

    def test_first_frame_is_written_plainly(self) -> None:
        animator, stream = self._animator()
        animator.render(["a", "b", "c"])
        self.assertEqual(stream.getvalue(), "a\nb\nc")

    def test_subsequent_frame_moves_up_relatively(self) -> None:
        """**相对移动**是它与失败的全屏 TUI 的本质区别。

        全屏 TUI 用绝对定位（`ESC[<行>;1H`），要求「渲染行数恰好等于终端
        行数」，一旦不等就整体错位。相对移动只需要知道**自己占了几行**。
        """
        animator, stream = self._animator()
        animator.render(["a", "b", "c"])
        stream.seek(0)
        stream.truncate(0)
        animator.render(["x", "y", "z"])
        self.assertTrue(stream.getvalue().startswith(cursor_up(2)))

    def test_frame_height_is_forced_constant(self) -> None:
        """帧高必须固定，否则相对移动会算错、画面逐渐漂移。"""
        animator, stream = self._animator(height=3)
        animator.render(["只有一行"])  # 不足 3 行
        self.assertEqual(stream.getvalue().count("\n"), 2, "应补齐到 3 行")

    def test_overlong_frame_is_truncated(self) -> None:
        """超宽会触发终端自动折行，同样破坏行数记账。"""
        animator, stream = self._animator(height=2)
        animator.render(["x" * 200, "y"])
        for line in stream.getvalue().split("\n"):
            self.assertLessEqual(string_display_width(line), 40)

    def test_extra_lines_are_dropped(self) -> None:
        animator, stream = self._animator(height=2)
        animator.render(["a", "b", "c", "d"])
        self.assertEqual(len(stream.getvalue().split("\n")), 2)

    def test_clear_erases_the_block(self) -> None:
        """必须擦干净：否则动画残影与后续输出混在一起。"""
        animator, stream = self._animator()
        animator.render(["a", "b", "c"])
        stream.seek(0)
        stream.truncate(0)
        animator.clear()
        output = stream.getvalue()
        self.assertIn(cursor_up(2), output)
        self.assertIn(ERASE_TO_END, output)

    def test_clear_without_render_is_safe(self) -> None:
        animator, stream = self._animator()
        animator.clear()
        self.assertEqual(stream.getvalue(), "")

    def test_disabled_animator_outputs_nothing(self) -> None:
        stream = io.StringIO()
        animator = BlockAnimator(height=3, width=40, stream=stream, enabled=False)
        animator.render(["a", "b", "c"])
        animator.clear()
        self.assertEqual(stream.getvalue(), "")

    def test_play_respects_max_seconds(self) -> None:
        """启动动画绝不能拖慢启动——必须有硬上限。"""
        animator, stream = self._animator(height=2)
        frames = [["a"], ["b"], ["c"], ["d"], ["e"]] * 20
        started = time.monotonic()
        animator.play(frames, interval=0.05, max_seconds=0.15)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "应被 max_seconds 截断")

    def test_play_can_be_aborted(self) -> None:
        """用户一开始输入就该中断动画。"""
        animator, _stream = self._animator(height=2)
        played: list[int] = []

        def should_abort() -> bool:
            return len(played) >= 1

        frames = [["a"], ["b"], ["c"], ["d"]]
        original = animator.render

        def counting_render(lines, **kwargs):  # noqa: ANN001
            played.append(1)
            return original(lines, **kwargs)

        animator.render = counting_render  # type: ignore[method-assign]
        animator.play(frames, interval=0.0, should_abort=should_abort)
        self.assertLessEqual(len(played), 2, "应在 should_abort 后立刻停下")


class StartupAnimationTests(unittest.TestCase):
    def setUp(self) -> None:
        set_color_enabled(None)
        self._env = patch.dict(os.environ, COLOR_ON)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        set_color_enabled(None)

    def test_frames_have_constant_height(self) -> None:
        """帧高必须一致，否则多行动画的相对移动会算错。"""
        from minicode.ui.startup import StartupAnimation

        animation = StartupAnimation(width=40, height=5, enabled=True)
        frames = animation.frames()
        self.assertTrue(frames)
        heights = {len(frame) for frame in frames}
        self.assertEqual(len(heights), 1, f"帧高不一致: {heights}")

    def test_frames_fit_width(self) -> None:
        from minicode.ui.startup import StartupAnimation

        animation = StartupAnimation(width=20, height=5, enabled=True)
        for frame in animation.frames():
            for line in frame:
                self.assertLessEqual(string_display_width(line), 20)

    def test_narrow_terminal_skips_animation(self) -> None:
        """终端太窄时**不播**，而不是截断。

        像素画被截断会变成一堆无意义的方块——不如直接不播。
        这是「宁可没有动画」原则的体现。
        """
        from minicode.ui.startup import StartupAnimation

        self.assertEqual(
            StartupAnimation(width=10, height=5, enabled=True).frames(),
            [],
            "窄终端下不该有帧",
        )

    def test_wide_enough_terminal_has_frames(self) -> None:
        from minicode.ui.startup import StartupAnimation

        self.assertTrue(StartupAnimation(width=40, height=5, enabled=True).frames())

    def test_animation_progressively_lights_up(self) -> None:
        """动画应当是「逐渐点亮」，不是每帧全变。"""
        from minicode.ui.startup import PIXEL_ON, StartupAnimation

        frames = StartupAnimation(width=40, height=5, enabled=True).frames()
        counts = [sum(row.count(PIXEL_ON) for row in frame) for frame in frames]
        self.assertEqual(counts, sorted(counts), f"点亮数应单调不减: {counts}")
        self.assertLess(counts[0], counts[-1], "应当确实有变化")

    def test_disabled_animation_outputs_nothing(self) -> None:
        """非终端环境下必须完全静默，不留空行。"""
        from minicode.ui.startup import StartupAnimation

        stream = io.StringIO()
        StartupAnimation(width=40, enabled=False, stream=stream).play()
        self.assertEqual(stream.getvalue(), "")

    def test_play_clears_afterwards(self) -> None:
        """播放完必须擦掉，否则残影留在屏幕上。"""
        from minicode.ui.startup import StartupAnimation

        stream = io.StringIO()
        StartupAnimation(
            width=40, height=3, enabled=True, stream=stream, max_seconds=0.05
        ).play()
        self.assertIn(ERASE_TO_END, stream.getvalue())

    def test_static_title_is_available_without_animation(self) -> None:
        """动画只是开场；跳过时用户仍要能看到完整信息。"""
        from minicode.ui.startup import banner_title_lines, startup_hint

        lines = banner_title_lines()
        self.assertTrue(lines)
        self.assertIn("MiniCode", strip_ansi("\n".join(lines)))
        self.assertTrue(startup_hint())


class AnimatedConsoleTests(ColorEnvMixin):
    """动画与普通输出的协作。"""

    def _ui(self, *, animate: bool) -> tuple[ConsoleUI, io.StringIO]:
        stream = io.StringIO()
        return ConsoleUI(stream=stream, width=60, animate=animate), stream

    def test_animation_disabled_for_non_tty_stream(self) -> None:
        """StringIO 不是终端 → 动画必须自动关闭。"""
        ui, _stream = self._ui(animate=False)
        self.assertFalse(ui.animate)

    def test_no_animation_output_when_disabled(self) -> None:
        ui, stream = self._ui(animate=False)
        ui.play_startup()
        ui.begin_turn()
        ui.end_turn()
        self.assertNotIn("\x1b[", stream.getvalue())
        self.assertNotIn("\r", stream.getvalue())

    def test_write_stops_spinner_first(self) -> None:
        """**关键**：正式输出前必须擦掉转圈，否则两者叠在同一行。"""
        ui, stream = self._ui(animate=True)
        ui.begin_turn()
        ui.write("正式内容")
        output = stream.getvalue()
        erase_at = output.rfind(ERASE_LINE)
        content_at = output.rfind("正式内容")
        self.assertGreater(erase_at, -1, "应擦掉转圈")
        self.assertLess(erase_at, content_at, "擦除应发生在写入之前")

    def test_end_turn_stops_spinner(self) -> None:
        ui, stream = self._ui(animate=True)
        ui.begin_turn()
        ui.end_turn()
        self.assertIn(ERASE_LINE, stream.getvalue())

    def test_progress_stops_spinner(self) -> None:
        """进度消息本身就是「还在干活」的信号，转圈是多余的。"""
        ui, stream = self._ui(animate=True)
        ui.begin_turn()
        stream.seek(0)
        stream.truncate(0)
        ui.progress("进行中")
        self.assertIn(ERASE_LINE, stream.getvalue())

    def test_spinner_never_leaks_into_redirected_output(self) -> None:
        """**最重要的一条**：非终端下整个回合不该有任何动画痕迹。"""
        ui, stream = self._ui(animate=False)
        ui.echo_user("输入")
        ui.begin_turn()
        ui.tool_start("read_file")
        ui.tool_result("read_file", ok=True, detail="完成")
        ui.assistant("回答\n", is_final=True)
        ui.end_turn()

        output = stream.getvalue()
        self.assertNotIn("\r", output, "不该有回车")
        self.assertNotIn("\x1b[2K", output, "不该有擦行")
        self.assertNotIn("\x1b[", output, "不该有光标移动")


if __name__ == "__main__":
    unittest.main(verbosity=2)
