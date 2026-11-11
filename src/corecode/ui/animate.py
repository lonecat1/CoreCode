"""动画原语 —— 转圈与固定高度块重绘。

## 两类动画，两套安全边界

**① 单行动画（转圈）**：只发 `\\r`（回车）回到行首，重写同一行。
不移动光标、不依赖终端尺寸。**几乎所有终端都支持**，因此最安全。

**② 多行动画（启动像素画）**：需要「上移 N 行」再重绘。用相对移动
（`ESC[<n>A`）而不是绝对定位——相对移动只需要知道**自己占了几行**，
不需要知道终端总行数。这是它与失败的全屏 TUI 的本质区别：全屏 TUI 用
绝对定位（`ESC[<行>;1H`），要求「渲染行数恰好等于终端行数」，一旦不等
就整体错位。

## 设计约束

  - **每一帧都必须是固定高度**。多行动画里，帧高变化会让相对移动算错，
    于是画面逐渐漂移。因此补空行到固定高度，而不是让帧自己长高。
  - **每一行都必须 ≤ 终端宽度**。超宽会让终端自动折行，同样破坏行数记账。
  - **必须能优雅收尾**：`stop()` 擦掉动画行，让后续输出从干净的一行开始。
    少了这一步，动画的残影会跟正式输出混在一起。
  - **非终端环境下完全静默**：不输出任何东西，也不留空行。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .theme import COLOR_HINT, COLOR_STATUS, paint

# ---------------------------------------------------------------------------
# ANSI 原语
# ---------------------------------------------------------------------------

CARRIAGE_RETURN = "\r"
ERASE_LINE = "\x1b[2K"
ERASE_TO_END = "\x1b[J"


def cursor_up(count: int) -> str:
    """上移 `count` 行。`count<=0` 时返回空串。"""
    return f"\x1b[{count}A" if count > 0 else ""


def cursor_down(count: int) -> str:
    return f"\x1b[{count}B" if count > 0 else ""


#: 转圈字形。用盲文点阵——它在等宽字体里宽度一致，不会让行抖动。
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

#: 备用的 ASCII 转圈（终端字体缺盲文字形时用）。
SPINNER_ASCII = ("|", "/", "-", "\\")


def spinner_glyph(frame: int, *, ascii_only: bool = False) -> str:
    frames = SPINNER_ASCII if ascii_only else SPINNER_FRAMES
    return frames[frame % len(frames)]


# ---------------------------------------------------------------------------
# 单行动画
# ---------------------------------------------------------------------------


@dataclass
class Spinner:
    """单行转圈动画。

    用法::

        spinner = Spinner("思考中…")
        spinner.start()          # 起一个后台线程定时重绘
        ...                      # 干活
        spinner.stop()           # 擦掉动画行

    也可以在同步代码里手动 `tick()`，不启线程——测试走这条路径。
    """

    label: str = ""
    stream: object | None = None
    enabled: bool = True
    #: 每秒帧数。12 帧/秒足够顺，又不会让慢速终端来不及渲染。
    fps: int = 12

    _frame: int = 0
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _drawn: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- 帧内容 -------------------------------------------------------------

    def frame_text(self) -> str:
        glyph = spinner_glyph(self._frame)
        text = f"{glyph} {self.label}" if self.label else glyph
        return paint(text, COLOR_STATUS)

    def tick(self) -> None:
        """推进一帧并重绘（同步版本，供测试与手动驱动使用）。"""
        if not self.enabled:
            return
        self._frame += 1
        self._draw()

    def _draw(self) -> None:
        with self._lock:
            self._write(CARRIAGE_RETURN + ERASE_LINE + self.frame_text())
            self._drawn = True

    def _write(self, text: str) -> None:
        import sys

        target = self.stream if self.stream is not None else sys.stdout
        try:
            target.write(text)  # type: ignore[union-attr]
            target.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            self.enabled = False

    # -- 后台线程 -----------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        # **立刻画第一帧**，不等第一个定时器。
        #
        # 两个好处：用户等待时马上看到反馈（而不是先愣 83ms）；行为变得
        # 确定——「start 之后一定已经画过一帧」，因此 `stop()` 的擦除时机
        # 是可预测的，测试也能稳定断言。
        self._draw()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="corecode-spinner", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / max(1, self.fps)
        while not self._stop.wait(interval):
            self._frame += 1
            self._draw()

    def stop(self) -> None:
        """停止并擦掉动画行。

        **必须调用**：否则转圈的残影会与后续正式输出混在一起。
        擦除用「回车 + 擦整行」，让光标留在干净的行首。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._thread = None
        if self._drawn and self.enabled:
            with self._lock:
                self._write(CARRIAGE_RETURN + ERASE_LINE)
            self._drawn = False

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# 多行动画（固定高度块）
# ---------------------------------------------------------------------------


@dataclass
class BlockAnimator:
    """固定高度块的逐帧重绘。

    用于启动像素动画这类多帧内容。**帧高必须固定**——高度变化会让相对移动
    算错位置，画面逐渐漂移。因此 `render` 会把帧补齐/截断到 `height` 行。
    """

    height: int
    width: int = 80
    stream: object | None = None
    enabled: bool = True

    _drawn_height: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def render(self, lines: list[str], *, color: str = "") -> None:
        """画一帧。`lines` 会被补齐或截断到固定高度。"""
        if not self.enabled:
            return

        from .width import string_display_width, truncate_plain

        frame: list[str] = []
        for line in lines[: self.height]:
            shown = (
                line
                if string_display_width(line) <= self.width
                else truncate_plain(line, self.width)
            )
            frame.append(paint(shown, color) if color else shown)
        while len(frame) < self.height:
            frame.append("")

        with self._lock:
            parts: list[str] = []
            if self._drawn_height:
                # 回到块首行：相对移动，只需知道自己占了几行。
                parts.append(cursor_up(self._drawn_height - 1))
                parts.append(CARRIAGE_RETURN)
            parts.append("\n".join(frame))
            self._write("".join(parts))
            self._drawn_height = len(frame)

    def clear(self) -> None:
        """擦掉整块，让光标停在块首行。

        必须调用：否则动画残影会与后续输出混在一起。
        """
        if not self.enabled or not self._drawn_height:
            return
        with self._lock:
            parts = [cursor_up(self._drawn_height - 1), CARRIAGE_RETURN, ERASE_TO_END]
            self._write("".join(parts))
            self._drawn_height = 0

    def _write(self, text: str) -> None:
        import sys

        target = self.stream if self.stream is not None else sys.stdout
        try:
            target.write(text)  # type: ignore[union-attr]
            target.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            self.enabled = False

    def play(
        self,
        frames: list[list[str]],
        *,
        interval: float = 0.08,
        color: str = "",
        max_seconds: float | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        """依次播放各帧（同步，阻塞）。

        `max_seconds` 是**硬上限**：启动动画绝不能拖慢程序启动。
        `should_abort` 让调用方在用户已开始输入时立刻中断动画。
        """
        if not self.enabled or not frames:
            return
        started = time.monotonic()
        for frame in frames:
            if should_abort is not None and should_abort():
                break
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                break
            self.render(frame, color=color)
            time.sleep(interval)
        self.clear()


def hint_line(text: str) -> str:
    """动画下方的一行提示（压暗）。"""
    return paint(text, COLOR_HINT)
