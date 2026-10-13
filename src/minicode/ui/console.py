"""控制台界面 —— 把渲染层接到会话宿主上。

## 这个模块只做三件事

  1. 把宿主的回调（`on_assistant` / `on_tool_start` …）转成渲染调用；
  2. 决定「什么时候该换行、什么时候该安静」；
  3. 处理流式输出的拼接。

**它不碰终端控制。** 没有 raw mode、没有光标定位、没有全屏重绘——
只往 stdout 写文本。这是它能在你任何终端里都工作的原因。

## 输出节奏

行模式的美观有一半在于**留白的节奏**，而这类细节最容易出错：

  - 助手正文前留一个空行（与上一段分开），但**首条消息前不留**；
  - 工具调用紧凑排列（它们是一组动作，不该被空行切碎）；
  - 进度消息压暗且不加空行；
  - 一次回合结束后留一个空行，让下一轮输入有呼吸感。

这些规则集中在 `ConsoleUI` 里，因此可以被测试（用假 stdout）。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, TextIO

from .render import (
    StreamPrinter,
    output_width,
    render_assistant,
    render_error,
    render_notice,
    render_progress,
    render_question,
    render_status_line,
    render_tool_result,
    render_tool_start,
    render_user,
    supports_color,
)
from .theme import COLOR_HINT, paint, set_color_enabled


@dataclass
class ConsoleUI:
    """行模式界面。所有输出都经由这里，便于测试与统一节奏。"""

    stream: TextIO = field(default_factory=lambda: sys.stdout)
    width: int = 0
    #: 是否已经输出过内容。用于决定「首条消息前是否留空行」。
    _printed_anything: bool = False
    _printer: StreamPrinter | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.width:
            self.width = output_width()
        # stdout 不是终端时关掉颜色——否则转义序列会污染重定向的文件。
        # 这与 TUI 不同：行模式必须能在管道里安全使用。
        set_color_enabled(supports_color(self.stream))

    # -- 底层输出 -----------------------------------------------------------

    def write(self, text: str, *, end: str = "\n") -> None:
        if not text:
            return
        with self._lock:
            try:
                self.stream.write(text + end)
                self.stream.flush()
            except (OSError, ValueError):
                # 管道关闭（例如 `| head`）不该让程序崩溃。
                pass
            self._printed_anything = True

    def blank(self) -> None:
        """输出一个空行（用于分隔段落）。"""
        with self._lock:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    # -- 用户输入回显 -------------------------------------------------------

    def echo_user(self, text: str) -> None:
        """回显用户输入。

        行模式下终端通常自己回显，但经过 `input()` 之后已经换行，
        这里再打一次能让对话记录完整（尤其在滚动回看时有用）。
        """
        self.write(render_user(text, self.width))

    # -- 助手输出（流式） ---------------------------------------------------

    def _ensure_printer(self) -> StreamPrinter:
        if self._printer is None:
            self._printer = StreamPrinter(width=self.width)
        return self._printer

    def assistant(self, text: str, *, is_final: bool = True) -> None:
        """输出助手文本。

        流式场景下回调会被多次调用，因此按行缓冲：遇到换行才输出，
        回合结束由 `end_turn()` 冲掉残留。
        """
        printer = self._ensure_printer()
        chunk = printer.feed(text)
        if chunk:
            self.write(chunk)

    def flush_assistant(self) -> None:
        """冲掉流式缓冲里残留的尾巴。**回合结束时必须调用**，否则丢尾。"""
        if self._printer is None:
            return
        tail = self._printer.flush()
        if tail:
            self.write(tail)

    # -- 工具调用 -----------------------------------------------------------

    def tool_start(self, name: str, detail: str = "") -> None:
        self.write(render_tool_start(name, detail, self.width))

    def tool_result(self, summary: str, *, ok: bool = True, detail: str = "") -> None:
        """工具结果。

        `detail` 在这里**统一压缩**，而不是指望调用方先处理：
        工具输出动辄上千字符，原样打出来会刷屏并把对话淹没。放在入口处
        压缩，就没有任何调用路径能绕过它。

        `ask_user` 是特例：它不是「执行了某件事」，而是**在向用户提问**。
        混在普通工具结果里会让人看不出需要自己回答——因此单独醒目显示。
        """
        if summary == "ask_user":
            self.question(detail)
            return

        # `⏺ read_file` 已经说明了是哪个工具，`⎿` 后面再重复一次工具名
        # 是纯噪音。有详情时用详情当摘要，没有才退回工具名。
        short = _shorten(detail)
        label = short or summary
        self.write(render_tool_result(label, ok=ok, width=self.width))

    def question(self, text: str) -> None:
        """醒目地显示模型的反问，并说明需要用户回答。

        行模式没有可交互的待批准面板，因此必须靠**明确的文字提示**来告诉
        用户「现在轮到你了」。少了这一步，`ask_user` 看起来就像一条普通
        输出，用户会以为程序卡住了。
        """
        body = _shorten(text, limit=400) or "（模型没有给出具体问题）"
        self.blank()
        self.write(render_question(body, self.width))
        self.write(paint("请直接输入你的回答。", COLOR_HINT))
        self.blank()

    # -- 其它 ---------------------------------------------------------------

    def progress(self, text: str) -> None:
        self.write(render_progress(text, self.width))

    def notice(self, text: str) -> None:
        self.write(render_notice(text, self.width))

    def error(self, text: str) -> None:
        self.write(render_error(text, self.width))

    def status(self, **kwargs: Any) -> None:
        line = render_status_line(**kwargs)
        if line:
            self.write(line)

    # -- 回合边界 -----------------------------------------------------------

    def begin_turn(self) -> None:
        """回合开始：在助手正文之前留一个空行（首条消息除外）。"""
        if self._printed_anything:
            self.blank()

    def end_turn(self) -> None:
        """回合结束：冲掉流式尾巴，并留一个空行。"""
        self.flush_assistant()
        self.blank()
        self._printer = None

    # -- 供宿主使用的回调集合 -----------------------------------------------

    def callbacks(self) -> dict[str, Any]:
        """构造 `HostCallbacks` 需要的回调字典。

        返回字典而不是直接改宿主，是为了让调用方（CLI）决定接哪些、
        以及在测试里替换掉。
        """
        return {
            "on_assistant": lambda text, is_final: self.assistant(
                text, is_final=is_final
            ),
            "on_progress": self.progress,
            "on_tool_start": lambda name, data: self.tool_start(str(name)),
            "on_tool_result": lambda name, output, is_error: self.tool_result(
                str(name), ok=not is_error, detail=output
            ),
            "on_notice": self.notice,
        }


def _shorten(text: Any, limit: int = 100) -> str:
    """把工具输出压成一行摘要。

    行模式里没有可滚动的详情面板，因此只展示第一行有意义的内容——
    完整输出仍然在会话记录里，需要时可以读文件或用 `/status` 查。

    **必须同时限制长度**：工具输出动辄上千字符，原样打出来会把终端刷屏，
    把真正的对话淹没。这个函数是界面可用性的一部分，不是可选的润色。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    first = raw.splitlines()[0].strip()
    if len(first) <= limit:
        return first
    return first[: limit - 1] + "…"


def build_console_ui(stream: TextIO | None = None) -> ConsoleUI:
    return ConsoleUI(stream=stream if stream is not None else sys.stdout)
