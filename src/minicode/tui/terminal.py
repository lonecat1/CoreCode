"""终端层 —— 读键、写字节、管理交替屏幕。

**刻意做得很薄**：真正的逻辑都在 `frames.py`（纯函数）与 `panels.py`（纯字符串）
里。这一层只负责三件事：

  1. 判断自己是否运行在真实 TTY 上；
  2. 把终端原始模式打开/关掉；
  3. 把字节写进 stdout、从 stdin 读键。

## 为什么这么薄

原始模式的终端交互在自动化环境里无法真实驱动：没有 TTY 时 `termios`/`msvcrt`
的 raw 模式要么不可用、要么行为不同。把「有逻辑的部分」全部推到纯函数层，
就能**真正测试差分渲染算法**，而这一层只剩几乎不含判断的转发——
未测试的代码面积被压到最小。

## 降级

没有 TTY 时 `interactive=False`，`read_key()` 返回 None，调用方应回退到行模式。
`cli.py` 正是这么做的，因此 `minicode` 在管道/CI 里仍然可用。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from .frames import (
    ENTER_ALT_SCREEN_BUNDLE,
    EXIT_ALT_SCREEN_BUNDLE,
    HIDE_CURSOR,
    SHOW_CURSOR,
    FrameState,
    cursor_to,
    diff_frames,
)

# ---------------------------------------------------------------------------
# 键
# ---------------------------------------------------------------------------

KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_ENTER = "enter"
KEY_ESCAPE = "escape"
KEY_BACKSPACE = "backspace"
KEY_TAB = "tab"
KEY_CTRL_C = "ctrl-c"
KEY_CTRL_D = "ctrl-d"
KEY_PAGE_UP = "page-up"
KEY_PAGE_DOWN = "page-down"
KEY_HOME = "home"
KEY_END = "end"

#: 常见的 ANSI 方向键序列 → 逻辑键。终端对方向键有不止一种编码。
ESCAPE_SEQUENCES: dict[str, str] = {
    "\x1b[A": KEY_UP,
    "\x1bOA": KEY_UP,  # 应用模式（部分终端）
    "\x1b[B": KEY_DOWN,
    "\x1bOB": KEY_DOWN,
    "\x1b[C": KEY_RIGHT,
    "\x1bOC": KEY_RIGHT,
    "\x1b[D": KEY_LEFT,
    "\x1bOD": KEY_LEFT,
    "\x1b[5~": KEY_PAGE_UP,
    "\x1b[6~": KEY_PAGE_DOWN,
    "\x1b[H": KEY_HOME,
    "\x1bOH": KEY_HOME,
    "\x1b[F": KEY_END,
    "\x1bOF": KEY_END,
    "\x1b[1~": KEY_HOME,
    "\x1b[4~": KEY_END,
}

#: 单字节控制字符 → 逻辑键。
CONTROL_KEYS: dict[str, str] = {
    "\r": KEY_ENTER,
    "\n": KEY_ENTER,
    "\x7f": KEY_BACKSPACE,
    "\x08": KEY_BACKSPACE,
    "\t": KEY_TAB,
    "\x03": KEY_CTRL_C,
    "\x04": KEY_CTRL_D,
}


def decode_key(raw: str) -> str | None:
    """把原始输入片段转成逻辑键。无法识别时返回原字符（可打印输入）。

    单独拆出来是为了能测：键位解码是终端交互里最容易出错、
    却又完全可以用纯函数验证的部分。
    """
    if not raw:
        return None
    if raw in ESCAPE_SEQUENCES:
        return ESCAPE_SEQUENCES[raw]
    if raw in CONTROL_KEYS:
        return CONTROL_KEYS[raw]
    if raw == "\x1b":
        return KEY_ESCAPE
    if raw.startswith("\x1b"):
        # 未知转义序列：当作 Esc，丢弃剩余部分，避免把乱码插进输入行。
        return KEY_ESCAPE
    return raw


# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------


@dataclass
class TerminalSize:
    columns: int
    rows: int


def detect_size(default_columns: int = 100, default_rows: int = 30) -> TerminalSize:
    """读取终端尺寸，失败时给一个合理的默认值。"""
    try:
        size = os.get_terminal_size()
        return TerminalSize(columns=size.columns, rows=size.lines)
    except OSError:
        pass
    columns = int(os.environ.get("COLUMNS") or 0)
    rows = int(os.environ.get("LINES") or 0)
    return TerminalSize(
        columns=columns or default_columns, rows=rows or default_rows
    )


def is_tty() -> bool:
    """stdin 与 stdout 都是终端时才算可交互。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Terminal:
    """一个终端会话。非 TTY 环境下所有写操作仍然安全（只是没有意义）。"""

    def __init__(self, stream: Any = None, *, force_non_interactive: bool = False) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._state = FrameState()
        self._raw_restore: Any = None
        self._entered = False
        self._hidden_cursor = False
        self.interactive = (
            False if force_non_interactive else is_tty()
        )

    # -- 生命周期 -----------------------------------------------------------

    def enter(self) -> None:
        """进入交替屏幕（仅交互模式）。"""
        if not self.interactive or self._entered:
            return
        self.write(ENTER_ALT_SCREEN_BUNDLE)
        self.hide_cursor()
        self._entered = True

    def leave(self) -> None:
        """退出交替屏幕并恢复终端状态。

        必须在 `finally` 里调用：没有它，用户会在一个没有光标的
        交替屏幕里迷路——这是 TUI 最糟糕的失败方式。
        """
        if self._hidden_cursor:
            self.show_cursor()
        if self._entered:
            self.write(EXIT_ALT_SCREEN_BUNDLE)
            self._entered = False
        self._state.reset()

    def hide_cursor(self) -> None:
        if self.interactive:
            self.write(HIDE_CURSOR)
            self._hidden_cursor = True

    def show_cursor(self) -> None:
        if self.interactive:
            self.write(SHOW_CURSOR)
            self._hidden_cursor = False

    # -- 输出 ---------------------------------------------------------------

    def write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            # 管道关闭（例如 `| head`）不该让程序崩溃。
            self.interactive = False

    def render(self, frame: str, size: TerminalSize | None = None) -> bool:
        """渲染一帧。返回是否真的写出了内容。"""
        measured = size or detect_size()
        result = diff_frames(
            frame,
            self._state,
            columns=measured.columns,
            rows=measured.rows,
        )
        if result.output:
            self.write(result.output)
        return not result.is_noop

    def move_cursor(self, row: int, column: int = 1) -> None:
        self.write(cursor_to(row, column))

    def reset_frame(self) -> None:
        """丢弃上一帧记忆，下次渲染全量重绘。"""
        self._state.reset()

    # -- 输入 ---------------------------------------------------------------

    def read_key(self) -> str | None:
        """读一个逻辑键。非交互环境下返回 None。"""
        if not self.interactive:
            return None
        try:
            char = sys.stdin.read(1)
        except (OSError, ValueError):
            return None
        if not char:
            return None
        return decode_key(char)

    # -- 原始模式（可选） ---------------------------------------------------

    def enable_raw_mode(self) -> bool:
        """尝试进入原始模式。失败返回 False（调用方应降级）。

        Windows 与 POSIX 的路径完全不同，因此这里用 try 包住两种实现，
        而不是用 `if os.name` 分支 + 各自的错误处理。
        """
        if not self.interactive:
            return False
        if self._raw_restore is not None:
            return True

        if os.name == "nt":
            return self._enable_raw_windows()
        return self._enable_raw_posix()

    def disable_raw_mode(self) -> None:
        restore = self._raw_restore
        self._raw_restore = None
        if restore is None:
            return
        try:
            restore()
        except Exception:  # noqa: BLE001 - 恢复失败不该掩盖真实错误
            pass

    def _enable_raw_posix(self) -> bool:
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setraw(fd)

            def restore() -> None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)

            self._raw_restore = restore
            return True
        except Exception:  # noqa: BLE001
            return False

    def _enable_raw_windows(self) -> bool:
        try:
            import msvcrt  # type: ignore[import-not-found]
        except ImportError:
            return False

        # msvcrt 支持逐字符读取，但把回显关掉需要改控制台模式；
        # 在受限环境里这一步可能失败，失败就当作「没有原始模式」。
        try:
            self._raw_restore = lambda: None
            return hasattr(msvcrt, "getwch")
        except Exception:  # noqa: BLE001
            return False
