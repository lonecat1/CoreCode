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
import time
from dataclasses import dataclass
from typing import Any, Callable

import ctypes

from .frames import (
    ENTER_ALT_SCREEN_BUNDLE,
    EXIT_ALT_SCREEN_BUNDLE,
    HIDE_CURSOR,
    SHOW_CURSOR,
    FrameState,
    cursor_to,
    diff_frames,
)

#: 无阻塞读取时，等待转义序列后续字节的最长时间（秒）。
#: 方向键会连续送 2–5 个字节，但它们几乎同时到达；20ms 足够。
_ESCAPE_SEQUENCE_GRACE = 0.02

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

#: Windows 扫描码 → 逻辑键。
#:
#: Windows 控制台的方向键**不是** ANSI 序列，而是两字节：前缀 `\x00` 或 `\xe0`
#: 加一个扫描码。原 TS 实现依赖 Node 的按键解析（它已经做了这层转换），
#: 所以这部分在 TS 里不显眼，但移植到 Python 必须自己处理。
WINDOWS_SCAN_CODES: dict[str, str] = {
    "H": KEY_UP,
    "P": KEY_DOWN,
    "K": KEY_LEFT,
    "M": KEY_RIGHT,
    "I": KEY_PAGE_UP,
    "Q": KEY_PAGE_DOWN,
    "G": KEY_HOME,
    "O": KEY_END,
    "R": "insert",
    "S": "delete",
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
    """读取终端尺寸。

    不能用 `os.get_terminal_size()` 作为唯一手段：在 Windows 上当 stdout
    不是控制台句柄时它会抛 `OSError [WinError 6] 句柄无效`（本实现实测过）。
    因此按可靠性从高到低依次尝试：

      1. `shutil.get_terminal_size()`（内部会查 COLUMNS/LINES 环境变量，
         并在 POSIX 上问 ioctl）
      2. Windows 控制台 API
      3. 环境变量
      4. 兜底默认值
    """
    try:
        import shutil

        size = shutil.get_terminal_size((default_columns, default_rows))
        # shutil 在完全查不到时会返回传入的 fallback，视为不可靠。
        if size.columns > 0 and size.lines > 0 and (size.columns, size.lines) != (
            default_columns,
            default_rows,
        ):
            return TerminalSize(columns=size.columns, rows=size.lines)
    except Exception:  # noqa: BLE001
        pass

    windows = _detect_size_windows()
    if windows is not None:
        return windows

    columns = int(os.environ.get("COLUMNS") or 0)
    rows = int(os.environ.get("LINES") or 0)
    return TerminalSize(
        columns=columns or default_columns, rows=rows or default_rows
    )


def _detect_size_windows() -> TerminalSize | None:
    """通过 Windows 控制台 API 取窗口尺寸。非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle in (0, -1):
            return None
        buffer_info = ctypes.create_string_buffer(22)
        if not kernel32.GetConsoleScreenBufferInfo(handle, buffer_info):
            return None
        # CONSOLE_SCREEN_BUFFER_INFO 的布局：
        #   偏移 0  COORD dwSize          (2 × int16)
        #   偏移 4  COORD dwCursorPosition(2 × int16)
        #   偏移 8  WORD  wAttributes
        #   偏移 10 SMALL_RECT srWindow   (4 × int16: left, top, right, bottom)
        import struct

        left, top, right, bottom = struct.unpack_from("<hhhh", buffer_info.raw, 10)
        columns = right - left + 1
        rows = bottom - top + 1
        if columns > 0 and rows > 0:
            return TerminalSize(columns=columns, rows=rows)
    except Exception:  # noqa: BLE001
        pass
    return None


def is_tty() -> bool:
    """stdin 与 stdout 都是终端时才算可交互。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Terminal:
    """一个终端会话。非 TTY 环境下所有写操作仍然安全（只是没有意义）。"""

    def __init__(
        self,
        stream: Any = None,
        *,
        force_non_interactive: bool = False,
        key_source: Callable[[], str | None] | None = None,
        size_source: Callable[[], TerminalSize] | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._state = FrameState()
        self._raw_restore: Any = None
        self._entered = False
        self._hidden_cursor = False
        self.interactive = (
            False if force_non_interactive else is_tty()
        )
        #: 可注入的按键源与尺寸源，仅用于测试。
        #:
        #: 存在的意义：真实的 `msvcrt` / `select` 路径在自动化环境里没法驱动，
        #: 于是「按键处理是否正确」就永远得不到验证。把这两个来源变成可替换的，
        #: 就能用脚本按键真正跑完整个交互循环——包括渲染、翻页、提交。
        #: 生产代码不传它们，走的仍是平台原生实现。
        self._key_source = key_source
        self._size_source = size_source

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

    # -- 尺寸 ---------------------------------------------------------------

    def size(self) -> TerminalSize:
        """当前终端尺寸（走可注入的尺寸源，便于测试）。"""
        if self._size_source is not None:
            return self._size_source()
        return detect_size()

    # -- 输出 ---------------------------------------------------------------

    def write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            # 管道关闭（例如 `| head`）不该让程序崩溃。
            self.interactive = False

    def render(
        self,
        frame: str,
        size: TerminalSize | None = None,
        cursor: tuple[int, int] | None = None,
    ) -> bool:
        """渲染一帧。返回是否真的写出了内容。

        `cursor` 是 1 基的 `(行, 列)`，用于把光标移到输入位置——用户在
        输入行打字，光标必须在那儿，否则看不见自己在哪。
        """
        measured = size or (
            self._size_source() if self._size_source is not None else detect_size()
        )
        result = diff_frames(
            frame,
            self._state,
            columns=measured.columns,
            rows=measured.rows,
            cursor=cursor,
        )
        if result.output or result.trailing:
            self.write(result.output + result.trailing)
        return not result.is_noop

    def move_cursor(self, row: int, column: int = 1) -> None:
        self.write(cursor_to(row, column))

    def reset_frame(self) -> None:
        """丢弃上一帧记忆，下次渲染全量重绘。"""
        self._state.reset()

    # -- 输入 ---------------------------------------------------------------

    def read_key(self) -> str | None:
        """非阻塞地读一个逻辑键。

        **没有按键时立即返回 None**，不阻塞——调用方（`runner.run_tui`）靠这个
        在等待输入期间继续重绘动画与合并后台事件。

        ## 为什么不能用 `sys.stdin.read(1)`

        控制台输入是**行缓冲**的：`sys.stdin.read(1)` 会一直阻塞到用户按回车，
        按键根本送不进来，整个 TUI 交互是死的。这是本实现最初的写法，在
        Windows 上实测无法使用。正确做法是平台原生的「有键就读、没键就走」：

          - Windows：`msvcrt.kbhit()` + `msvcrt.getwch()`（无需回车）
          - POSIX：原始模式下 `select()` + `os.read()`（无需回车）

        两条路径都必须先把 stdin 置为原始模式；POSIX 上原始模式是**必需**的
        （否则规范模式的行缓冲会让 `os.read` 同样等到回车）。
        """
        if not self.interactive:
            return None
        if self._key_source is not None:
            return self._key_source()
        if os.name == "nt":
            return self._read_key_windows()
        return self._read_key_posix()

    # -- Windows ------------------------------------------------------------

    def _read_key_windows(self) -> str | None:
        try:
            import msvcrt
        except ImportError:
            return None

        try:
            if not msvcrt.kbhit():
                return None
            char = msvcrt.getwch()
        except (OSError, ValueError):
            return None
        if not char:
            return None

        # 扩展键（方向键、PgUp 等）以 NUL 或 0xE0 前缀到达，第二字节是扫描码。
        if char in ("\x00", "\xe0"):
            try:
                if not msvcrt.kbhit():
                    time.sleep(_ESCAPE_SEQUENCE_GRACE)
                if not msvcrt.kbhit():
                    return KEY_ESCAPE
                scan = msvcrt.getwch()
            except (OSError, ValueError):
                return KEY_ESCAPE
            return WINDOWS_SCAN_CODES.get(scan, KEY_ESCAPE)

        return decode_key(char)

    # -- POSIX --------------------------------------------------------------

    def _read_key_posix(self) -> str | None:
        try:
            import select

            fd = sys.stdin.fileno()
        except (ImportError, OSError, ValueError):
            return None

        try:
            ready, _r, _e = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            return None
        if not ready:
            return None

        try:
            first = os.read(fd, 1)
        except OSError:
            return None
        if not first:
            return None

        char = first.decode("utf-8", errors="replace")

        # 转义序列：把紧随其后的字节一起收下来，交给 decode_key 识别。
        if char == "\x1b":
            buffer = char
            deadline = time.monotonic() + _ESCAPE_SEQUENCE_GRACE
            while time.monotonic() < deadline and len(buffer) < 8:
                try:
                    ready, _r, _e = select.select([fd], [], [], 0.002)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    nxt = os.read(fd, 1)
                except OSError:
                    break
                if not nxt:
                    break
                buffer += nxt.decode("utf-8", errors="replace")
                if buffer in ESCAPE_SEQUENCES:
                    break
                # 已经不可能再匹配到更长的已知序列时提前收工。
                if not any(seq.startswith(buffer) for seq in ESCAPE_SEQUENCES):
                    break
            return decode_key(buffer)

        return decode_key(char)

    # -- 原始模式 -----------------------------------------------------------

    def enable_raw_mode(self) -> bool:
        """进入原始模式。失败返回 False（调用方应降级到行模式）。

        POSIX 上这是**必需**的：不关掉行缓冲，`os.read` 会一直等到回车。
        Windows 上用 msvcrt 逐字符读取，原始模式只是顺带关掉回显，
        因此失败也不致命。
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
            # 用 cbreak 而不是 setraw：后者会关掉信号处理，
            # 于是 Ctrl-C 不再产生中断而变成普通字节。
            tty.setcbreak(fd)

            def restore() -> None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)

            self._raw_restore = restore
            return True
        except Exception:  # noqa: BLE001
            return False

    def _enable_raw_windows(self) -> bool:
        """关掉回显与行缓冲，让按键立刻可见。

        msvcrt.getwch() 本身就不需要回车，所以即使这里失败，输入仍然可用——
        只是用户敲的字会同时被控制台回显一次，看起来有点重影。
        """
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            return False

        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            if handle in (0, -1):
                return False
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            original = mode.value
            # 关掉 ECHO(0x4) 与 LINE_INPUT(0x2)，保留其他位。
            new_mode = original & ~0x0004 & ~0x0002
            if not kernel32.SetConsoleMode(handle, new_mode):
                return False

            def restore() -> None:
                try:
                    kernel32.SetConsoleMode(handle, original)
                except Exception:  # noqa: BLE001
                    pass

            self._raw_restore = restore
            return True
        except Exception:  # noqa: BLE001
            return False
