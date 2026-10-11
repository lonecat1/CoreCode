"""TUI 的行为测试。

**差分帧渲染是本文件的重点**：它是 objective 点名要求的能力，也是唯一
「写错了肉眼很难发现」的部分——少画一行只是看起来旧了一点点，
多画一行只是慢一点点。

策略是把算法全部做成纯函数，于是可以精确断言「第二帧输出了哪些字节」。
真正接触终端的那一小层（`terminal.py`）刻意不做逻辑，未测面积被压到最小。
"""

from __future__ import annotations

import io
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.tui.app import (  # noqa: E402
    AppState,
    BannerStats,
    compose_frame,
    compose_transcript_lines,
    compute_layout,
    handle_key,
)
from minicode.tui.frames import (  # noqa: E402
    ERASE_LINE,
    HOME,
    FrameState,
    clear_screen_sequence,
    cursor_to,
    diff_frames,
)
from minicode.tui.panels import (  # noqa: E402
    PanelOptions,
    render_banner,
    render_context_badge,
    render_footer_bar,
    render_panel,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
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
    detect_size,
)
from minicode.tui.width import (  # noqa: E402
    char_display_width,
    pad_plain,
    string_display_width,
    strip_ansi,
    truncate_plain,
    wrap_plain,
)

unittest.TestCase.maxDiff = None

SIZE = TerminalSize(columns=80, rows=24)


# ---------------------------------------------------------------------------
# 显示宽度
# ---------------------------------------------------------------------------


class DisplayWidthTests(unittest.TestCase):
    """宽度算错的后果是边框错位，而且 ASCII 场景永远正确——必须专测宽字符。"""

    def test_ascii_is_one_column(self) -> None:
        self.assertEqual(string_display_width("hello"), 5)

    def test_combining_marks_are_zero_width(self) -> None:
        # e + 组合重音 = 视觉上仍是一个字符
        self.assertEqual(char_display_width("\u0301"), 0)
        self.assertEqual(string_display_width("e\u0301"), 1)

    def test_cjk_is_two_columns(self) -> None:
        self.assertEqual(string_display_width("中文"), 4)
        self.assertEqual(string_display_width("日本語"), 6)

    def test_hangul_is_two_columns(self) -> None:
        self.assertEqual(string_display_width("한글"), 4)

    def test_fullwidth_forms_are_two_columns(self) -> None:
        self.assertEqual(string_display_width("ＡＢ"), 4)

    def test_emoji_is_two_columns(self) -> None:
        self.assertEqual(string_display_width("😀"), 2)

    def test_cjk_exception_u303f_is_one_column(self) -> None:
        """U+303F 落在宽字符区间里，但宽度是 1 —— 原实现显式排除。"""
        self.assertEqual(char_display_width("\u303f"), 1)

    def test_ansi_is_not_counted(self) -> None:
        self.assertEqual(string_display_width("\x1b[31mred\x1b[0m"), 3)

    def test_strip_ansi_removes_colour(self) -> None:
        self.assertEqual(strip_ansi("\x1b[1;32mok\x1b[0m"), "ok")

    def test_width_uses_codepoints_not_utf16_units(self) -> None:
        """Emoji 在 UTF-16 里是 2 个 code unit，但显示宽度是 2 列——
        两者数值巧合相同，所以这里用一个宽度为 2 但 UTF-16 长度为 1 的字符
        （CJK）来区分：宽度 2、长度 1。"""
        self.assertEqual(string_display_width("中"), 2)
        self.assertEqual(len("中"), 1)


class TruncatePadTests(unittest.TestCase):
    def test_truncate_adds_ellipsis(self) -> None:
        self.assertEqual(truncate_plain("abcdefghij", 7), "abcd...")

    def test_truncate_respects_wide_characters(self) -> None:
        """按可见宽度截断：4 个中文=8 列，限 7 列只能放 2 个字符加省略号。"""
        result = truncate_plain("中文字符", 7)
        self.assertLessEqual(string_display_width(result), 7)
        self.assertTrue(result.endswith("..."))

    def test_truncate_short_enough_is_unchanged(self) -> None:
        self.assertEqual(truncate_plain("abc", 10), "abc")

    def test_truncate_zero_width(self) -> None:
        self.assertEqual(truncate_plain("abc", 0), "")

    def test_truncate_does_not_split_ansi_sequence(self) -> None:
        text = "\x1b[31m红色文字\x1b[0m"
        result = truncate_plain(text, 6)
        # 结果里不应出现被切断的转义（例如只有 "\x1b["）
        self.assertNotIn("\x1b[3", result.replace("\x1b[31m", ""))

    def test_pad_to_width(self) -> None:
        self.assertEqual(string_display_width(pad_plain("ab", 5)), 5)

    def test_pad_wide_characters(self) -> None:
        self.assertEqual(string_display_width(pad_plain("中文", 6)), 6)

    def test_pad_does_not_shrink(self) -> None:
        self.assertEqual(pad_plain("abcdef", 3), "abcdef")


class WrapTests(unittest.TestCase):
    def test_wraps_on_spaces(self) -> None:
        self.assertEqual(wrap_plain("one two three", 7), ["one two", "three"])

    def test_preserves_blank_lines(self) -> None:
        self.assertEqual(wrap_plain("a\n\nb", 10), ["a", "", "b"])

    def test_wraps_cjk_by_character(self) -> None:
        """中文没有空格，必须按字符宽度切。"""
        result = wrap_plain("中文字符测试", 4)
        self.assertEqual(result, ["中文", "字符", "测试"])

    def test_hard_splits_overlong_word(self) -> None:
        """没有空格可断时按宽度硬切，且每片都不超宽。"""
        result = wrap_plain("abcdefghij", 4)
        self.assertEqual(result, ["abcd", "efgh", "ij"])
        for line in result:
            self.assertLessEqual(string_display_width(line), 4)

    def test_hard_split_is_width_based_not_word_based(self) -> None:
        """`leading` 在 6 列下被切成 `leadin` + `g`。

        这是**正确**行为而非缺陷：一个 7 列的词在 6 列里放不下，
        又没有任何空格可断，只能按宽度切。断言它，是为了防止有人
        「顺手优化」成整词下移——那会让这一行被浪费掉。
        """
        self.assertEqual(wrap_plain("  leading", 6), ["leadin", "g"])

    def test_leading_spaces_do_not_produce_blank_lines(self) -> None:
        result = wrap_plain("     x", 4)
        self.assertNotIn("", result, "行首空格不该挤出空行")
        for line in result:
            self.assertLessEqual(string_display_width(line), 4)

    def test_every_line_fits(self) -> None:
        text = "中文混排 with some english words 和更多中文内容 here"
        for line in wrap_plain(text, 12):
            self.assertLessEqual(string_display_width(line), 12, f"超宽: {line!r}")


# ---------------------------------------------------------------------------
# 差分帧渲染 —— 核心算法
# ---------------------------------------------------------------------------


class DiffFramesTests(unittest.TestCase):
    def test_first_frame_is_full_render(self) -> None:
        state = FrameState()
        result = diff_frames("a\nb", state, columns=80, rows=24)
        self.assertTrue(result.full_render)
        self.assertEqual(result.output, HOME + "\x1b[J" + "a\nb")

    def test_identical_frame_produces_no_output(self) -> None:
        """**这是差分渲染的全部意义**：没变化就一个字节都不写。"""
        state = FrameState()
        diff_frames("a\nb\nc", state, columns=80, rows=24)
        result = diff_frames("a\nb\nc", state, columns=80, rows=24)
        self.assertTrue(result.is_noop)
        self.assertEqual(result.output, "")
        self.assertEqual(result.changed_lines, [])

    def test_only_changed_line_is_written(self) -> None:
        state = FrameState()
        diff_frames("a\nb\nc", state, columns=80, rows=24)
        result = diff_frames("a\nB\nc", state, columns=80, rows=24)
        self.assertFalse(result.full_render)
        self.assertEqual(result.changed_lines, [1])
        # 第 2 行：定位到 2;1 → 擦除整行 → 写入新内容。第 1、3 行完全不出现。
        self.assertEqual(result.output, f"{cursor_to(2)}{ERASE_LINE}B")
        self.assertNotIn("a", result.output)
        self.assertNotIn("c", result.output)

    def test_multiple_changed_lines_are_each_addressed(self) -> None:
        state = FrameState()
        diff_frames("a\nb\nc\nd", state, columns=80, rows=24)
        result = diff_frames("A\nb\nC\nd", state, columns=80, rows=24)
        self.assertEqual(result.changed_lines, [0, 2])
        self.assertIn(cursor_to(1), result.output)
        self.assertIn(cursor_to(3), result.output)
        self.assertNotIn(cursor_to(2), result.output)

    def test_growing_frame_renders_only_new_lines(self) -> None:
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        result = diff_frames("a\nb\nc", state, columns=80, rows=24)
        self.assertEqual(result.changed_lines, [2])

    def test_shrinking_frame_erases_leftover_lines(self) -> None:
        """帧变短时必须擦掉残影，否则旧内容会留在屏幕上。"""
        state = FrameState()
        diff_frames("a\nb\nc", state, columns=80, rows=24)
        result = diff_frames("a", state, columns=80, rows=24)
        self.assertEqual(result.changed_lines, [1, 2])
        # 空行也要发出擦除序列
        self.assertEqual(result.output.count(ERASE_LINE), 2)

    def test_column_change_forces_full_render(self) -> None:
        """列数变化会让所有折行位置失效，逐行差分没有意义。"""
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        result = diff_frames("a\nb", state, columns=100, rows=24)
        self.assertTrue(result.full_render)

    def test_row_change_forces_full_render(self) -> None:
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        result = diff_frames("a\nb", state, columns=80, rows=30)
        self.assertTrue(result.full_render)

    def test_after_full_render_diffing_resumes(self) -> None:
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        diff_frames("a\nb", state, columns=100, rows=24)  # 触发全量
        result = diff_frames("a\nB", state, columns=100, rows=24)
        self.assertFalse(result.full_render)
        self.assertEqual(result.changed_lines, [1])

    def test_trailing_blank_line_is_significant(self) -> None:
        state = FrameState()
        diff_frames("a", state, columns=80, rows=24)
        result = diff_frames("a\n", state, columns=80, rows=24)
        self.assertEqual(result.changed_lines, [1])

    def test_missing_line_is_not_the_same_as_an_empty_line(self) -> None:
        """**「这一行不存在」与「这一行是空行」必须是两种状态。**

        旧实现用 `previous[i] if i < len(previous) else ""` 取值，于是两者
        塌缩成同一个 `""`。后果：帧变短且新帧最后一行是空行时，该擦的行被
        判定为「没变化」而不擦，屏幕上留下残影。

        这条测试是变异检查逼出来的：把哨兵 `None` 换回 `""` 之后，
        原先的用例全都照样通过——说明它们没有覆盖这个区分。
        """
        state = FrameState()
        diff_frames("a\nb\nc", state, columns=80, rows=24)
        # 三行缩成「a + 一个空行」：长度从 3 变 2，但第 2 行在新帧里是空行，
        # 不是「不存在」。旧实现会认为第 2 行没变而不擦除，于是 "b" 留在屏上。
        result = diff_frames("a\n", state, columns=80, rows=24)
        self.assertIn(1, result.changed_lines, "第 2 行必须被擦除")
        self.assertIn(2, result.changed_lines, "第 3 行必须被擦除")
        self.assertEqual(result.output.count(ERASE_LINE), 2)

    def test_equal_length_frames_with_a_blank_tail_still_diff(self) -> None:
        """长度相同但内容不同：一个以内容结尾，一个以空行结尾。"""
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        result = diff_frames("a\n", state, columns=80, rows=24)
        self.assertEqual(result.changed_lines, [1])

    def test_state_tracks_previous_frame(self) -> None:
        state = FrameState()
        diff_frames("a\nb", state, columns=80, rows=24)
        self.assertEqual(state.lines, ["a", "b"])
        self.assertEqual(state.columns, 80)

    def test_reset_forces_full_render(self) -> None:
        state = FrameState()
        diff_frames("a", state, columns=80, rows=24)
        state.reset()
        result = diff_frames("a", state, columns=80, rows=24)
        self.assertTrue(result.full_render)

    def test_clear_screen_is_not_a_hard_clear(self) -> None:
        """用 `H`+`J` 而不是 `2J`：硬清屏会闪。"""
        sequence = clear_screen_sequence()
        self.assertEqual(sequence, "\x1b[H\x1b[J")
        self.assertNotIn("\x1b[2J", sequence)


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------


class PanelTests(unittest.TestCase):
    def test_panel_border_width_matches(self) -> None:
        for width in (60, 80, 100, 120):
            with self.subTest(width=width):
                rendered = render_panel("标题", "内容", width)
                for line in rendered.split("\n"):
                    self.assertEqual(
                        string_display_width(line), width, f"宽度不符: {line!r}"
                    )

    def test_panel_contains_title_and_body(self) -> None:
        rendered = strip_ansi(render_panel("我的标题", "正文内容", 80))
        self.assertIn("我的标题", rendered)
        self.assertIn("正文内容", rendered)

    def test_panel_pads_to_min_body_lines(self) -> None:
        """min_body_lines 数的是**正文行**，不含边框与标题。

        布局：上边框 1 + 标题 1 + 标题后空行 1 + 正文 N + 下边框 1。
        """
        rendered = render_panel("t", "一行", 80, PanelOptions(min_body_lines=4))
        self.assertEqual(len(rendered.split("\n")), 4 + 4)

    def test_panel_without_title_omits_title_rows(self) -> None:
        with_title = render_panel("t", "x", 80)
        without = render_panel("t", "x", 80, PanelOptions(show_title=False))
        self.assertLess(len(without.split("\n")), len(with_title.split("\n")))

    def test_panel_without_frame_returns_body_only(self) -> None:
        rendered = render_panel("t", "只有正文", 80, PanelOptions(frame=False))
        self.assertEqual(rendered, "只有正文")

    def test_long_body_is_wrapped_within_panel(self) -> None:
        body = "中文内容" * 40
        rendered = render_panel("t", body, 80)
        for line in rendered.split("\n"):
            self.assertEqual(string_display_width(line), 80)

    def test_right_title_is_shown(self) -> None:
        rendered = strip_ansi(render_panel("左", "b", 80, PanelOptions(right_title="右侧")))
        self.assertIn("右侧", rendered)


class BadgeTests(unittest.TestCase):
    def _stats(self, utilization: float, level: str, source: str = "provider_usage"):
        class Accounting:
            provider_usage_tokens = 100

        class Stats:
            pass

        stats = Stats()
        stats.utilization = utilization
        stats.warning_level = level
        stats.accounting = Accounting()
        stats.accounting.source = source
        return stats

    def test_badge_shows_percentage(self) -> None:
        rendered = strip_ansi(render_context_badge(self._stats(0.62, "normal")))
        self.assertIn("62%", rendered)
        self.assertIn("[ctx]", rendered)

    def test_badge_bar_reflects_utilization(self) -> None:
        rendered = strip_ansi(render_context_badge(self._stats(0.50, "normal")))
        self.assertIn("▓" * 5 + "░" * 5, rendered)

    def test_badge_clamps_over_100_percent(self) -> None:
        """计量误差可能让利用率略微超过 1，进度条不能超出 10 格。"""
        rendered = strip_ansi(render_context_badge(self._stats(1.4, "blocked")))
        self.assertNotIn("▓" * 11, rendered)

    def test_badge_source_labels(self) -> None:
        cases = {
            "provider_usage": "usage",
            "provider_usage_plus_estimate": "usage+est",
            "estimate_only": "est",
        }
        for source, label in cases.items():
            with self.subTest(source=source):
                rendered = strip_ansi(render_context_badge(self._stats(0.3, "normal", source)))
                self.assertIn(label, rendered)

    def test_badge_colour_follows_warning_level(self) -> None:
        """等级必须改变颜色——否则徽章就只是个数字，失去了警示作用。"""
        from minicode.tui.panels import GREEN, RED, YELLOW

        self.assertIn(GREEN, render_context_badge(self._stats(0.1, "normal")))
        self.assertIn(YELLOW, render_context_badge(self._stats(0.6, "warning")))
        self.assertIn(RED, render_context_badge(self._stats(0.9, "critical")))


class BannerTests(unittest.TestCase):
    def test_banner_lines_match_width(self) -> None:
        stats = BannerStats(model="m", session_id="s", cwd="/tmp", message_count=3)
        for line in render_banner(stats, 80).split("\n"):
            self.assertEqual(string_display_width(line), 80)

    def test_banner_shows_model_and_session(self) -> None:
        stats = BannerStats(model="claude-sonnet-4", session_id="abc123")
        rendered = strip_ansi(render_banner(stats, 100))
        self.assertIn("claude-sonnet-4", rendered)
        self.assertIn("abc123", rendered)

    def test_banner_marks_mcp_errors(self) -> None:
        stats = BannerStats(mcp_total=2, mcp_connected=1, mcp_error=1)
        rendered = strip_ansi(render_banner(stats, 100))
        self.assertIn("1 err", rendered)

    def test_banner_marks_mcp_connecting(self) -> None:
        stats = BannerStats(mcp_total=2, mcp_connected=0, mcp_connecting=2)
        self.assertIn("connecting", strip_ansi(render_banner(stats, 100)))

    def test_banner_omits_mcp_when_none(self) -> None:
        self.assertNotIn("mcp", strip_ansi(render_banner(BannerStats(), 100)))

    def test_long_cwd_is_truncated(self) -> None:
        stats = BannerStats(cwd="/very/" + "long/" * 40)
        for line in render_banner(stats, 80).split("\n"):
            self.assertEqual(string_display_width(line), 80)


class ChromeTests(unittest.TestCase):
    def test_tool_panel_running_has_no_verdict(self) -> None:
        rendered = render_tool_panel("read_file", "a.py", ok=None, width=80)
        self.assertNotIn("✓", rendered)
        self.assertNotIn("✗", rendered)

    def test_tool_panel_ok_and_failure_markers(self) -> None:
        self.assertIn("✓", render_tool_panel("read_file", "x", ok=True, width=80))
        self.assertIn("✗", render_tool_panel("read_file", "x", ok=False, width=80))

    def test_status_line_without_status(self) -> None:
        self.assertIn("ready", strip_ansi(render_status_line(None)))

    def test_status_line_has_spinner(self) -> None:
        rendered = render_status_line("thinking", 0)
        self.assertNotEqual(rendered, render_status_line("thinking", 1))

    def test_footer_fills_width(self) -> None:
        self.assertEqual(string_display_width(render_footer_bar("hi", 60)), 60)

    def test_footer_marks_scrolling(self) -> None:
        self.assertIn("滚动中", render_footer_bar("hi", 60, scrolling=True))

    def test_slash_menu_lists_matches(self) -> None:
        rendered = strip_ansi(render_slash_menu([("/help", "帮助"), ("/status", "状态")], 80))
        self.assertIn("/help", rendered)
        self.assertIn("/status", rendered)

    def test_slash_menu_empty(self) -> None:
        self.assertEqual(render_slash_menu([], 80), "")


# ---------------------------------------------------------------------------
# 按键解码
# ---------------------------------------------------------------------------


class DecodeKeyTests(unittest.TestCase):
    def test_arrow_sequences(self) -> None:
        cases = {
            "\x1b[A": KEY_UP,
            "\x1b[B": KEY_DOWN,
            "\x1b[C": KEY_RIGHT,
            "\x1b[D": KEY_LEFT,
            "\x1bOA": KEY_UP,  # 应用模式
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

    def test_lone_escape(self) -> None:
        self.assertEqual(decode_key("\x1b"), KEY_ESCAPE)

    def test_unknown_escape_degrades_to_escape(self) -> None:
        """未知序列当作 Esc，绝不能把乱码插进输入行。"""
        self.assertEqual(decode_key("\x1b[99;99R"), KEY_ESCAPE)

    def test_printable_passes_through(self) -> None:
        self.assertEqual(decode_key("a"), "a")
        self.assertEqual(decode_key("中"), "中")

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(decode_key(""))


# ---------------------------------------------------------------------------
# 应用状态与按键处理
# ---------------------------------------------------------------------------


class InputEditingTests(unittest.TestCase):
    def test_typing_inserts_at_cursor(self) -> None:
        state = AppState()
        for char in "abc":
            handle_key(state, char, SIZE)
        self.assertEqual(state.input, "abc")
        self.assertEqual(state.cursor, 3)

    def test_backspace_removes_before_cursor(self) -> None:
        state = AppState()
        state.set_input("abc")
        state.cursor = 2
        handle_key(state, KEY_BACKSPACE, SIZE)
        self.assertEqual(state.input, "ac")
        self.assertEqual(state.cursor, 1)

    def test_backspace_at_start_is_noop(self) -> None:
        state = AppState()
        state.set_input("abc")
        state.cursor = 0
        handle_key(state, KEY_BACKSPACE, SIZE)
        self.assertEqual(state.input, "abc")

    def test_cursor_movement(self) -> None:
        state = AppState()
        state.set_input("abc")
        handle_key(state, KEY_LEFT, SIZE)
        self.assertEqual(state.cursor, 2)
        handle_key(state, KEY_HOME, SIZE)
        self.assertEqual(state.cursor, 0)
        handle_key(state, KEY_END, SIZE)
        self.assertEqual(state.cursor, 3)
        handle_key(state, KEY_RIGHT, SIZE)
        self.assertEqual(state.cursor, 3, "不应越过末尾")

    def test_insertion_in_the_middle(self) -> None:
        state = AppState()
        state.set_input("ac")
        state.cursor = 1
        handle_key(state, "b", SIZE)
        self.assertEqual(state.input, "abc")

    def test_tab_completes_unique_command(self) -> None:
        state = AppState()
        state.set_input("/he")
        handle_key(state, KEY_TAB, SIZE)
        self.assertEqual(state.input, "/help")

    def test_tab_completes_common_prefix(self) -> None:
        state = AppState()
        state.set_input("/re")
        handle_key(state, KEY_TAB, SIZE)
        # /resume 与 /rename
        self.assertEqual(state.input, "/re")


class HistoryTests(unittest.TestCase):
    def test_enter_remembers_and_clears(self) -> None:
        state = AppState()
        state.set_input("第一条")
        outcome = handle_key(state, KEY_ENTER, SIZE)
        self.assertEqual(outcome.submit, "第一条")
        self.assertEqual(state.input, "")
        self.assertEqual(state.history, ["第一条"])

    def test_empty_enter_does_nothing(self) -> None:
        state = AppState()
        outcome = handle_key(state, KEY_ENTER, SIZE)
        self.assertIsNone(outcome.submit)
        self.assertEqual(state.history, [])

    def test_up_recalls_previous(self) -> None:
        state = AppState()
        state.remember("一")
        state.remember("二")
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "二")
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "一")

    def test_up_at_oldest_stays(self) -> None:
        state = AppState()
        state.remember("唯一")
        handle_key(state, KEY_UP, SIZE)
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "唯一")

    def test_down_returns_to_draft(self) -> None:
        """向下翻到底应回到「还没提交的草稿」，而不是空。"""
        state = AppState()
        state.remember("旧命令")
        state.set_input("正在输入")
        handle_key(state, KEY_UP, SIZE)
        self.assertEqual(state.input, "旧命令")
        handle_key(state, KEY_DOWN, SIZE)
        self.assertEqual(state.input, "正在输入")

    def test_duplicate_consecutive_not_stored_twice(self) -> None:
        state = AppState()
        state.remember("一样")
        state.remember("一样")
        self.assertEqual(state.history, ["一样"])

    def test_history_is_bounded(self) -> None:
        state = AppState()
        for index in range(250):
            state.remember(f"cmd{index}")
        self.assertLessEqual(len(state.history), 200)
        self.assertEqual(state.history[-1], "cmd249")


class ControlKeyTests(unittest.TestCase):
    def test_ctrl_c_clears_input_and_signals(self) -> None:
        state = AppState()
        state.set_input("写了半截")
        outcome = handle_key(state, KEY_CTRL_C, SIZE)
        self.assertTrue(outcome.interrupt)
        self.assertEqual(state.input, "")

    def test_ctrl_d_quits_only_when_empty(self) -> None:
        state = AppState()
        self.assertTrue(handle_key(state, KEY_CTRL_D, SIZE).quit)
        state.set_input("有内容")
        self.assertFalse(handle_key(state, KEY_CTRL_D, SIZE).quit)

    def test_escape_clears_input(self) -> None:
        state = AppState()
        state.set_input("/he")
        handle_key(state, KEY_ESCAPE, SIZE)
        self.assertEqual(state.input, "")


class ScrollTests(unittest.TestCase):
    def _long_state(self, lines: int = 100) -> AppState:
        state = AppState()
        for index in range(lines):
            state.push("assistant", f"第 {index} 行")
        return state

    def test_page_up_scrolls_back(self) -> None:
        state = self._long_state()
        compose_frame(state, SIZE)  # 回填 last_total_lines
        handle_key(state, KEY_PAGE_UP, SIZE)
        self.assertGreater(state.scroll_offset, 0)

    def test_page_down_returns_toward_bottom(self) -> None:
        state = self._long_state()
        compose_frame(state, SIZE)
        handle_key(state, KEY_PAGE_UP, SIZE)
        before = state.scroll_offset
        handle_key(state, KEY_PAGE_DOWN, SIZE)
        self.assertLess(state.scroll_offset, before)

    def test_scroll_never_exceeds_content(self) -> None:
        state = AppState()
        state.push("assistant", "只有一行")
        compose_frame(state, SIZE)
        for _ in range(10):
            handle_key(state, KEY_PAGE_UP, SIZE)
        self.assertEqual(state.scroll_offset, 0)

    def test_scroll_never_negative(self) -> None:
        state = self._long_state()
        compose_frame(state, SIZE)
        for _ in range(10):
            handle_key(state, KEY_PAGE_DOWN, SIZE)
        self.assertEqual(state.scroll_offset, 0)

    def test_new_content_does_not_yank_the_view(self) -> None:
        """用户在翻历史时，新输出不该把他拽回底部。"""
        state = self._long_state()
        compose_frame(state, SIZE)
        handle_key(state, KEY_PAGE_UP, SIZE)
        offset = state.scroll_offset
        state.push("assistant", "新内容")
        compose_frame(state, SIZE)
        self.assertEqual(state.scroll_offset, offset, "偏移应保持")

    def test_end_jumps_to_bottom(self) -> None:
        state = self._long_state()
        compose_frame(state, SIZE)
        handle_key(state, KEY_PAGE_UP, SIZE)
        handle_key(state, KEY_END, SIZE)
        self.assertEqual(state.scroll_offset, 0)


# ---------------------------------------------------------------------------
# 整帧组装
# ---------------------------------------------------------------------------


class ComposeFrameTests(unittest.TestCase):
    def test_layout_reserves_room_for_chrome(self) -> None:
        layout = compute_layout(TerminalSize(columns=80, rows=24))
        self.assertLess(layout.transcript_lines, 24)
        self.assertGreaterEqual(layout.transcript_lines, 3)

    def test_tiny_terminal_does_not_produce_negative_viewport(self) -> None:
        layout = compute_layout(TerminalSize(columns=20, rows=4))
        self.assertGreaterEqual(layout.transcript_lines, 3)

    def test_frame_has_no_overwide_lines(self) -> None:
        """任何一行都不能超过终端宽度，否则会折行、把布局挤乱。"""
        state = AppState(
            banner=BannerStats(
                model="claude-sonnet-4",
                session_id="abc",
                cwd="D:\\CoreCode\\minicode-py",
                message_count=99,
                transcript_count=42,
                skill_count=5,
                mcp_total=2,
                mcp_connected=1,
                mcp_error=1,
            )
        )
        state.push("user", "中文很长的用户输入" * 8)
        state.push("assistant", "回答内容" * 30)
        state.push("tool", "grep_files", ok=True)

        for columns in (60, 80, 100, 140):
            with self.subTest(columns=columns):
                frame = compose_frame(state, TerminalSize(columns=columns, rows=30))
                for line in frame.split("\n"):
                    self.assertLessEqual(
                        string_display_width(line),
                        columns,
                        f"{columns} 列下超宽: {line!r}",
                    )

    def test_input_row_is_anchored_above_status(self) -> None:
        state = AppState(input="正在输入")
        state.set_input("正在输入")
        frame = compose_frame(state, SIZE)
        lines = frame.split("\n")
        self.assertIn("正在输入", strip_ansi(lines[-3]))
        self.assertIn("ready", strip_ansi(lines[-2]))

    def test_input_stays_anchored_when_transcript_is_short(self) -> None:
        """内容很少时输入行也必须贴在底部，而不是浮在屏幕中间。"""
        short = AppState()
        short.push("assistant", "一行")
        frame = compose_frame(short, SIZE)
        lines = frame.split("\n")
        self.assertIn("›", strip_ansi(lines[-3]))

    def test_empty_transcript_shows_a_hint(self) -> None:
        frame = strip_ansi(compose_frame(AppState(), SIZE))
        self.assertIn("还没有内容", frame)

    def test_scrolled_view_shows_older_content(self) -> None:
        state = AppState()
        for index in range(100):
            state.push("assistant", f"标记{index}")
        compose_frame(state, SIZE)
        state.scroll_offset = 50
        frame = strip_ansi(compose_frame(state, SIZE))
        self.assertNotIn("标记99", frame, "上翻时不该显示最新内容")
        self.assertIn("已上翻", frame, "页脚应提示当前处于滚动状态")

    def test_slash_menu_appears_while_typing_command(self) -> None:
        state = AppState()
        state.set_input("/st")
        frame = strip_ansi(compose_frame(state, SIZE))
        self.assertIn("/status", frame)

    def test_slash_menu_hidden_after_space(self) -> None:
        state = AppState()
        state.set_input("/goal 做点事")
        frame = strip_ansi(compose_frame(state, SIZE))
        self.assertNotIn("创建/控制 Goal", frame)

    def test_mcp_waiting_shows_in_status(self) -> None:
        frame = strip_ansi(compose_frame(AppState(), SIZE, mcp_waiting=True))
        self.assertIn("MCP 连接中", frame)


class AppStateTests(unittest.TestCase):
    def test_update_running_tool_finalizes(self) -> None:
        state = AppState()
        state.push("tool", "read_file", ok=None)
        self.assertTrue(state.update_running_tool("read_file 完成", ok=True))
        self.assertIs(state.transcript[-1].ok, True)
        self.assertEqual(state.transcript[-1].text, "read_file 完成")

    def test_update_running_tool_without_target_returns_false(self) -> None:
        state = AppState()
        state.push("assistant", "没有运行中的工具")
        self.assertFalse(state.update_running_tool("x", ok=True))

    def test_transcript_is_bounded(self) -> None:
        state = AppState()
        for index in range(2200):
            state.push("assistant", f"行{index}")
        self.assertLessEqual(len(state.transcript), 2000)

    def test_slash_matches_only_at_start(self) -> None:
        state = AppState()
        state.set_input("/")
        self.assertGreater(len(state.slash_matches()), 5)
        state.set_input("不是命令")
        self.assertEqual(state.slash_matches(), [])


# ---------------------------------------------------------------------------
# 终端层（薄层，但仍要保证不 TTy 时安全）
# ---------------------------------------------------------------------------


class TerminalTests(unittest.TestCase):
    def test_non_interactive_writes_are_safe(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True)
        terminal.enter()
        terminal.write("内容")
        terminal.leave()
        self.assertEqual(stream.getvalue(), "内容")
        self.assertFalse(terminal.interactive)

    def test_non_interactive_enter_is_noop(self) -> None:
        """没有 TTY 时绝不能发出交替屏幕序列——那会把管道输出弄脏。"""
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True)
        terminal.enter()
        self.assertNotIn("\x1b[?1049h", stream.getvalue())

    def test_non_interactive_read_key_returns_none(self) -> None:
        terminal = Terminal(io.StringIO(), force_non_interactive=True)
        self.assertIsNone(terminal.read_key())

    def test_render_writes_and_tracks_state(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True)
        self.assertTrue(terminal.render("a\nb", SIZE))
        first = stream.getvalue()
        self.assertIn("a\nb", first)
        # 相同帧 → 不再写任何东西
        self.assertFalse(terminal.render("a\nb", SIZE))

    def test_render_only_rewrites_changed_line(self) -> None:
        stream = io.StringIO()
        terminal = Terminal(stream, force_non_interactive=True)
        terminal.render("a\nb\nc", SIZE)
        stream.seek(0)
        stream.truncate(0)
        terminal.render("a\nB\nc", SIZE)
        self.assertEqual(stream.getvalue(), f"{cursor_to(2)}{ERASE_LINE}B")

    def test_leave_is_safe_without_enter(self) -> None:
        terminal = Terminal(io.StringIO(), force_non_interactive=True)
        terminal.leave()  # 不应抛

    def test_detect_size_has_sane_defaults(self) -> None:
        size = detect_size()
        self.assertGreater(size.columns, 0)
        self.assertGreater(size.rows, 0)

    def test_broken_stream_does_not_raise(self) -> None:
        class Broken(io.StringIO):
            def write(self, _text):  # type: ignore[override]
                raise OSError("管道已关闭")

        terminal = Terminal(Broken(), force_non_interactive=True)
        terminal.write("x")  # 不应抛
        self.assertFalse(terminal.interactive, "失败后应自动降级")


if __name__ == "__main__":
    unittest.main(verbosity=2)
