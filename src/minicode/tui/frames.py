"""差分帧渲染 —— 只重绘变化的行。

## 为什么这很重要

每次交互都清屏重画会让长对话疯狂闪烁，滚动位置也会丢。原实现的做法是：
保留上一帧的**每一行**，新帧逐行比对，只对不同的行发出「定位光标 → 擦除该行
→ 写入该行」的序列。没变的行一个字节都不写。

好处是闪烁与写入量都只与实际变化量成正比，而不是与屏幕大小成正比。

## 什么会强制全量重绘

  - 没有上一帧（首次渲染）；
  - **终端尺寸变了**：行数与列数都算在内。列数变化会让换行位置全变，
    行数变化会让滚动区域错位，此时逐行差分没有意义。

## 纯函数

`diff_frames()` 不碰终端。它接收「上一帧状态 + 新帧文本 + 尺寸」，
返回要写入的字节串与新的状态。这样这个算法可以被完整测试——
而不是「写了但没法验证」。终端层只剩「把字节写到 stdout」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ANSI 序列常量（与原实现逐字节一致）
# ---------------------------------------------------------------------------

ENTER_ALT_SCREEN = "\x1b[?1049h"
EXIT_ALT_SCREEN = "\x1b[?1049l"
ERASE_SCREEN_AND_HOME = "\x1b[2J\x1b[H"
HOME = "\x1b[H"
ERASE_TO_END = "\x1b[J"
ERASE_LINE = "\x1b[2K"

ENABLE_MOUSE_TRACKING = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
DISABLE_MOUSE_TRACKING = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

#: 交替屏幕的进入/退出组合，避免调用方漏掉某一步。
ENTER_ALT_SCREEN_BUNDLE = (
    DISABLE_MOUSE_TRACKING + ENTER_ALT_SCREEN + ERASE_SCREEN_AND_HOME + ENABLE_MOUSE_TRACKING
)
EXIT_ALT_SCREEN_BUNDLE = DISABLE_MOUSE_TRACKING + EXIT_ALT_SCREEN


@dataclass
class FrameState:
    """渲染器需要记住的上一帧。"""

    lines: list[str] | None = None
    columns: int = 0
    rows: int = 0

    def reset(self) -> None:
        self.lines = None
        self.columns = 0
        self.rows = 0


def cursor_to(row: int, column: int = 1) -> str:
    """1 基的行列定位序列。"""
    return f"\x1b[{row};{column}H"


@dataclass
class FrameDiff:
    """一次渲染的产物。"""

    output: str
    full_render: bool
    changed_lines: list[int] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.output


def diff_frames(
    frame: str,
    state: FrameState,
    *,
    columns: int,
    rows: int,
) -> FrameDiff:
    """算出渲染这一帧需要写出的内容，并更新 `state`。

    `state` 会被就地修改（它代表「屏幕上现在是什么」）。
    """
    lines = frame.split("\n")

    force_full = (
        state.lines is None or state.columns != columns or state.rows != rows
    )

    if force_full:
        output = HOME + ERASE_TO_END + frame
        state.lines = lines
        state.columns = columns
        state.rows = rows
        return FrameDiff(output=output, full_render=True, changed_lines=list(range(len(lines))))

    previous = state.lines or []
    max_lines = max(len(previous), len(lines))
    parts: list[str] = []
    changed: list[int] = []

    for index in range(max_lines):
        # 关键：**不存在**的行与**空的**行必须区分开。
        #
        # 之前写成 `previous[index] if index < len(previous) else ""`，
        # 于是「上一帧没有第 2 行」与「上一帧第 2 行是空行」变成同一个值，
        # 帧变长且新行是空行时会被判定为「没变化」而漏画。
        # 用哨兵 `None` 表示「这一行不存在」。
        next_exists = index < len(lines)
        prev_exists = index < len(previous)
        next_line = lines[index] if next_exists else None
        previous_line = previous[index] if prev_exists else None

        if next_exists == prev_exists and next_line == previous_line:
            continue

        parts.append(cursor_to(index + 1) + ERASE_LINE + (next_line or ""))
        changed.append(index)

    state.lines = lines
    state.columns = columns
    state.rows = rows

    return FrameDiff(output="".join(parts), full_render=False, changed_lines=changed)


def clear_screen_sequence() -> str:
    """轻量重绘：不清整屏，只回到左上并擦到末尾。

    与原实现一致——`ESC[2J` 会闪，`ESC[H ESC[J` 不会。
    """
    return HOME + ERASE_TO_END
