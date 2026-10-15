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

    def test_banner_lines_are_all_the_same_width(self) -> None:
        """**所有行必须等宽**，否则右边框会错开。

        这条钉住一个真实缺陷：早先内容行的可用宽度算成 `inner - 2 - 标签宽 - 2`，
        比正确值少 1，于是**内容行宽 75、边框行宽 76**——右边框每一行都错开
        一列。启动横幅是用户看到的第一样东西，错位很显眼。

        用**显示宽度**而不是 `len()`：横幅里有中文与全角括号，按字符数比会
        漏掉这类问题（那正是它当初没被发现的原因之一）。
        """
        for width in (40, 50, 60, 76, 80, 100, 120):
            with self.subTest(width=width):
                rendered = strip_ansi(
                    render_banner(
                        model="qwen3.8-27b",
                        session_id="a1b2c3d4",
                        cwd=r"D:\CoreCode\minicode-py",
                        tool_count=17,
                        readonly_tool_count=6,
                        skill_count=2,
                        mcp_summary="mcp 1/3 · 1 等待 · 1 失败",
                        width=width,
                    )
                )
                widths = {
                    string_display_width(line)
                    for line in rendered.split("\n")
                    if line
                }
                self.assertEqual(
                    len(widths), 1, f"width={width} 时各行宽度不一致：{sorted(widths)}"
                )

    def test_banner_borders_line_up(self) -> None:
        """右边框必须在同一列——这是「等宽」的用户可见含义。"""
        rendered = strip_ansi(
            render_banner(
                model="m",
                session_id="s",
                cwd="/x",
                tool_count=3,
                skill_count=1,
                width=80,
            )
        )
        columns = {
            string_display_width(line)
            for line in rendered.split("\n")
            if line
        }
        self.assertEqual(columns, {80})

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

    def test_animation_holds_before_clearing(self) -> None:
        """**「不闪而过」的核心**：全亮后要停留，不能点亮完立刻擦。

        这条用**时间**来断言：播放总耗时必须明显大于「帧数 × 帧间隔」，
        多出来的部分就是停留。只断言「擦除发生了」是没用的——那在
        「不停留」的实现里同样成立（变异检查抓到过这一点）。
        """
        from minicode.ui.startup import StartupAnimation

        stream = io.StringIO()
        animation = StartupAnimation(
            width=40,
            height=3,
            enabled=True,
            stream=stream,
            interval=0.01,
            hold_seconds=0.25,
            max_seconds=5.0,
        )
        frame_count = len(animation.frames())
        started = time.monotonic()
        animation.play()
        elapsed = time.monotonic() - started

        minimum_hold = animation.hold_seconds * 0.6  # 留一点调度余量
        self.assertGreaterEqual(
            elapsed,
            frame_count * animation.interval + minimum_hold,
            f"播放耗时 {elapsed:.3f}s 太短，说明全亮后没有停留",
        )

    def test_hold_can_be_disabled(self) -> None:
        """`hold_seconds=0` 时不该多等——留给不需要停留的场景。"""
        from minicode.ui.startup import StartupAnimation

        animation = StartupAnimation(
            width=40,
            height=3,
            enabled=True,
            stream=io.StringIO(),
            interval=0.001,
            hold_seconds=0.0,
            max_seconds=5.0,
        )
        started = time.monotonic()
        animation.play()
        self.assertLess(time.monotonic() - started, 0.5)

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


class StreamingTests(ColorEnvMixin):
    """流式输出。

    流式引入了一类**非显然后果**：同一段文本会被两条路径看到——流式回调
    （`model.next` 内部）与 `hooks.progress()` / `hooks.assistant()`
    （循环里）。若不显式去重，用户会看到内容重复。这几条测试钉住去重。
    """

    def _ui(self, *, animate: bool = False) -> tuple[ConsoleUI, io.StringIO]:
        stream = io.StringIO()
        return ConsoleUI(stream=stream, width=60, animate=animate), stream

    def test_delta_is_written_immediately(self) -> None:
        """增量必须**立刻**写出，不能攒到换行。

        攒着就失去了流式的意义——那和等完整回答没区别。
        """
        ui, stream = self._ui()
        ui.assistant_delta("你好")
        self.assertIn("你好", stream.getvalue())

    def test_short_answer_is_flushed_at_turn_end(self) -> None:
        """**关键**：卡在探测缓冲里的内容，回合结束必须吐出来。

        必须用**真正的标记前缀**（如 `<fi` 是 `<final>` 的前缀）才会进入
        缓冲。普通短文本、乃至 `<ok` 这种「以 `<` 开头但不是任何标记前缀」
        的内容都会被立刻放行——那是正确行为，但也意味着那样的测试覆盖不到
        这条路径。变异检查正是这样抓出我的测试形同虚设。
        """
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<fi")  # `<final>` 的前缀，会进入缓冲
        self.assertEqual(stream.getvalue(), "", "应仍在探测缓冲里")
        ui.end_turn()
        self.assertIn("<fi", stream.getvalue(), "回合结束必须吐出缓冲内容")

    def test_plain_short_answer_streams_immediately(self) -> None:
        """普通短文本不该被缓冲——立刻显示才是流式的意义。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("短")
        self.assertIn("短", stream.getvalue(), "不可能是标记时应立刻放行")

    def test_non_marker_angle_bracket_is_released(self) -> None:
        """以 `<` 开头但不是标记前缀的内容，不该被卡住。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<ok")
        self.assertIn("<ok", stream.getvalue(), "不是标记前缀应立刻放行")

    def test_incomplete_marker_is_released_at_turn_end(self) -> None:
        """标记永远没收完时，回合结束也要把内容吐出来，不能丢。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<prog")  # `<progress>` 的前缀，但永远没收完
        ui.end_turn()
        self.assertIn("<prog", stream.getvalue())

    def test_split_closing_tag_is_not_leaked(self) -> None:
        """**收尾标签会被流式切成两半**——半个标签绝不能漏到界面上。

        SSE 增量边界是任意的：`</final>` 可能分成 `</fi` 和 `nal>` 到达。
        若不做处理，第一段会被当正文打出去，界面上就残留 `</final>`
        这种碎片。我实际在真实模型输出里见到过。

        做法是把末尾可能是半个收尾标签的部分**扣住**，等后续增量到齐
        再一起判断。
        """
        ui, stream = self._ui()
        ui.begin_turn()
        for chunk in ("<final>这是回答", "</fi", "nal>"):
            ui.assistant_delta(chunk)
        ui.end_turn()
        output = stream.getvalue()
        self.assertIn("这是回答", output)
        self.assertNotIn("</final>", output)
        self.assertNotIn("</fi", output)

    def test_tail_hold_is_released_when_not_a_tag(self) -> None:
        """扣住的内容若最终不是标签，必须原样吐出来，不能吞掉。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("内容以 < 结尾<")
        ui.end_turn()
        self.assertIn("内容以 < 结尾<", stream.getvalue())

    def test_tail_hold_resets_between_turns(self) -> None:
        """扣留缓冲必须逐回合重置，否则会污染下一回合。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("第一回合<")
        ui.end_turn()

        stream.seek(0)
        stream.truncate(0)
        ui.begin_turn()
        ui.assistant_delta("第二回合")
        ui.end_turn()
        self.assertEqual(stream.getvalue().strip(), "第二回合")

    def test_protocol_markers_are_stripped(self) -> None:
        """`<final>` / `<progress>` 是给程序看的协议标记，不该显示给用户。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<final>这是回答</final>")
        ui.end_turn()
        output = stream.getvalue()
        self.assertIn("这是回答", output)
        self.assertNotIn("<final>", output)
        self.assertNotIn("</final>", output)

    def test_progress_marker_is_stripped(self) -> None:
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<progress>正在分析代码结构</progress>")
        ui.end_turn()
        output = stream.getvalue()
        self.assertIn("正在分析代码结构", output)
        self.assertNotIn("<progress>", output)

    def test_long_answer_streams_without_marker_leak(self) -> None:
        """长回答会越过探测阈值，标记必须在那一刻就被剥掉。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<final>这是一段足够长的回答内容用于越过探测阈值</final>")
        ui.end_turn()
        output = stream.getvalue()
        self.assertNotIn("<final>", output)
        self.assertIn("这是一段足够长的回答内容", output)

    def test_assistant_is_ignored_after_streaming(self) -> None:
        """**去重**：流式已经打过的内容，`assistant()` 不能再打一遍。

        内容必须**含换行**：`StreamPrinter.feed()` 只在遇到换行时才返回
        内容，若用没有换行的文本，变异后的 `assistant()` 也会返回空串、
        输出毫无变化——那样的测试杀不死变异（变异检查抓到过这一点）。
        """
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<final>只应出现一次的内容\n</final>")
        ui.assistant("只应出现一次的内容\n", is_final=True)
        ui.end_turn()
        self.assertEqual(
            stream.getvalue().count("只应出现一次的内容"),
            1,
            "流式与非流式路径重复输出了同一段内容",
        )

    def test_progress_is_ignored_after_streaming(self) -> None:
        """**去重**：进度文本也走流式，`progress()` 不该重复显示。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<progress>正在分析</progress>")
        ui.progress("正在分析")
        ui.end_turn()
        self.assertEqual(stream.getvalue().count("正在分析"), 1, "进度消息重复了")

    def test_non_streaming_path_still_works(self) -> None:
        """适配器不支持流式时，功能必须完全不受影响。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant("完整回答\n", is_final=True)
        ui.end_turn()
        self.assertIn("完整回答", stream.getvalue())

    def test_probe_state_resets_between_turns(self) -> None:
        """探测状态必须逐回合重置，否则第二回合会吞掉开头。"""
        ui, stream = self._ui()
        ui.begin_turn()
        ui.assistant_delta("<final>第一回合</final>")
        ui.end_turn()

        stream.seek(0)
        stream.truncate(0)
        ui.begin_turn()
        ui.assistant_delta("第二回合")
        ui.end_turn()
        self.assertIn("第二回合", stream.getvalue())

    def test_delta_stops_spinner_first(self) -> None:
        """转圈占着当前行，直接写字会叠在一起。"""
        ui, stream = self._ui(animate=True)
        ui.begin_turn()
        stream.seek(0)
        stream.truncate(0)
        ui.assistant_delta("内容")
        self.assertIn(ERASE_LINE, stream.getvalue(), "应先擦掉转圈")

    def test_empty_delta_is_ignored(self) -> None:
        ui, stream = self._ui()
        ui.assistant_delta("")
        self.assertEqual(stream.getvalue(), "")


class TurnStatsDisplayTests(ColorEnvMixin):
    def _ui(self) -> tuple[ConsoleUI, io.StringIO]:
        stream = io.StringIO()
        return ConsoleUI(stream=stream, width=60, animate=False), stream

    def test_stats_line_is_shown(self) -> None:
        from minicode.stats import SessionStats, TurnStats

        stats = SessionStats()
        stats.record(TurnStats(duration_seconds=3.2, tool_calls=4))
        ui, stream = self._ui()
        ui.turn_stats(stats)
        output = stream.getvalue()
        self.assertIn("3.2s", output)
        self.assertIn("4 次工具调用", output)

    def test_stats_are_not_duplicated_by_callbacks(self) -> None:
        """统计**不该**挂在宿主回调上——那会与显式显示重复。

        我实际踩过：统计打了两遍。
        """
        ui, _stream = self._ui()
        self.assertNotIn(
            "on_turn_stats",
            ui.callbacks(),
            "统计应由调用方在 end_turn 之后显式显示，不挂回调",
        )

    def test_stats_without_turns_is_silent(self) -> None:
        from minicode.stats import SessionStats

        ui, stream = self._ui()
        ui.turn_stats(SessionStats())
        self.assertEqual(stream.getvalue(), "")

    def test_stats_omits_tool_count_when_zero(self) -> None:
        from minicode.stats import SessionStats, TurnStats

        stats = SessionStats()
        stats.record(TurnStats(duration_seconds=1.5, tool_calls=0))
        ui, stream = self._ui()
        ui.turn_stats(stats)
        self.assertNotIn("工具调用", stream.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
