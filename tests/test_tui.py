"""TUI 的行为测试 —— 内联实时区域 + 仿 Claude Code 布局。

重点在三块：

  1. **宽度不变式**：内联重绘靠行数记账定位，任何一行超宽都会被终端自动
     折行、把记账打乱。因此「每行 ≤ 终端宽」是正确性前提，必须钉住。
  2. **行比对**：没变的行不重写（不闪的根源），变了的行才擦除重写。
  3. **交互循环**：用脚本化按键真正跑完打字 → 提交 → 回合 → 退出。
     `runner.py` 与 `terminal.py` 曾经完全没有测试，而 TUI 第一次实测
     无法使用正是出在那里。
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.tui.app import (  # noqa: E402
    AppState,
    BannerStats,
    clamp_width,
    compose_region,
    compute_region_height,
    handle_key,
    render_input_box,
    render_status_line,
    transcript_viewport,
)
from minicode.tui.screen import (  # noqa: E402
    ERASE_LINE,
    LiveRegion,
    cursor_up,
    split_for_display,
)
from minicode.tui.terminal import (  # noqa: E402
    KEY_BACKSPACE,
    KEY_CTRL_C,
    KEY_CTRL_D,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_HOME,
    KEY_LEFT,
    KEY_PAGE_DOWN,
    KEY_PAGE_UP,
    KEY_RIGHT,
    KEY_TAB,
    KEY_UP,
    Terminal,
    TerminalSize,
    decode_key,
)
from minicode.tui.theme import (  # noqa: E402
    BOX_BOTTOM_LEFT,
    BOX_TOP_LEFT,
    BOX_VERTICAL,
    GLYPH_TOOL,
    color_enabled,
    paint,
)
from minicode.tui.width import (  # noqa: E402
    char_display_width,
    string_display_width,
    strip_ansi,
    truncate_plain,
    wrap_plain,
)

unittest.TestCase.maxDiff = None

SIZE = TerminalSize(columns=80, rows=24)


def _state(**banner: object) -> AppState:
    return AppState(banner=BannerStats(**banner))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 宽度（渲染的地基）
# ---------------------------------------------------------------------------


class DisplayWidthTests(unittest.TestCase):
    def test_ascii_is_one_column(self) -> None:
        self.assertEqual(string_display_width("hello"), 5)

    def test_cjk_is_two_columns(self) -> None:
        self.assertEqual(string_display_width("中文"), 4)

    def test_combining_marks_are_zero_width(self) -> None:
        self.assertEqual(char_display_width("\u0301"), 0)
        self.assertEqual(string_display_width("e\u0301"), 1)

    def test_emoji_is_two_columns(self) -> None:
        self.assertEqual(string_display_width("😀"), 2)

    def test_ansi_is_not_counted(self) -> None:
        self.assertEqual(string_display_width("\x1b[31mred\x1b[0m"), 3)

    def test_truncate_respects_wide_characters(self) -> None:
        result = truncate_plain("中文字符", 7)
        self.assertLessEqual(string_display_width(result), 7)

    def test_wrap_never_exceeds_width(self) -> None:
        text = "中文混排 with some english words 和更多中文内容 here"
        for line in wrap_plain(text, 12):
            self.assertLessEqual(string_display_width(line), 12)


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------


class ThemeTests(unittest.TestCase):
    def test_paint_wraps_with_reset(self) -> None:
        # 显式清掉 NO_COLOR：测试不能依赖外部环境。
        # 早先这条断言在「环境里恰好有 NO_COLOR」时会失败——那说明测试
        # 本身对环境有隐含依赖，是测试的缺陷而不是代码的。
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paint("x", "\x1b[31m"), "\x1b[31mx\x1b[0m")

    def test_paint_without_codes_is_identity(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paint("x"), "x")

    def test_no_color_disables_painting(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(color_enabled())
            self.assertEqual(paint("x", "\x1b[31m"), "x")

    def test_reversed_arguments_raise_instead_of_rendering_wrong(self) -> None:
        """把颜色码当成正文传进来时必须报错，不能静默画错。

        我实际写成过 `paint(COLOR_STATUS, BOX_HORIZONTAL * n)`，结果边框
        字符变成转义序列字面量、被截断函数剥掉后什么都不剩——输入框看起来
        「消失了」，而且不报任何错。
        """
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AssertionError):
                paint("\x1b[38;5;245m", "───")

    def test_theme_does_not_mutate_environment(self) -> None:
        """`color_enabled` 只读环境，绝不写入。

        早先的实现用 `os.environ.setdefault("NO_COLOR", "1")` 表达「不支持
        ANSI」这个运行时状态，污染了整个进程并泄漏到子进程。
        """
        with patch.dict(os.environ, {}, clear=True):
            color_enabled()
            self.assertNotIn("NO_COLOR", os.environ)

    def test_rendering_works_under_no_color(self) -> None:
        """`NO_COLOR` 下仍必须画出完整布局（只是没有颜色）。

        这是用户色彩显示异常时唯一的自救手段，不能因此让界面缺东西。
        """
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            state = _state()
            state.set_input("你好")
            region = compose_region(state, 80, 24)
            joined = "\n".join(region.lines)
            self.assertNotIn("\x1b[", joined, "NO_COLOR 下不该有颜色序列")
            self.assertIn(BOX_TOP_LEFT, joined, "边框必须仍然存在")
            self.assertIn("你好", joined)


# ---------------------------------------------------------------------------
# 内联实时区域
# ---------------------------------------------------------------------------


class LiveRegionTests(unittest.TestCase):
    def test_first_render_writes_plainly(self) -> None:
        region = LiveRegion()
        output = region.render(["a", "b"], cursor_row=1)
        self.assertEqual(output, "a\nb")
        self.assertEqual(region.height, 2)

    def test_first_render_positions_cursor_when_needed(self) -> None:
        """首帧也要定位：写完多行后光标在最后一行，目标行通常在中间。"""
        region = LiveRegion()
        output = region.render(["a", "b", "c"], cursor_row=0)
        self.assertTrue(output.startswith("a\nb\nc"))
        self.assertTrue(output.endswith(cursor_up(2)))

    def test_unchanged_region_produces_no_output_at_all(self) -> None:
        """**这是「不闪」与「可测」的根源**：完全没变就一个字节都不写。

        内联模式每 30ms 重绘一次；若「什么都没变」也照发一串光标移动序列，
        既是无谓写入，也让「无变化」无法被断言。
        """
        region = LiveRegion()
        region.render(["a", "b"], cursor_row=0)
        output = region.render(["a", "b"], cursor_row=0)
        self.assertEqual(output, "")

    def test_cursor_only_change_still_emits_positioning(self) -> None:
        """内容没变但光标该换行时，仍要输出定位序列。"""
        region = LiveRegion()
        region.render(["a", "b", "c"], cursor_row=2)
        output = region.render(["a", "b", "c"], cursor_row=0)
        self.assertNotEqual(output, "")
        self.assertIn(cursor_up(2), output)

    def test_changed_line_is_erased_and_rewritten(self) -> None:
        region = LiveRegion()
        region.render(["a", "b"])
        output = region.render(["a", "B"])
        self.assertIn(ERASE_LINE, output)
        self.assertIn("B", output)

    def test_only_the_changed_line_is_rewritten(self) -> None:
        """**逐行比对的核心断言**：只有变化的那一行被擦除重写。

        这条必须单独存在：`test_unchanged_region_produces_no_output_at_all`
        覆盖的是「整帧完全相同」——那条走的是提前返回，**掩盖了逐行比对
        是否真的生效**。变异检查抓到过这一点（把 `if old == line: continue`
        改成 `if False:` 之后，所有测试照样通过）。
        """
        region = LiveRegion()
        region.render(["a", "b", "c"], cursor_row=2)
        output = region.render(["a", "B", "c"], cursor_row=2)
        self.assertEqual(
            output.count(ERASE_LINE), 1, "只有一行变了，就只该擦一行"
        )
        self.assertIn("B", output)
        self.assertNotIn("a", output, "未变化的行不该被重写")
        self.assertNotIn("c", output, "未变化的行不该被重写")

    def test_unchanged_lines_are_skipped_but_changed_ones_are_not(self) -> None:
        """混合场景：部分行变、部分不变，两边都要正确。"""
        region = LiveRegion()
        region.render(["one", "two", "three", "four"], cursor_row=3)
        output = region.render(["one", "TWO", "three", "four"], cursor_row=3)
        self.assertEqual(output.count(ERASE_LINE), 1)
        self.assertIn("TWO", output)
        for untouched in ("one", "three", "four"):
            self.assertNotIn(untouched, output)

    def test_cursor_returns_to_region_top_before_redraw(self) -> None:
        region = LiveRegion()
        region.render(["a", "b", "c"])
        output = region.render(["a", "b", "C"])
        self.assertTrue(output.startswith(cursor_up(2)), "应先回到区域首行")

    def test_growing_region_keeps_previous_lines(self) -> None:
        region = LiveRegion()
        region.render(["a"])
        output = region.render(["a", "b"])
        self.assertIn("b", output)
        self.assertEqual(region.height, 2)

    def test_shrinking_region_clears_leftovers(self) -> None:
        """区域变短时必须擦掉残影，否则旧内容留在屏幕上。"""
        region = LiveRegion()
        region.render(["a", "b", "c"])
        output = region.render(["a"])
        self.assertEqual(region.height, 1)
        self.assertGreaterEqual(output.count(ERASE_LINE), 1)

    def test_empty_input_is_treated_as_one_blank_line(self) -> None:
        region = LiveRegion()
        region.render([])
        self.assertEqual(region.height, 1)

    def test_reset_forces_full_redraw(self) -> None:
        region = LiveRegion()
        region.render(["a", "b"], cursor_row=1)
        region.reset()
        output = region.render(["a", "b"], cursor_row=1)
        self.assertEqual(output, "a\nb", "重置后应按首帧处理")


class SplitForDisplayTests(unittest.TestCase):
    def test_trailing_newline_does_not_add_a_line(self) -> None:
        """末尾换行不该产生额外空行——否则每帧都多一行，布局越推越高。"""
        self.assertEqual(split_for_display("a\nb\n"), ["a", "b"])

    def test_inner_blank_lines_are_kept(self) -> None:
        self.assertEqual(split_for_display("a\n\nb"), ["a", "", "b"])

    def test_empty_text_is_one_line(self) -> None:
        self.assertEqual(split_for_display(""), [""])


# ---------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------


class ClampWidthTests(unittest.TestCase):
    """宽度不变式——内联重绘的正确性前提。"""

    def test_overlong_lines_are_truncated(self) -> None:
        lines = clamp_width(["x" * 100, "short"], 40)
        for line in lines:
            self.assertLessEqual(string_display_width(line), 40)

    def test_wide_characters_respect_the_limit(self) -> None:
        lines = clamp_width(["中" * 100], 40)
        self.assertLessEqual(string_display_width(lines[0]), 40)

    def test_short_lines_are_untouched(self) -> None:
        self.assertEqual(clamp_width(["a", "bb"], 40), ["a", "bb"])

    def test_ansi_colour_is_preserved_when_truncating(self) -> None:
        lines = clamp_width(["\x1b[31m" + "x" * 100 + "\x1b[0m"], 20)
        self.assertIn("\x1b[31m", lines[0])


class RegionGeometryTests(unittest.TestCase):
    def test_region_never_exceeds_rows_minus_one(self) -> None:
        """留一行给退出后的 shell 提示符，也避免顶到屏幕顶端。"""
        for rows in (10, 20, 24, 40, 60):
            with self.subTest(rows=rows):
                self.assertLessEqual(compute_region_height(rows), max(1, rows - 1))

    def test_region_has_a_floor_for_tiny_terminals(self) -> None:
        self.assertGreaterEqual(compute_region_height(3), 8)

    def test_viewport_leaves_room_for_chrome(self) -> None:
        self.assertLess(transcript_viewport(24), compute_region_height(24))


class RegionCompositionTests(unittest.TestCase):
    def test_every_line_fits_the_width(self) -> None:
        """**核心不变式**：任何列宽下都不能有超宽行。"""
        state = _state(model="qwen3.8-27b", session_id="abc", tool_count=12)
        state.push("user", "中文很长的用户输入" * 8)
        state.push("assistant", "回答内容" * 40)
        state.push("tool", "grep_files", ok=True, detail="匹配 12 处" * 10)

        for columns in (40, 60, 80, 100, 140, 200):
            with self.subTest(columns=columns):
                region = compose_region(state, columns, 24)
                for index, line in enumerate(region.lines):
                    self.assertLessEqual(
                        string_display_width(line),
                        columns,
                        f"{columns} 列下第 {index} 行超宽: {line!r}",
                    )

    def test_region_height_matches_terminal(self) -> None:
        state = _state()
        for rows in (20, 24, 30, 40):
            with self.subTest(rows=rows):
                region = compose_region(state, 80, rows)
                self.assertLessEqual(len(region.lines), max(1, rows - 1))

    def test_empty_transcript_shows_a_hint(self) -> None:
        region = compose_region(_state(), 80, 24)
        self.assertIn("输入一条消息开始", strip_ansi("\n".join(region.lines)))

    def test_banner_only_on_first_frame(self) -> None:
        state = _state(model="m", session_id="s")
        with_banner = strip_ansi(
            "\n".join(compose_region(state, 80, 24, banner=True).lines)
        )
        without = strip_ansi(
            "\n".join(compose_region(state, 80, 24, banner=False).lines)
        )
        self.assertIn("MiniCode", with_banner)
        self.assertNotIn("MiniCode", without)

    def test_transcript_content_is_visible(self) -> None:
        state = _state()
        state.push("user", "标记文本")
        state.push("assistant", "回答标记")
        rendered = strip_ansi("\n".join(compose_region(state, 80, 24).lines))
        self.assertIn("标记文本", rendered)
        self.assertIn("回答标记", rendered)

    def test_tool_entry_uses_claude_code_glyphs(self) -> None:
        """`⏺` 起头、`⎿` 承接结果——这是 Claude Code 最有辨识度的记号。"""
        state = _state()
        state.push("tool", "read_file", ok=True, detail="读取完成")
        rendered = "\n".join(compose_region(state, 80, 24).lines)
        self.assertIn(GLYPH_TOOL, rendered)
        self.assertIn("⎿", rendered)
        self.assertIn("读取完成", rendered)

    def test_input_box_has_rounded_border(self) -> None:
        state = _state()
        state.set_input("你好")
        lines = render_input_box(state, 40)
        self.assertEqual(len(lines), 3)
        self.assertIn(BOX_TOP_LEFT, lines[0])
        self.assertIn(BOX_BOTTOM_LEFT, lines[2])
        self.assertIn(BOX_VERTICAL, lines[1])
        for line in lines:
            self.assertEqual(string_display_width(line), 40, "输入框每行必须精确等宽")

    def test_input_box_survives_narrow_terminals(self) -> None:
        state = _state()
        state.set_input("很长很长的输入内容" * 5)
        for width in (20, 30, 40):
            with self.subTest(width=width):
                for line in render_input_box(state, width):
                    self.assertEqual(string_display_width(line), width)

    def test_cursor_lands_after_the_input_text(self) -> None:
        state = _state()
        state.set_input("abc")
        region = compose_region(state, 80, 24)
        line = strip_ansi(region.lines[region.cursor_row])
        self.assertIn("abc", line)
        # 光标列应指向最后一个字符之后（1 基）
        self.assertEqual(region.cursor_column, line.index("abc") + len("abc") + 1)

    def test_cursor_column_is_clamped_to_width(self) -> None:
        state = _state()
        state.set_input("x" * 500)
        for width in (40, 80):
            with self.subTest(width=width):
                region = compose_region(state, width, 24)
                self.assertLessEqual(region.cursor_column, width)

    def test_scrolled_view_reports_offset(self) -> None:
        state = _state()
        for index in range(100):
            state.push("assistant", f"第 {index} 行")
        compose_region(state, 80, 24)
        state.scroll_offset = 30
        rendered = strip_ansi("\n".join(compose_region(state, 80, 24).lines))
        self.assertIn("已上翻", rendered)


class StatusLineTests(unittest.TestCase):
    def test_spinner_animates(self) -> None:
        state = _state(model="m")
        state.running = True
        first = strip_ansi(render_status_line(state, 80))
        state.animation_frame = 1
        second = strip_ansi(render_status_line(state, 80))
        self.assertNotEqual(first, second)

    def test_shows_model_and_context(self) -> None:
        state = _state(model="qwen3.8-27b", context_percent=42)
        line = strip_ansi(render_status_line(state, 120))
        self.assertIn("qwen3.8-27b", line)
        self.assertIn("42%", line)

    def test_falls_back_to_hint_when_idle(self) -> None:
        state = _state()
        self.assertIn("Enter", strip_ansi(render_status_line(state, 120)))

    def test_never_exceeds_width(self) -> None:
        state = _state(model="m" * 200, context_percent=99)
        for width in (20, 40, 80):
            with self.subTest(width=width):
                self.assertLessEqual(
                    string_display_width(render_status_line(state, width)), width
                )


# ---------------------------------------------------------------------------
# 按键解码
# ---------------------------------------------------------------------------


class DecodeKeyTests(unittest.TestCase):
    def test_ansi_arrows(self) -> None:
        cases = {
            "\x1b[A": KEY_UP,
            "\x1b[B": KEY_DOWN,
            "\x1b[C": KEY_RIGHT,
            "\x1b[D": KEY_LEFT,
            "\x1bOA": KEY_UP,
            "\x1b[5~": KEY_PAGE_UP,
            "\x1b[6~": KEY_PAGE_DOWN,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(decode_key(raw), expected)

    def test_control_characters(self) -> None:
        self.assertEqual(decode_key("\r"), KEY_ENTER)
        self.assertEqual(decode_key("\x7f"), KEY_BACKSPACE)
        self.assertEqual(decode_key("\x03"), KEY_CTRL_C)
        self.assertEqual(decode_key("\x04"), KEY_CTRL_D)

    def test_unknown_escape_degrades_to_escape(self) -> None:
        """未知序列当作 Esc，绝不能把乱码插进输入行。"""
        self.assertEqual(decode_key("\x1b[99;99R"), KEY_ESCAPE)

    def test_printable_passes_through(self) -> None:
        self.assertEqual(decode_key("a"), "a")
        self.assertEqual(decode_key("中"), "中")


# ---------------------------------------------------------------------------
# 按键处理
# ---------------------------------------------------------------------------


class InputEditingTests(unittest.TestCase):
    def test_typing_and_backspace(self) -> None:
        state = AppState()
        for char in "abc":
            handle_key(state, char, SIZE)
        self.assertEqual(state.input, "abc")
        handle_key(state, KEY_BACKSPACE, SIZE)
        self.assertEqual(state.input, "ab")

    def test_insertion_at_cursor(self) -> None:
        state = AppState()
        state.set_input("ac")
        state.cursor = 1
        handle_key(state, "b", SIZE)
        self.assertEqual(state.input, "abc")

    def test_home_and_end(self) -> None:
        state = AppState()
        state.set_input("abc")
        handle_key(state, KEY_HOME, SIZE)
        self.assertEqual(state.cursor, 0)
        handle_key(state, KEY_END, SIZE)
        self.assertEqual(state.cursor, 3)

    def test_tab_completes_command(self) -> None:
        state = AppState()
        state.set_input("/he")
        handle_key(state, KEY_TAB, SIZE)
        self.assertEqual(state.input, "/help")

    def test_enter_submits_and_clears(self) -> None:
        state = AppState()
        state.set_input("你好")
        outcome = handle_key(state, KEY_ENTER, SIZE)
        self.assertEqual(outcome.submit, "你好")
        self.assertEqual(state.input, "")
        self.assertEqual(state.history, ["你好"])

    def test_empty_enter_does_nothing(self) -> None:
        state = AppState()
        self.assertIsNone(handle_key(state, KEY_ENTER, SIZE).submit)

    def test_ctrl_c_clears_and_signals(self) -> None:
        state = AppState()
        state.set_input("半截")
        outcome = handle_key(state, KEY_CTRL_C, SIZE)
        self.assertTrue(outcome.interrupt)
        self.assertEqual(state.input, "")

    def test_ctrl_d_quits_only_when_empty(self) -> None:
        state = AppState()
        self.assertTrue(handle_key(state, KEY_CTRL_D, SIZE).quit)
        state.set_input("x")
        self.assertFalse(handle_key(state, KEY_CTRL_D, SIZE).quit)

    def test_escape_clears_input(self) -> None:
        state = AppState()
        state.set_input("/he")
        handle_key(state, KEY_ESCAPE, SIZE)
        self.assertEqual(state.input, "")


class HistoryTests(unittest.TestCase):
    def test_up_and_down(self) -> None:
        state = AppState()
        state.remember("一")
        state.remember("二")
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "二")
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "一")
        handle_key(state, KEY_DOWN, SIZE)
        self.assertEqual(state.input, "二")

    def test_down_restores_draft(self) -> None:
        state = AppState()
        state.remember("旧命令")
        state.set_input("正在输入")
        handle_key(state, KEY_UP, SIZE)
        handle_key(state, KEY_DOWN, SIZE)
        self.assertEqual(state.input, "正在输入")


class ScrollTests(unittest.TestCase):
    def _long(self) -> AppState:
        state = AppState()
        for index in range(100):
            state.push("assistant", f"第 {index} 行")
        compose_region(state, 80, 24)  # 回填 last_transcript_lines
        return state

    def test_page_up_then_down(self) -> None:
        state = self._long()
        handle_key(state, KEY_PAGE_UP, SIZE)
        self.assertGreater(state.scroll_offset, 0)
        handle_key(state, KEY_PAGE_DOWN, SIZE)
        self.assertEqual(state.scroll_offset, 0)

    def test_scroll_is_bounded(self) -> None:
        state = self._long()
        for _ in range(50):
            handle_key(state, KEY_PAGE_UP, SIZE)
        self.assertLessEqual(state.scroll_offset, state.last_transcript_lines)

    def test_new_content_does_not_yank_the_view(self) -> None:
        """用户在翻历史时，新输出不该把他拽回底部。"""
        state = self._long()
        handle_key(state, KEY_PAGE_UP, SIZE)
        offset = state.scroll_offset
        state.push("assistant", "新内容")
        compose_region(state, 80, 24)
        self.assertEqual(state.scroll_offset, offset)

    def test_end_jumps_to_bottom(self) -> None:
        state = self._long()
        handle_key(state, KEY_PAGE_UP, SIZE)
        handle_key(state, KEY_END, SIZE)
        self.assertEqual(state.scroll_offset, 0)


class AppStateTests(unittest.TestCase):
    def test_update_running_tool_finalizes(self) -> None:
        state = AppState()
        state.push("tool", "read_file", ok=None)
        self.assertTrue(state.update_running_tool("read_file", True, "完成"))
        self.assertIs(state.transcript[-1].ok, True)
        self.assertEqual(state.transcript[-1].detail, "完成")

    def test_transcript_is_bounded(self) -> None:
        state = AppState()
        for index in range(2200):
            state.push("assistant", f"行{index}")
        self.assertLessEqual(len(state.transcript), 2000)

    def test_slash_matches(self) -> None:
        state = AppState()
        state.set_input("/")
        self.assertGreater(len(state.slash_matches()), 5)
        state.set_input("不是命令")
        self.assertEqual(state.slash_matches(), [])


# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------


class KeySplittingTests(unittest.TestCase):
    """把原始输入切成逻辑键 —— 读取线程的核心。

    这类逻辑最容易出错的地方是**跨块到达的序列**：读取线程逐字符读，
    因此 `\\x1b[A`（方向键）会分三次到达，必须缓冲到凑齐才能判定。
    """

    def _terminal(self) -> Terminal:
        return Terminal(io.StringIO(), force_non_interactive=True, ansi=True)

    def test_single_characters(self) -> None:
        keys, rest = self._terminal()._split_keys("abc")  # noqa: SLF001
        self.assertEqual(keys, ["a", "b", "c"])
        self.assertEqual(rest, "")

    def test_windows_scan_codes(self) -> None:
        terminal = self._terminal()
        for raw, expected in (
            ("\x00H", KEY_UP),
            ("\x00P", KEY_DOWN),
            ("\xe0K", KEY_LEFT),
            ("\xe0M", KEY_RIGHT),
            ("\x00I", KEY_PAGE_UP),
        ):
            with self.subTest(raw=raw):
                keys, rest = terminal._split_keys(raw)  # noqa: SLF001
                self.assertEqual(keys, [expected])
                self.assertEqual(rest, "")

    def test_ansi_sequences(self) -> None:
        terminal = self._terminal()
        for raw, expected in (
            ("\x1b[A", KEY_UP),
            ("\x1b[B", KEY_DOWN),
            ("\x1b[5~", KEY_PAGE_UP),
            ("\x1b[H", KEY_HOME),
        ):
            with self.subTest(raw=raw):
                keys, rest = terminal._split_keys(raw)  # noqa: SLF001
                self.assertEqual(keys, [expected])
                self.assertEqual(rest, "")

    def test_incomplete_escape_sequence_waits(self) -> None:
        """**关键**：半个转义序列必须留在缓冲里等后续字节。

        不缓冲的话，`\\x1b` 会被立刻当成 Esc，方向键就失效了。
        """
        terminal = self._terminal()
        keys, rest = terminal._split_keys("\x1b")  # noqa: SLF001
        self.assertEqual(keys, [], "不应立刻判定为 Esc")
        self.assertEqual(rest, "\x1b", "应留在缓冲里")

        keys, rest = terminal._split_keys(rest + "[A")  # noqa: SLF001
        self.assertEqual(keys, [KEY_UP])
        self.assertEqual(rest, "")

    def test_incomplete_scan_code_waits(self) -> None:
        terminal = self._terminal()
        keys, rest = terminal._split_keys("\x00")  # noqa: SLF001
        self.assertEqual(keys, [])
        self.assertEqual(rest, "\x00")

        keys, rest = terminal._split_keys(rest + "H")  # noqa: SLF001
        self.assertEqual(keys, [KEY_UP])

    def test_control_characters(self) -> None:
        terminal = self._terminal()
        keys, _ = terminal._split_keys("\r\x7f\x03\x04")  # noqa: SLF001
        self.assertEqual(keys, [KEY_ENTER, KEY_BACKSPACE, KEY_CTRL_C, KEY_CTRL_D])

    def test_mixed_sequence(self) -> None:
        terminal = self._terminal()
        keys, rest = terminal._split_keys("a\x00Hb")  # noqa: SLF001
        self.assertEqual(keys, ["a", KEY_UP, "b"])
        self.assertEqual(rest, "")


class ReaderThreadTests(unittest.TestCase):
    """读取线程的端到端行为（用可控的假 stdin 驱动）。"""

    class FakeStdin:
        def __init__(self, data: str, *, eof_after: bool = True) -> None:
            self._data = list(data)
            self._index = 0
            self._eof_after = eof_after

        def read(self, _n: int = 1) -> str:
            if self._index >= len(self._data):
                time.sleep(0.02)
                return "" if self._eof_after else ""
            char = self._data[self._index]
            self._index += 1
            time.sleep(0.005)
            return char

        def fileno(self) -> int:
            raise io.UnsupportedOperation("no fileno")

        def isatty(self) -> bool:
            return True

    def _drain(self, terminal: Terminal, count: int, timeout: float = 3.0) -> list[str]:
        got: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(got) < count:
            key = terminal.read_key()
            if key is not None:
                got.append(key)
            time.sleep(0.005)
        return got

    def test_reads_mixed_input(self) -> None:
        """打字、扫描码方向键、ANSI 方向键、控制键混在一起都要正确。"""
        original = sys.stdin
        sys.stdin = self.FakeStdin("ab\x00H\x1b[B\r\x03")  # type: ignore[assignment]
        try:
            terminal = Terminal(io.StringIO(), ansi=False)
            terminal.interactive = True
            terminal._use_msvcrt = False  # noqa: SLF001 - 强制走通用路径
            got = self._drain(terminal, 6)
            terminal.stop_reader()
        finally:
            sys.stdin = original

        self.assertEqual(
            got, ["a", "b", KEY_UP, KEY_DOWN, KEY_ENTER, KEY_CTRL_C]
        )

    def test_lone_escape_eventually_fires(self) -> None:
        """单独的 `\\x1b` 必须**最终**产出 Esc，不能永远等下去。

        没有这个超时兜底，按 Esc 键永远没反应——因为读取线程无法区分
        「用户按了 Esc」和「方向键序列的开头」。
        """
        original = sys.stdin
        sys.stdin = self.FakeStdin("\x1b")  # type: ignore[assignment]
        try:
            terminal = Terminal(io.StringIO(), ansi=False)
            terminal.interactive = True
            terminal._use_msvcrt = False  # noqa: SLF001
            got = self._drain(terminal, 1, timeout=2.0)
            terminal.stop_reader()
        finally:
            sys.stdin = original

        self.assertEqual(got, [KEY_ESCAPE])

    def test_reader_stops_on_eof(self) -> None:
        original = sys.stdin
        sys.stdin = self.FakeStdin("a")  # type: ignore[assignment]
        try:
            terminal = Terminal(io.StringIO(), ansi=False)
            terminal.interactive = True
            terminal._use_msvcrt = False  # noqa: SLF001
            self._drain(terminal, 1)
            terminal.stop_reader()
            # 再读应当稳定返回 None，而不是抛异常
            for _ in range(5):
                self.assertIsNone(terminal.read_key())
        finally:
            sys.stdin = original


class RealConsoleDetectionTests(unittest.TestCase):
    """`is_real_windows_console` 与 `isatty` 是**不同**的判据。

    这个区别是本项目实际踩过的坑：Git Bash / MSYS2 下 `isatty()` 为真，
    但标准句柄不是 Windows 控制台，`msvcrt.kbhit()` 永远返回 False——
    界面画出来了，按键完全没反应。

    ## 为什么这些测试要构造环境而不是依赖当前环境

    我最初的版本直接断言 `_use_msvcrt` 的值，但**当前测试环境里
    `isatty()` 本来就是 False**，于是「用 isatty 判定」和「用真控制台判定」
    两种实现恰好给出相同结果——变异检查里那条变异因此存活。
    必须显式构造「isatty 为真但句柄不是控制台」这个组合才能区分。
    """

    def test_returns_false_off_windows(self) -> None:
        from minicode.tui.terminal import is_real_windows_console

        if os.name != "nt":
            self.assertFalse(is_real_windows_console(-10))

    def test_does_not_raise_on_any_platform(self) -> None:
        from minicode.tui.terminal import is_real_windows_console

        self.assertIn(is_real_windows_console(-10), (True, False))

    def test_msvcrt_requires_a_real_console_not_just_isatty(self) -> None:
        """**关键区分**：isatty 为真、句柄不是控制台时，不得启用 msvcrt。

        这正是 Git Bash 的情形。用 `isatty()` 判定的实现会在这里失败。
        """
        import minicode.tui.terminal as terminal_module

        class FakeStdin:
            def isatty(self) -> bool:
                return True  # Git Bash 下确实为真

            def read(self, _n: int = 1) -> str:
                return ""

        with patch.object(terminal_module.sys, "stdin", FakeStdin()):
            with patch.object(
                terminal_module, "is_real_windows_console", lambda _which=-10: False
            ):
                terminal = Terminal(io.StringIO(), ansi=False)
        self.assertFalse(
            terminal._use_msvcrt,  # noqa: SLF001
            "句柄不是真控制台时不得启用 msvcrt（否则 kbhit 永远返回 False）",
        )

    def test_msvcrt_enabled_when_console_is_real(self) -> None:
        """反过来：句柄确实是控制台时才启用 msvcrt 快路径。"""
        import minicode.tui.terminal as terminal_module

        if os.name != "nt":
            self.skipTest("仅 Windows 适用")
        with patch.object(
            terminal_module, "is_real_windows_console", lambda _which=-10: True
        ):
            terminal = Terminal(io.StringIO(), ansi=False)
        self.assertTrue(terminal._use_msvcrt)  # noqa: SLF001

    def test_diagnosis_flags_non_tty_stdin(self) -> None:
        """stdin 不是终端时必须给出**对应那条**诊断。

        断言必须落在具体措辞上：两种诊断消息都含 "stdin"，只断言这个词的话，
        即便「非终端」这条分支失效、落到下面 MSYS 分支，测试也照样通过
        ——变异检查抓到过这一点。
        """
        import minicode.tui.terminal as terminal_module

        class NotATty:
            def isatty(self) -> bool:
                return False

            def read(self, _n: int = 1) -> str:
                return ""

        with patch.object(terminal_module.sys, "stdin", NotATty()):
            terminal = Terminal(io.StringIO(), ansi=False)
            diagnosis = terminal.input_diagnosis()

        self.assertIsNotNone(diagnosis, "非终端 stdin 必须被检出")
        self.assertIn("不是终端", diagnosis or "", "应报「不是终端」这条")
        self.assertNotIn("Windows 控制台", diagnosis or "", "不该落到 MSYS 那条")

    def test_diagnosis_flags_msys_style_console(self) -> None:
        """Git Bash 风格（isatty 真、句柄假）必须给出提示。"""
        import minicode.tui.terminal as terminal_module

        if os.name != "nt":
            self.skipTest("仅 Windows 适用")

        class FakeStdin:
            def isatty(self) -> bool:
                return True

            def read(self, _n: int = 1) -> str:
                return ""

        with patch.object(terminal_module.sys, "stdin", FakeStdin()):
            with patch.object(
                terminal_module, "is_real_windows_console", lambda _which=-10: False
            ):
                terminal = Terminal(io.StringIO(), ansi=False)
                diagnosis = terminal.input_diagnosis()
        self.assertIsNotNone(diagnosis)
        self.assertIn("MSYS", diagnosis or "")

    def test_diagnosis_is_silent_when_injected_source_exists(self) -> None:
        """测试注入按键源时不该报诊断——那会干扰测试输出。"""
        terminal = Terminal(
            io.StringIO(),
            key_source=lambda: None,
            size_source=lambda: SIZE,
            ansi=False,
        )
        self.assertIsNone(terminal.input_diagnosis())

    def test_terminal_records_which_input_path_it_uses(self) -> None:
        terminal = Terminal(io.StringIO(), force_non_interactive=True, ansi=True)
        self.assertIsInstance(terminal._use_msvcrt, bool)  # noqa: SLF001


class InputDeadmanSwitchTests(unittest.TestCase):
    """输入通道可疑且迟迟无按键时，必须主动退回行模式。

    绝不能把用户留在一个不响应的界面里——那是最糟的失败方式：看起来正常，
    实际什么都不响应。
    """

    def test_suspect_input_falls_back_after_timeout(self) -> None:
        from minicode.tui.runner import run_tui

        class SuspectTerminal(ScriptedTerminal):
            """自检报告有问题、且永远读不到按键。"""

            def input_diagnosis(self) -> str | None:
                return "模拟：stdin 不是控制台"

        terminal = SuspectTerminal([])  # 没有任何按键
        terminal._keys = []
        with patch("minicode.tui.runner._INPUT_DEADMAN_SECONDS", 0.05):
            code = run_tui(AppState(), _bridge(), terminal=terminal)
        self.assertEqual(code, -1, "应返回 -1 让调用方回退到行模式")

    def test_healthy_input_does_not_fall_back(self) -> None:
        """自检正常时不该误触发保底。"""
        from minicode.tui.runner import run_tui

        terminal = ScriptedTerminal([KEY_CTRL_D])
        with patch("minicode.tui.runner._INPUT_DEADMAN_SECONDS", 0.05):
            code = run_tui(AppState(), _bridge(), terminal=terminal)
        self.assertEqual(code, 0, "正常通道应正常退出，而不是回退")

    def test_any_keypress_disarms_the_deadman(self) -> None:
        """只要收到过一个按键，就不该再触发保底。

        注意 `Ctrl-D` 只在**输入为空**时才退出（有内容时它不清空输入，
        见 `handle_key`）。因此这里用 `max_iterations` 给循环一个确定的
        退出路径——否则按键耗尽后 `read_key()` 永远返回 None，循环会一直跑
        （真实交互本该如此，但测试必须有出口）。
        """
        from minicode.tui.runner import run_tui

        class SuspectTerminal(ScriptedTerminal):
            def input_diagnosis(self) -> str | None:
                return "模拟：可疑"

        # 先打一个字符证明通道可用，然后大量空转（超过保底时间）
        terminal = SuspectTerminal([*_type("x")])
        with patch("minicode.tui.runner._INPUT_DEADMAN_SECONDS", 0.01):
            # 帧间隔也要 patch：否则 400 次迭代 × 30ms ≈ 12 秒，
            # 一条测试不该这么慢。
            with patch("minicode.tui.runner._FRAME_INTERVAL", 0.0):
                code = run_tui(
                    AppState(), _bridge(), terminal=terminal, max_iterations=400
                )
        # 收到过按键 → 不该走保底回退（那会返回 -1）；应跑到迭代上限返回 0
        self.assertEqual(code, 0, "收到过按键后不该回退到行模式")

    def test_deadman_only_fires_before_any_key(self) -> None:
        """保底只在「一个键都没收到」时生效——这是它的精确边界。"""
        from minicode.tui.runner import run_tui

        class SuspectTerminal(ScriptedTerminal):
            def input_diagnosis(self) -> str | None:
                return "模拟：可疑"

        # 没有任何按键：保底应当触发
        silent = SuspectTerminal([])
        with patch("minicode.tui.runner._INPUT_DEADMAN_SECONDS", 0.02):
            self.assertEqual(
                run_tui(AppState(), _bridge(), terminal=silent), -1
            )

        # 有按键：不触发
        talking = SuspectTerminal([*_type("a")])
        with patch("minicode.tui.runner._INPUT_DEADMAN_SECONDS", 0.02):
            with patch("minicode.tui.runner._FRAME_INTERVAL", 0.0):
                self.assertEqual(
                    run_tui(
                        AppState(), _bridge(), terminal=talking, max_iterations=200
                    ),
                    0,
                )

    def test_diagnosis_is_shown_to_the_user(self) -> None:
        """诊断文本必须真的出现在界面上——否则等于没说。"""
        from minicode.tui.runner import run_tui

        class SuspectTerminal(ScriptedTerminal):
            def input_diagnosis(self) -> str | None:
                return "这是诊断文本"

        terminal = SuspectTerminal([KEY_CTRL_D])
        run_tui(AppState(), _bridge(), terminal=terminal)
        self.assertIn("这是诊断文本", _rendered(terminal))


class TerminalTests(unittest.TestCase):
    def test_non_interactive_is_safe(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=True)
        terminal.enter()
        terminal.write("内容")
        terminal.leave()
        self.assertFalse(terminal.interactive)

    def test_non_interactive_read_key_returns_none(self) -> None:
        terminal = Terminal(io.StringIO(), force_non_interactive=True)
        self.assertIsNone(terminal.read_key())

    def test_ansi_disabled_falls_back_to_plain_text(self) -> None:
        """不支持 ANSI 时绝不打印转义序列——那看起来就是界面彻底坏了。

        **必须渲染两帧。** 首帧的光标恰好已在目标行，因此不产生任何转义
        序列——只断言首帧的话，即便降级分支失效也照样通过。变异检查抓到过
        这一点。第二帧必然涉及光标上移，才真正区分得开。
        """
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=False)
        terminal.render_region(["a", "b"], cursor_row=1)
        terminal.render_region(["a", "B"], cursor_row=1)  # 第二帧必然要重定位
        output = stream.getvalue()
        self.assertNotIn("\x1b", output, "降级模式不该输出任何转义序列")
        self.assertIn("a", output)
        self.assertIn("B", output)

    def test_ansi_disabled_prints_every_frame_in_full(self) -> None:
        """降级模式下每帧都整块重印，因此内容一定可见。

        内联重绘依赖 ANSI；没有 ANSI 就只能整块打印。这条保证「至少能用」。
        """
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=False)
        terminal.render_region(["第一帧"])
        terminal.render_region(["第二帧"])
        output = stream.getvalue()
        self.assertIn("第一帧", output)
        self.assertIn("第二帧", output)

    def test_ansi_enabled_emits_escape_sequences(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=True)
        terminal.enter()
        terminal.render_region(["a", "b"], cursor_row=0)
        self.assertIn("\x1b", stream.getvalue())

    def test_render_region_uses_live_region_diffing(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=True)
        terminal.render_region(["a", "b"])
        stream.seek(0)
        stream.truncate(0)
        terminal.render_region(["a", "b"])
        self.assertEqual(stream.getvalue(), "", "内容没变就不该写任何东西")

    def test_leave_restores_cursor(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True, ansi=True)
        terminal.enter()
        terminal.leave()
        self.assertIn("\x1b[?25h", stream.getvalue(), "必须恢复光标")

    def test_leave_is_safe_without_enter(self) -> None:
        terminal = Terminal(io.StringIO(), force_non_interactive=True, ansi=True)
        terminal.leave()  # 不应抛

    def test_broken_stream_does_not_raise(self) -> None:
        class Broken(io.StringIO):
            def write(self, _text):  # type: ignore[override]
                raise OSError("管道已关闭")

        terminal = Terminal(Broken(), force_non_interactive=True, ansi=True)
        terminal.write("x")  # 不应抛
        self.assertFalse(terminal.interactive, "失败后应自动降级")

    def test_injected_key_and_size_sources(self) -> None:
        keys = iter(["a", None])
        terminal = Terminal(
            io.StringIO(),
            key_source=lambda: next(keys, None),
            size_source=lambda: TerminalSize(columns=123, rows=45),
        )
        terminal.interactive = True
        self.assertEqual(terminal.read_key(), "a")
        self.assertIsNone(terminal.read_key())
        self.assertEqual(terminal.size().columns, 123)


# ---------------------------------------------------------------------------
# 交互循环
# ---------------------------------------------------------------------------


class ScriptedTerminal(Terminal):
    """按键由脚本决定、尺寸固定的终端，用来驱动 `run_tui`。"""

    def __init__(self, keys: list[str | None]) -> None:
        self.stream = io.StringIO()
        self._keys = list(keys)
        self.renders: list[list[str]] = []
        self.cursors: list[tuple[int, int]] = []
        super().__init__(
            self.stream,
            key_source=self._next_key,
            size_source=lambda: SIZE,
            ansi=True,
        )
        self.interactive = True

    def _next_key(self) -> str | None:
        return self._keys.pop(0) if self._keys else None

    def render_region(
        self,
        lines: list[str],
        *,
        cursor_row: int = 0,
        cursor_column: int = 1,
    ) -> None:
        self.renders.append(list(lines))
        self.cursors.append((cursor_row, cursor_column))
        super().render_region(
            lines, cursor_row=cursor_row, cursor_column=cursor_column
        )


def _bridge(**overrides: object):
    from minicode.tui.runner import HostBridge

    defaults: dict[str, object] = {
        "run_turn": lambda text: None,
        "run_local": lambda text: None,
        "is_busy": lambda: False,
        "summary": lambda: BannerStats(model="test"),
    }
    defaults.update(overrides)
    return HostBridge(**defaults)  # type: ignore[arg-type]


def _type(text: str) -> list[str]:
    return list(text)


def _rendered(terminal: "ScriptedTerminal") -> str:
    return strip_ansi("\n".join("\n".join(frame) for frame in terminal.renders))


class LoopLifecycleTests(unittest.TestCase):
    def test_ctrl_d_quits(self) -> None:
        from minicode.tui.runner import run_tui

        terminal = ScriptedTerminal([KEY_CTRL_D])
        self.assertEqual(run_tui(AppState(), _bridge(), terminal=terminal), 0)
        self.assertTrue(terminal.renders, "至少应渲染过一帧")

    def test_non_interactive_returns_minus_one(self) -> None:
        """非 TTY 时返回 -1，让调用方回退到行模式。"""
        from minicode.tui.runner import run_tui

        terminal = Terminal(io.StringIO(), force_non_interactive=True)
        self.assertEqual(run_tui(AppState(), _bridge(), terminal=terminal), -1)

    def test_typing_then_enter_submits(self) -> None:
        from minicode.tui.runner import run_tui

        submitted: list[str] = []
        done = threading.Event()

        def on_turn(text: str) -> None:
            submitted.append(text)
            done.set()

        terminal = ScriptedTerminal([*_type("你好"), KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), _bridge(run_turn=on_turn), terminal=terminal)
        done.wait(5)
        self.assertEqual(submitted, ["你好"])

    def test_submitted_text_appears_in_a_render(self) -> None:
        from minicode.tui.runner import run_tui

        done = threading.Event()
        terminal = ScriptedTerminal([*_type("检查代码"), KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), _bridge(run_turn=lambda _t: done.set()), terminal=terminal)
        done.wait(5)
        self.assertIn("检查代码", _rendered(terminal))

    def test_backspace_edits_before_submitting(self) -> None:
        from minicode.tui.runner import run_tui

        submitted: list[str] = []
        done = threading.Event()
        terminal = ScriptedTerminal(
            [*_type("abc"), KEY_BACKSPACE, KEY_ENTER, KEY_CTRL_D]
        )
        run_tui(
            AppState(),
            _bridge(run_turn=lambda t: (submitted.append(t), done.set())),
            terminal=terminal,
        )
        done.wait(5)
        self.assertEqual(submitted, ["ab"])

    def test_ctrl_c_clears_without_submitting(self) -> None:
        from minicode.tui.runner import run_tui

        submitted: list[str] = []
        done = threading.Event()
        terminal = ScriptedTerminal(
            [*_type("丢弃"), KEY_CTRL_C, *_type("保留"), KEY_ENTER, KEY_CTRL_D]
        )
        run_tui(
            AppState(),
            _bridge(run_turn=lambda t: (submitted.append(t), done.set())),
            terminal=terminal,
        )
        done.wait(5)
        self.assertEqual(submitted, ["保留"])

    def test_bare_enter_does_not_submit(self) -> None:
        from minicode.tui.runner import run_tui

        submitted: list[str] = []
        terminal = ScriptedTerminal([KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), _bridge(run_turn=submitted.append), terminal=terminal)
        self.assertEqual(submitted, [])

    def test_terminal_is_restored_on_exit(self) -> None:
        from minicode.tui.runner import run_tui

        terminal = ScriptedTerminal([KEY_CTRL_D])
        calls: list[str] = []
        original = terminal.leave

        def leave() -> None:
            calls.append("leave")
            original()

        terminal.leave = leave  # type: ignore[method-assign]
        run_tui(AppState(), _bridge(), terminal=terminal)
        self.assertIn("leave", calls)

    def test_rendered_region_never_exceeds_the_terminal(self) -> None:
        """交互循环产出的每一帧都必须满足宽度与高度约束。"""
        from minicode.tui.runner import run_tui

        terminal = ScriptedTerminal(
            [*_type("很长的一段中文输入内容用来测试折行" * 3), KEY_ENTER, KEY_CTRL_D]
        )
        run_tui(AppState(), _bridge(), terminal=terminal)
        for frame in terminal.renders:
            self.assertLessEqual(len(frame), SIZE.rows - 1)
            for line in frame:
                self.assertLessEqual(string_display_width(line), SIZE.columns)


class LocalCommandTests(unittest.TestCase):
    def test_slash_command_goes_local_not_to_model(self) -> None:
        from minicode.tui.runner import run_tui

        local: list[str] = []
        turns: list[str] = []
        seen = threading.Event()

        def on_local(text: str) -> str | None:
            local.append(text)
            seen.set()
            return "本地结果"

        terminal = ScriptedTerminal([*_type("/status"), KEY_ENTER, KEY_CTRL_D])
        run_tui(
            AppState(),
            _bridge(run_turn=turns.append, run_local=on_local),
            terminal=terminal,
        )
        seen.wait(5)
        self.assertEqual(local, ["/status"])
        self.assertEqual(turns, [], "本地命令不该触发模型回合")

    def test_unknown_slash_command_is_not_sent_to_model(self) -> None:
        """未知命令必须明确报错并停止——与行模式行为一致。"""
        from minicode.tui.runner import run_tui

        turns: list[str] = []
        terminal = ScriptedTerminal([*_type("/nope"), KEY_ENTER, KEY_CTRL_D])
        run_tui(AppState(), _bridge(run_turn=turns.append), terminal=terminal)
        self.assertEqual(turns, [], "未知斜杠命令不该发给模型")
        self.assertIn("未知命令", _rendered(terminal))


class EventPipelineTests(unittest.TestCase):
    def test_callbacks_map_to_event_kinds(self) -> None:
        from minicode.tui.runner import UiEvents, make_terminal_events

        events = UiEvents()
        callbacks = make_terminal_events(events)
        callbacks["on_assistant"]("回答", True)
        callbacks["on_progress"]("进行中")
        callbacks["on_tool_start"]("read_file", {})
        callbacks["on_tool_result"]("read_file", "ok", False)
        callbacks["on_notice"]("提示")

        items, finished = events.drain()
        self.assertEqual(
            [item[0] for item in items],
            ["assistant", "progress", "tool_start", "tool_result", "notice"],
        )
        self.assertFalse(finished)
        # on_tool_result 的模型是 (name, output, is_error)，False = 成功
        self.assertIs(items[3][2], True)

    def test_drain_empties_the_queue(self) -> None:
        from minicode.tui.runner import UiEvents

        events = UiEvents()
        events.push("assistant", "一次性")
        first, _ = events.drain()
        second, _ = events.drain()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_finished_flag_reports_once(self) -> None:
        from minicode.tui.runner import UiEvents

        events = UiEvents()
        events.mark_finished()
        _items, finished = events.drain()
        self.assertTrue(finished)
        _items, again = events.drain()
        self.assertFalse(again)


class ExceptionSafetyTests(unittest.TestCase):
    def test_turn_exception_is_shown_not_crashed(self) -> None:
        from minicode.tui.runner import run_tui

        def boom(_text: str) -> None:
            raise RuntimeError("模型炸了")

        terminal = ScriptedTerminal(
            [*_type("触发"), KEY_ENTER, None, None, None, KEY_CTRL_D]
        )
        code = run_tui(AppState(), _bridge(run_turn=boom), terminal=terminal)
        self.assertEqual(code, 0)
        self.assertIn("模型炸了", _rendered(terminal))

    def test_terminal_is_restored_even_when_the_loop_raises(self) -> None:
        """循环异常也必须恢复终端——留在没有光标的终端里是最糟的失败方式。"""
        from minicode.tui.runner import run_tui

        terminal = ScriptedTerminal([KEY_CTRL_D])
        calls: list[str] = []
        original = terminal.leave

        def leave() -> None:
            calls.append("leave")
            original()

        terminal.leave = leave  # type: ignore[method-assign]

        def boom(_text: str) -> None:
            raise RuntimeError("模型炸了")

        run_tui(AppState(), _bridge(run_turn=boom), terminal=terminal)
        self.assertIn("leave", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
