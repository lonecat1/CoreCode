"""控制台界面 —— 把渲染层接到会话宿主上。

## 这个模块做四件事

  1. 把宿主的回调（`on_assistant` / `on_tool_start` …）转成渲染调用；
  2. 决定「什么时候该换行、什么时候该安静」；
  3. 处理流式输出的拼接；
  4. 管理**可选**动画（转圈 / 启动像素画）。

**它不碰终端输入。** 没有 raw mode、没有读键、没有全屏重绘——只往 stdout
写文本，动画也只用到 `\\r` 与相对光标移动。这是它能在任何终端里都工作的
原因（全屏 TUI 依赖读键，在 Git Bash 下按键永远读不到）。

## 动画是可选增强，不是前提

每个动画都先过 `caps.supports_animation`：非终端、`NO_COLOR`、`TERM=dumb`
一律跳过。**跳过后不输出任何东西**（连空行都不留），因此重定向到文件时
输出是干净的。动画失败也绝不影响功能。

## 输出节奏

行模式的美观有一半在于**留白的节奏**，而这类细节最容易出错：

  - 助手正文前留一个空行（与上一段分开），但**首条消息前不留**；
  - 工具调用紧凑排列（它们是一组动作，不该被空行切碎）；
  - 进度消息压暗且不加空行；
  - 一次回合结束后留一个空行，让下一轮输入有呼吸感；
  - 转圈动画只在自己那一行活动，收尾时擦干净。

这些规则集中在 `ConsoleUI` 里，因此可以被测试（用假 stdout）。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, TextIO

from .animate import Spinner
from .caps import supports_animation
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
    #: 是否启用动画。构造时按环境探测，也可显式传入（CLI 的 --no-animation）。
    animate: bool | None = None
    #: 是否已经输出过内容。用于决定「首条消息前是否留空行」。
    _printed_anything: bool = False
    _printer: StreamPrinter | None = None
    _spinner: Spinner | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.width:
            self.width = output_width()
        # stdout 不是终端时关掉颜色——否则转义序列会污染重定向的文件。
        # 这与 TUI 不同：行模式必须能在管道里安全使用。
        set_color_enabled(supports_color(self.stream))
        if self.animate is None:
            # 转圈只用 `\r`，不需要光标移动，因此不要求 VT。
            self.animate = supports_animation(self.stream)

    # -- 动画 ---------------------------------------------------------------

    def start_spinner(self, label: str = "思考中…") -> None:
        """开始转圈动画。

        只在真正支持动画的终端里生效；否则完全静默。动画失败也绝不影响
        功能——它是可选增强，不是前提。
        """
        if not self.animate:
            return
        self.stop_spinner()
        self._spinner = Spinner(label=label, stream=self.stream)
        self._spinner.start()

    def update_spinner(self, label: str) -> None:
        if self._spinner is not None:
            self._spinner.label = label

    def stop_spinner(self) -> None:
        """停止并擦掉转圈行。

        **必须在正式输出之前调用**：否则转圈残影会与正文混在同一行。

        这里额外检查 `_spinner` 是否真的画过东西（`drawn`）——后台线程刚起
        就被叫停时它可能一帧都没画，此时发擦除序列会在屏幕上留下一个多余
        的空行。这个小细节决定了输出是否干净。
        """
        spinner = self._spinner
        self._spinner = None
        if spinner is not None:
            spinner.stop()

    # -- 底层输出 -----------------------------------------------------------

    def write(self, text: str, *, end: str = "\n") -> None:
        if not text:
            return
        # 正文输出前先擦掉动画行，避免两者叠在同一行。
        self.stop_spinner()
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
        # 进度消息本身就是「还在干活」的信号，此时转圈是多余的。
        self.stop_spinner()
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
        """回合开始：留空行并启动转圈。

        转圈在这里起，在**任何正式输出之前**会被 `write()` 自动擦掉，
        因此用户看到的是「转圈 → 结果」，不会有残影。
        """
        if self._printed_anything:
            self.blank()
        self.start_spinner("思考中…")

    def end_turn(self) -> None:
        """回合结束：擦掉转圈、冲掉流式尾巴，并留一个空行。"""
        self.stop_spinner()
        self.flush_assistant()
        self.blank()
        self._printer = None

    # -- 启动动画 -----------------------------------------------------------

    def play_startup(self, *, should_abort: Any = None) -> None:
        """播放启动像素动画。非终端环境下完全静默。"""
        if not self.animate:
            return
        from .startup import StartupAnimation

        StartupAnimation(
            width=self.width, enabled=True, stream=self.stream
        ).play(should_abort=should_abort)

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
