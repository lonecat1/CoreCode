"""终端层 —— 内联实时区域 + 平台原生取键。

## 三个必须做对的地方

**① 不用备用屏幕。**
见 `screen.py` 的说明：绝对定位要求「帧高恰好等于终端高」，一旦不等就崩。
内联模式只在自占的几行里原地重绘，不需要知道终端总行数。

**② Windows 的 HANDLE 必须声明为指针宽度。**
`GetStdHandle` 返回 `HANDLE`（指针大小），而 `ctypes` 默认 `restype` 是
`c_int`（32 位）。64 位 Windows 上这会把句柄**截断**，后续 `GetConsoleMode`
/ `SetConsoleMode` 作用在错误的句柄上并静默失败——表现为「原始模式怎么都
开不起来」。

**③ Windows 控制台要先启用 VT 处理。**
Windows 10 起支持 ANSI 转义序列，但**默认关闭**，需要
`SetConsoleMode(handle, ENABLE_VIRTUAL_TERMINAL_PROCESSING)`。
不开的话所有转义序列都会以 `←[38;5;209m` 这类形式**原样打印**到屏幕上
——这正是「TUI 看起来完全坏掉」最经典的原因。

无法启用 VT 时降级为纯文本输出（不发任何转义序列），而不是打印乱码。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from .screen import (
    HIDE_CURSOR,
    SHOW_CURSOR,
    LiveRegion,
    cursor_column,
)

#: 等待转义序列后续字节的最长时间（秒）。方向键会连发 2–5 字节。
_ESCAPE_GRACE = 0.02

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

#: ANSI 转义序列 → 逻辑键（POSIX 与现代 Windows 终端）。
ESCAPE_SEQUENCES: dict[str, str] = {
    "\x1b[A": KEY_UP,
    "\x1bOA": KEY_UP,
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
    "\x1b[1~": KEY_HOME,
    "\x1b[F": KEY_END,
    "\x1bOF": KEY_END,
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
#: Windows 控制台的方向键**不是** ANSI 序列，而是两字节：前缀 `\x00` 或
#: `\xe0` 加一个扫描码。原 TS 实现依赖 Node 的按键解析（它已做了这层转换），
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
}


def decode_key(raw: str) -> str | None:
    """把原始输入片段转成逻辑键。无法识别时返回原字符（可打印输入）。"""
    if not raw:
        return None
    if raw in ESCAPE_SEQUENCES:
        return ESCAPE_SEQUENCES[raw]
    if raw in CONTROL_KEYS:
        return CONTROL_KEYS[raw]
    if raw == "\x1b":
        return KEY_ESCAPE
    if raw.startswith("\x1b"):
        # 未知转义序列当作 Esc，丢弃剩余部分——绝不能把乱码插进输入行。
        return KEY_ESCAPE
    return raw


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------


@dataclass
class TerminalSize:
    columns: int
    rows: int


def _windows_console_size() -> TerminalSize | None:
    """通过 Windows 控制台 API 取窗口尺寸。"""
    if os.name != "nt":
        return None
    try:
        import struct

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle:
            return None
        info = ctypes.create_string_buffer(22)
        if not kernel32.GetConsoleScreenBufferInfo(ctypes.c_void_p(handle), info):
            return None
        left, top, right, bottom = struct.unpack_from("<hhhh", info.raw, 10)
        columns, rows = right - left + 1, bottom - top + 1
        if columns > 0 and rows > 0:
            return TerminalSize(columns=columns, rows=rows)
    except Exception:  # noqa: BLE001
        pass
    return None


def detect_size(default_columns: int = 100, default_rows: int = 30) -> TerminalSize:
    """读取终端尺寸，多级回退。

    不能只用 `os.get_terminal_size()`：在 Windows 上当 stdout 不是控制台句柄
    时它会抛 `OSError [WinError 6] 句柄无效`（本实现实测过）。
    """
    env_columns = int(os.environ.get("COLUMNS") or 0)
    env_rows = int(os.environ.get("LINES") or 0)
    if env_columns > 0 and env_rows > 0:
        return TerminalSize(columns=env_columns, rows=env_rows)

    windows = _windows_console_size()
    if windows is not None:
        return windows

    try:
        size = os.get_terminal_size()
        if size.columns > 0 and size.lines > 0:
            return TerminalSize(columns=size.columns, rows=size.lines)
    except (OSError, ValueError):
        pass

    try:
        import shutil

        fallback = shutil.get_terminal_size((default_columns, default_rows))
        if fallback.columns > 0 and fallback.lines > 0:
            return TerminalSize(columns=fallback.columns, rows=fallback.lines)
    except Exception:  # noqa: BLE001
        pass

    return TerminalSize(columns=default_columns, rows=default_rows)


def is_tty() -> bool:
    """stdin 与 stdout 都是终端时才算可交互。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# ANSI 能力
# ---------------------------------------------------------------------------

#: ENABLE_VIRTUAL_TERMINAL_PROCESSING
_VT_FLAG = 0x0004


def enable_windows_vt() -> bool:
    """在 Windows 控制台上启用 ANSI（VT）处理。返回是否可用。

    这是 Windows 上 TUI 能否工作的**关键一步**。Windows 10 起支持转义序列，
    但默认关闭；不开的话所有颜色与光标控制都会以 `←[38;5;209m` 这种形式
    原样打印出来——看起来就像界面彻底坏了。

    非 Windows 上恒为 True（终端本来就懂 ANSI）。
    """
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        # 必须声明指针宽度，否则 64 位下句柄被截断（见模块文档）。
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        if mode.value & _VT_FLAG:
            return True
        return bool(
            kernel32.SetConsoleMode(ctypes.c_void_p(handle), mode.value | _VT_FLAG)
        )
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------


class Terminal:
    """一个内联模式的终端会话。

    非 TTY 环境下 `interactive` 为假，所有写操作仍然安全（只是没有意义）。
    """

    def __init__(
        self,
        stream: Any = None,
        *,
        force_non_interactive: bool = False,
        key_source: Callable[[], str | None] | None = None,
        size_source: Callable[[], TerminalSize] | None = None,
        ansi: bool | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._region = LiveRegion()
        self._raw_restore: Any = None
        self._entered = False
        self._cursor_hidden = False
        self.interactive = False if force_non_interactive else is_tty()

        #: 可注入的按键源与尺寸源，仅用于测试。
        #:
        #: 真实的 `msvcrt` / `select` 路径在自动化环境里没法驱动，于是
        #: 「按键处理是否正确」就永远得不到验证。把它们变成可替换的，就能用
        #: 脚本按键真正跑完整个交互循环。生产代码不传，走的仍是原生实现。
        self._key_source = key_source
        self._size_source = size_source

        #: 是否可以使用 ANSI 转义序列。显式传入优先；否则探测。
        #: 探测失败时降级为纯文本，绝不打印乱码。
        self.ansi = enable_windows_vt() if ansi is None else ansi

    # -- 生命周期 -----------------------------------------------------------

    def enter(self) -> None:
        """准备实时区域。

        内联模式**不进入备用屏幕**，因此这里只隐藏光标并预留区域。
        隐藏光标是为了避免它在重绘过程中乱跳。
        """
        if self._entered:
            return
        self._entered = True
        if self.ansi:
            self.write(HIDE_CURSOR)
            self._cursor_hidden = True

    def leave(self) -> None:
        """结束实时区域，把终端恢复原状。

        必须在 `finally` 里调用：没有它用户会留在一个没有光标的终端里
        ——这是 TUI 最糟糕的失败方式。
        """
        if self._cursor_hidden:
            self.write(SHOW_CURSOR)
            self._cursor_hidden = False
        if self._entered and self.ansi and self._region.lines:
            # 换到干净的一行，让退出后的 shell 提示符不与界面粘连。
            self.write("\n")
        self._region.reset()
        self._entered = False

    def hide_cursor(self) -> None:
        if self.ansi:
            self.write(HIDE_CURSOR)
            self._cursor_hidden = True

    def show_cursor(self) -> None:
        if self.ansi:
            self.write(SHOW_CURSOR)
            self._cursor_hidden = False

    # -- 输出 ---------------------------------------------------------------

    def write(self, text: str) -> None:
        if not text:
            return
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            # 管道关闭（例如 `| head`）不该让程序崩溃。
            self.interactive = False

    def size(self) -> TerminalSize:
        if self._size_source is not None:
            return self._size_source()
        return detect_size()

    def render_region(
        self,
        lines: list[str],
        *,
        cursor_row: int = 0,
        cursor_column: int = 1,
    ) -> None:
        """把实时区域更新成 `lines`，并把光标放到指定位置。"""
        if not self.ansi:
            # 降级：整块重印，不做原地重绘。会累积，但至少可读、不错乱。
            self.write("\n".join(lines) + "\n")
            return

        output = self._region.render(lines, cursor_row=cursor_row)
        # 光标横向定位只在**确实有输出**时发。
        #
        # 否则「内容完全没变」的帧也会写出定位序列，破坏「无变化则零输出」
        # 这条性质——它既是「不闪」的基础，也是可测试性的基础。
        if output and cursor_column > 1:
            output += cursor_column_at(cursor_column)
        self.write(output)

    def reset_region(self) -> None:
        """丢弃上一帧记忆，下次渲染全量重画。"""
        self._region.reset()

    # -- 输入 ---------------------------------------------------------------

    def read_key(self) -> str | None:
        """非阻塞地读一个逻辑键。

        **没有按键时立即返回 None**，调用方靠它在等待输入期间继续重绘。

        ## 为什么不能用 `sys.stdin.read(1)`

        控制台输入是**行缓冲**的：`sys.stdin.read(1)` 会一直阻塞到用户按回车，
        按键根本送不进来，整个界面无法使用。这是本实现最初的写法，在 Windows
        上实测完全不可用。正确做法是平台原生的「有键就读、没键就走」。
        """
        if not self.interactive:
            return None
        if self._key_source is not None:
            return self._key_source()
        if os.name == "nt":
            return self._read_key_windows()
        return self._read_key_posix()

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

        # 扩展键：前缀 \x00 或 \xe0，第二字节是扫描码。
        if char in ("\x00", "\xe0"):
            try:
                if not msvcrt.kbhit():
                    time.sleep(_ESCAPE_GRACE)
                if not msvcrt.kbhit():
                    return KEY_ESCAPE
                scan = msvcrt.getwch()
            except (OSError, ValueError):
                return KEY_ESCAPE
            return WINDOWS_SCAN_CODES.get(scan, KEY_ESCAPE)

        return decode_key(char)

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

        if char == "\x1b":
            buffer = char
            deadline = time.monotonic() + _ESCAPE_GRACE
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

        `msvcrt.getwch()` 本身就不需要回车，所以即使这里失败，输入仍然可用
        ——只是用户敲的字会同时被控制台回显一次，看起来有点重影。
        """
        try:
            import msvcrt  # noqa: F401
        except ImportError:
            return False

        try:
            kernel32 = ctypes.windll.kernel32
            # 关键：声明指针宽度，否则 64 位下句柄被截断，SetConsoleMode 静默失败。
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            if not handle:
                return False
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
                return False
            original = mode.value
            # 关掉 ECHO(0x4) 与 LINE_INPUT(0x2)，保留其他位。
            if not kernel32.SetConsoleMode(
                ctypes.c_void_p(handle), original & ~0x0004 & ~0x0002
            ):
                return False

            def restore() -> None:
                try:
                    kernel32.SetConsoleMode(ctypes.c_void_p(handle), original)
                except Exception:  # noqa: BLE001
                    pass

            self._raw_restore = restore
            return True
        except Exception:  # noqa: BLE001
            return False


def cursor_column_at(column: int) -> str:
    """把光标横向移到本行第 `column` 列（1 基）。"""
    return cursor_column(column)
