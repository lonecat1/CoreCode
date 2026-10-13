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
import queue
import sys
import threading
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


def is_real_windows_console(which: int = -10) -> bool:
    """指定的标准句柄是否是一个**真正的** Windows 控制台。

    **这是与 `isatty()` 不同的判据，而取键时必须用它。**

    `msvcrt.kbhit()` / `getwch()` 只在标准句柄确实是控制台时才工作。在
    Git Bash / MSYS2 / Cygwin 下，stdin 是模拟终端（pty）或管道，`isatty()`
    可能返回 True，但 `GetConsoleMode` 会失败——此时 `kbhit()` **永远返回
    False**，表现为「界面画出来了，但按键完全无反应」。

    这个 bug 真实发生过：我仅凭 `isatty()` 就选了 msvcrt 路径。
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetConsoleMode.restype = ctypes.c_int

        handle = kernel32.GetStdHandle(which & 0xFFFFFFFF)
        if not handle:
            return False
        mode = ctypes.c_uint32()
        return bool(
            kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode))
        )
    except Exception:  # noqa: BLE001
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
        #: 真实的终端取键路径在自动化环境里没法驱动，于是「按键处理是否正确」
        #: 就永远得不到验证。把它们变成可替换的，就能用脚本按键真正跑完整个
        #: 交互循环。生产代码不传，走的仍是原生实现。
        self._key_source = key_source
        self._size_source = size_source

        #: 读取线程与它产出的键队列（见 `read_key` 的说明）。
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._key_queue: queue.Queue[str] = queue.Queue()

        #: 是否可以用 msvcrt 的「无需回车」路径。
        #:
        #: **判据必须是「标准句柄真的是控制台」，不能是 `isatty()`。**
        #: Git Bash / MSYS2 下 `isatty()` 为真但 `GetConsoleMode` 失败，
        #: 此时 `kbhit()` 永远返回 False —— 界面出来了，按键完全没反应。
        self._use_msvcrt = os.name == "nt" and is_real_windows_console(-10)

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
        self.stop_reader()
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

    # -- 输入能力自检 -------------------------------------------------------

    def input_diagnosis(self) -> str | None:
        """检查「按键能不能读到」。返回问题描述，正常时返回 None。

        这个检查存在的理由：**输入通道坏掉时的表现是「按键完全没反应」**，
        用户看到界面正常显示，会以为程序卡死，完全不知道是环境问题。
        提前检出并说清楚原因，比让人对着一个不响应的界面猜要好得多。

        已知会出问题的情况：
          - stdin 不是终端（被重定向、或在某些 IDE 的终端里）；
          - Git Bash / MSYS2 / Cygwin 下 `isatty()` 为真但标准句柄不是
            Windows 控制台，`msvcrt` 接口不可用；
          - stdin 已被别的东西读走（例如外层脚本）。
        """
        if self._key_source is not None:
            return None  # 测试注入的按键源，无需检查

        try:
            if not sys.stdin.isatty():
                return (
                    "stdin 不是终端（可能被重定向或管道占用）。\n"
                    "  按键无法读取。请直接在终端里运行，或用行模式：minicode"
                )
        except (AttributeError, ValueError):
            return "无法判断 stdin 是否为终端，按键可能无法读取。"

        if os.name == "nt" and not self._use_msvcrt and not self._console_handle_ok():
            # 既没有 msvcrt，也没有可用的控制台句柄——大概率是 MSYS 类环境。
            # 这种情况下通用路径仍然可能工作（pty 上的 select），因此不直接
            # 判定失败，只提示。
            return (
                "stdin 不是 Windows 控制台（常见于 Git Bash / MSYS2 / Cygwin）。\n"
                "  已改用通用读取路径；若按键仍无反应，请换用 PowerShell 或\n"
                "  Windows Terminal，或使用行模式：minicode"
            )
        return None

    def _console_handle_ok(self) -> bool:
        return is_real_windows_console(-10)

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
        """非阻塞地取一个逻辑键；没有按键时立即返回 None。

        ## 为什么用「读取线程」而不是「非阻塞轮询」

        早先的实现按平台选非阻塞接口：Windows 用 `msvcrt.kbhit()`，
        POSIX 用 `select()`。两者都有一个致命前提——**stdin 必须是那类接口
        认得的终端**：

          - `msvcrt.kbhit()` 只在标准句柄是**真正的 Windows 控制台**时有效。
            在 Git Bash / MSYS2 / Cygwin 下 stdin 是 pty 或管道，`isatty()`
            可能返回 True 而 `GetConsoleMode` 失败，于是 `kbhit()` **永远返回
            False**——界面画出来了，按键完全无反应。这是实际发生过的故障。
          - `select()` 在 Windows 上不能用于控制台句柄。

        改为**一条后台线程做阻塞读取、主线程从队列取**：

          - 不依赖任何「非阻塞探测」接口，因此对控制台、pty、管道一视同仁；
          - 主循环仍是「取不到就重绘」，不会阻塞界面；
          - 转义序列与扫描码的解码都收在读取线程里，与传输方式无关。

        代价是多一条线程，而它本来就在等 I/O，几乎不耗 CPU。
        """
        if not self.interactive:
            return None
        if self._key_source is not None:
            return self._key_source()

        self._ensure_reader()
        try:
            return self._key_queue.get_nowait()
        except queue.Empty:
            return None

    def _ensure_reader(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._read_loop, name="minicode-keys", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        """后台线程：阻塞读原始输入，解码成逻辑键后放进队列。

        退出条件：读到 EOF、流被关闭、或 `stop_reader()` 被调用。

        ## 悬空转义序列的处理

        单独的 `\\x1b` 既可能是「用户按了 Esc」，也可能是某个转义序列的开头
        （`\\x1b[A` = 方向键）。因此收到 `\\x1b` 后必须**限时等待**后续字节：

          - 等到了 → 是方向键等功能键；
          - 等够了还没有 → 判定为用户按了 Esc。

        没有这个兜底，`\\x1b` 会永久留在缓冲里，**按 Esc 永远没反应**。
        这也是读取必须支持超时的原因——无限阻塞的话超时根本检查不到。
        """
        stream = sys.stdin
        pending = ""
        escape_deadline: float | None = None

        while not self._reader_stop.is_set():
            # 有悬空转义序列时用短超时读取，否则可以一直等。
            timeout = None
            if escape_deadline is not None:
                timeout = max(0.0, escape_deadline - time.monotonic())

            try:
                chunk = self._read_chunk(stream, timeout)
            except (OSError, ValueError):
                break

            if chunk is None:
                if timeout is not None and escape_deadline is not None:
                    # 限时等待结束仍无后续 → 用户按的是 Esc。
                    if time.monotonic() >= escape_deadline:
                        self._key_queue.put(KEY_ESCAPE)
                        pending = pending[1:]
                        escape_deadline = None
                    continue
                break  # EOF

            pending += chunk
            keys, pending = self._split_keys(pending)
            for key in keys:
                self._key_queue.put(key)

            escape_deadline = (
                time.monotonic() + _ESCAPE_GRACE if pending.startswith("\x1b") else None
            )

        # 流结束时把残留字节按单键处理，避免丢掉最后一个按键。
        for char in pending:
            decoded = decode_key(char)
            if decoded is not None:
                self._key_queue.put(decoded)

    def _read_chunk(self, stream: Any, timeout: float | None = None) -> str | None:
        """读一小段原始输入。返回 None 表示「本次没有数据」或流已结束。

        `timeout=None` 表示可以无限等；否则最多等这么久。

        Windows 上若 msvcrt 可用，走「逐字符、无需回车」的路径；否则退回
        通用路径（`select` 或阻塞读取）——那可能需要回车，但**至少能用**，
        比「完全没反应」好得多。
        """
        if self._use_msvcrt:
            try:
                import msvcrt

                deadline = None if timeout is None else time.monotonic() + timeout
                while not msvcrt.kbhit():
                    if self._reader_stop.wait(0.005):
                        return None
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                return msvcrt.getwch()
            except (ImportError, OSError, ValueError):
                # 该环境下 msvcrt 不可用（例如 Git Bash）——永久退回通用路径。
                self._use_msvcrt = False

        return self._read_chunk_generic(stream, timeout)

    def _read_chunk_generic(self, stream: Any, timeout: float | None) -> str | None:
        """通用读取：能用 `select` 就限时等，否则退化为阻塞读。"""
        fd: int | None = None
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None

        if fd is not None:
            try:
                import select

                ready, _r, _e = select.select([fd], [], [], timeout)
                if not ready:
                    return None  # 超时（或无限等时不会走到这里）
                data = os.read(fd, 1)
                if not data:
                    return None
                return data.decode("utf-8", errors="replace")
            except (ImportError, OSError, ValueError):
                pass  # select 不可用（Windows 上的控制台句柄就是这样）

        # 最后手段：阻塞读一个字符。无法限时，但保证「至少能输入」。
        try:
            char = stream.read(1)
        except (OSError, ValueError):
            return None
        return char or None

    def _split_keys(self, pending: str) -> tuple[list[str], str]:
        """从缓冲里切出完整的逻辑键，返回 `(键列表, 剩余缓冲)`。

        需要缓冲是因为两类序列可能跨多次读取到达：

          - Windows 扩展键：`\\x00`/`\\xe0` + 扫描码（两字节）
          - ANSI 转义序列：`\\x1b[A` 等（两到六字节）
        """
        keys: list[str] = []
        index = 0
        length = len(pending)

        while index < length:
            char = pending[index]

            # Windows 扩展键
            if char in ("\x00", "\xe0"):
                if index + 1 >= length:
                    break  # 扫描码还没到
                keys.append(WINDOWS_SCAN_CODES.get(pending[index + 1], KEY_ESCAPE))
                index += 2
                continue

            # ANSI 转义序列
            if char == "\x1b":
                matched, consumed = self._match_escape(pending, index)
                if consumed == 0:
                    break  # 序列还没收齐
                keys.append(matched)
                index += consumed
                continue

            decoded = decode_key(char)
            if decoded is not None:
                keys.append(decoded)
            index += 1

        return keys, pending[index:]

    def _match_escape(self, pending: str, start: int) -> tuple[str | None, int]:
        """尝试从 `start` 起匹配一个转义序列。

        返回 `(逻辑键, 消耗的字符数)`。`consumed == 0` 表示「还需要更多字节」。
        """
        remaining = pending[start:]

        # 已经是完整已知序列
        if remaining in ESCAPE_SEQUENCES:
            return ESCAPE_SEQUENCES[remaining], len(remaining)

        # 还有可能是某个已知序列的前缀 → 继续等
        if any(seq.startswith(remaining) for seq in ESCAPE_SEQUENCES):
            return None, 0

        # 不再匹配任何已知序列：取最长能匹配的前缀，剩下的留给后续。
        best = ""
        for seq in ESCAPE_SEQUENCES:
            if remaining.startswith(seq) and len(seq) > len(best):
                best = seq
        if best:
            return ESCAPE_SEQUENCES[best], len(best)

        # 单个 Esc（后面跟的不是序列的一部分）
        if len(remaining) == 1:
            # 等一小会儿看是否有后续；没有就当作单独的 Esc。
            # 这里直接返回 Esc，避免「按 Esc 没反应」。
            return KEY_ESCAPE, 1
        return KEY_ESCAPE, 1

    def stop_reader(self) -> None:
        """让读取线程退出（`leave()` 会调用）。"""
        self._reader_stop.set()

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
