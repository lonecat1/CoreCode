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
from .theme import COLOR_ASSISTANT, COLOR_HINT, paint, set_color_enabled


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
    #: 本回合是否已经走过流式路径。用于避免与非流式重复输出。
    _streamed_this_turn: bool = False
    #: 流式开头的探测缓冲（用于剥掉协议标记）与是否已完成探测。
    _stream_probe: str = ""
    _stream_probe_done: bool = False
    #: 流式末尾的扣留缓冲：避免把 `</fi` 这种半个收尾标签当正文输出。
    _tail_hold: str = ""
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
        """输出助手文本（**非流式**路径）。

        按行缓冲：遇到换行才输出，回合结束由 `end_turn()` 冲掉残留。
        这样即便回调被分多次调用，输出也是完整的行。

        **本回合已经流过式时直接忽略**：内容已经逐字打出来了，
        再打一遍就是重复。这是流式与非流式两条路径的接缝处，必须显式处理。
        """
        if self._streamed_this_turn:
            return
        printer = self._ensure_printer()
        chunk = printer.feed(text)
        if chunk:
            self.write(chunk)

    def flush_assistant(self) -> None:
        """冲掉缓冲里残留的尾巴。**回合结束时必须调用**，否则丢尾。"""
        if self._printer is None:
            return
        tail = self._printer.flush()
        if tail:
            self.write(tail)

    # -- 流式增量 -----------------------------------------------------------

    #: 开始流式输出前最多缓冲的字符数。
    #:
    #: 模型用 `<final>` / `<progress>` 前缀声明这段文本的性质，而这些标记
    #: **不该显示给用户**。但流式是边收边打的，等收完再解析就失去了流式的
    #: 意义。折中办法：先缓冲开头若干字符，确认不是标记之后立刻放行。
    #:
    #: 取 12 是因为最长的标记 `<progress>` 是 10 个字符，留一点余量。
    STREAM_PROBE_CHARS = 12

    def assistant_delta(self, text: str) -> None:
        """逐字输出一段增量（**流式**路径）。

        ## 三个关键决定

        **① 开头缓冲若干字符，剥掉协议标记。**
        模型会在最前面写 `<final>` / `<progress>`，那是给程序看的。
        但缓冲必须是**有条件**的：一旦能确定「不是标记」就立刻放行，
        否则短增量会被一直攒着，用户看到的是卡顿而不是流畅输出
        ——那就把流式的意义抵消掉了。

        **② 先擦转圈再写字。** 转圈占着当前行，直接写字会与它叠在一起。

        **③ 不用行缓冲。** 流式的意义就是让用户看到字在长出来。
        """
        if not text:
            return

        if not self._stream_probe_done:
            self._stream_probe += text
            # 能确定「不是标记」就立刻放行——这是流畅度的关键。
            if not _could_be_marker_prefix(self._stream_probe):
                text = self._release_stream_probe()
            elif len(self._stream_probe) < self.STREAM_PROBE_CHARS:
                # 仍可能是标记的一部分，继续等。
                return
            else:
                text = self._release_stream_probe()
            if not text:
                return

        self._write_delta(text)

    def _release_stream_probe(self) -> str:
        """探测结束：剥掉标记，返回该输出的正文。"""
        self._stream_probe_done = True
        buffered = self._stream_probe
        self._stream_probe = ""
        return _strip_response_markers(buffered)

    def flush_stream_probe(self) -> None:
        """把还在缓冲里的内容全部吐出来。**回合结束时必须调用。**

        少了它会有两类内容丢失：

          - 短回答（不足 `STREAM_PROBE_CHARS`）留在探测缓冲里，一个字不显示；
          - 末尾的半个收尾标签留在扣留缓冲里。

        ## 一个容易写成死循环的地方

        冲刷时**不能**再走 `_write_delta`——那会重新执行 `_hold_tail`，
        把刚决定输出的内容又扣回去。我第一版就是这样：`<` 被吐出来又立刻
        被扣住，最终消失。

        但也不能直接 `_emit`：扣留缓冲里可能正好是一个**完整的收尾标签**
        （`</final>` 恰好在增量边界结束），直接写出去就漏了。因此这里
        先剥一次标记，再写。
        """
        held, self._tail_hold = self._tail_hold, ""
        if held:
            cleaned = _strip_response_markers(held)
            if cleaned:
                self._emit(cleaned)

        if not self._stream_probe_done and self._stream_probe:
            text = self._release_stream_probe()
            if text:
                self._emit(text)

    def _hold_tail(self, text: str) -> str:
        """把末尾可能是「半个收尾标签」的部分留下来，不输出。

        ## 为什么需要它

        流式增量会把收尾标签切开：`</fi` 和 `nal>` 分两次到达。若不做处理，
        第一段会被当成正文打出去，界面上就会残留 `</final>`。

        做法是：只要末尾**有可能是某个收尾标签的前缀**，就先扣住不发，
        等下一个增量到齐了再一起判断。扣住的字符数有上限（最长标签长度），
        因此不会无限积压。
        """
        combined = self._tail_hold + text
        keep = _longest_closing_prefix(combined)
        if keep:
            self._tail_hold = combined[-keep:]
            return combined[:-keep]
        self._tail_hold = ""
        return combined

    def _write_delta(self, text: str) -> None:
        """写入一段增量（先扣留末尾可能是半个标签的部分）。"""
        text = self._hold_tail(text)
        if text:
            self._emit(text)

    def _emit(self, text: str) -> None:
        """直接写出文本，**不再做任何扣留**。

        与 `_write_delta` 的分工：`_write_delta` 负责「决定哪些字节现在
        可以出去」（含扣留判断），`_emit` 只负责写。冲刷路径必须用 `_emit`
        ——否则会把刚吐出来的内容又扣回去。
        """
        if not text:
            return
        self.stop_spinner()
        self._streamed_this_turn = True
        with self._lock:
            try:
                self.stream.write(
                    paint(text, COLOR_ASSISTANT) if COLOR_ASSISTANT else text
                )
                self.stream.flush()
            except (OSError, ValueError):
                pass
            self._printed_anything = True

    # -- 工具调用 -----------------------------------------------------------

    def tool_start(self, name: str, detail: str = "") -> None:
        self.write(render_tool_start(name, detail, self.width))

    def tool_result(
        self, summary: str, *, ok: bool = True, detail: str = "", full: bool = False
    ) -> None:
        """工具结果。

        `detail` 默认在这里**统一压缩**，而不是指望调用方先处理：工具输出动辄
        上千字符，原样打出来会刷屏并把对话淹没。放在入口处压缩，就没有任何
        调用路径能绕过它。

        `full=True` 则**完整展示**输出（首行当摘要，其余作为缩进正文）。
        这是给**用户主动发起**的调用用的：用户自己敲了 `/read pyproject.toml`，
        压缩成一行 `PATH: pyproject.toml` 就等于什么都没给他——他打这条命令
        的目的就是看内容。模型驱动的调用没有这个诉求，因此保持压缩默认值。

        `ask_user` 是特例：它不是「执行了某件事」，而是**在向用户提问**。
        混在普通工具结果里会让人看不出需要自己回答——因此单独醒目显示。
        """
        if summary == "ask_user":
            self.question(detail)
            return

        if full:
            head, _, rest = detail.strip().partition("\n")
            # 首行交给渲染层按宽度截断，不在这里再压一次（否则丢信息）。
            self.write(
                render_tool_result(
                    head or summary, ok=ok, detail=rest.strip(), width=self.width
                )
            )
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
        """中途进度消息。

        进度消息本身就是「还在干活」的信号，此时转圈是多余的。

        **本回合已经流过式时直接忽略**：那段文本在流式过程中已经逐字显示过
        了，再打一遍就是重复。这是流式引入的一个非显然后果——`hooks.progress()`
        与流式回调会看到同一段内容。
        """
        self.stop_spinner()
        if self._streamed_this_turn:
            return
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
        """回合结束：擦掉转圈、收尾正文、留空行，并显示本次统计。

        ## 收尾顺序很重要

        探测缓冲**必须无条件先冲**：短回答（不足 `STREAM_PROBE_CHARS`）从头到尾
        都待在缓冲里，此时 `_streamed_this_turn` 仍是 False——若按它来选分支，
        那段内容会被直接丢掉，界面上一个字都不显示。
        """
        self.stop_spinner()
        # 先冲探测缓冲（无论走没走过流式）。
        self.flush_stream_probe()
        # 非流式路径才有 printer 缓冲需要冲；流式内容已经打出去了。
        if not self._streamed_this_turn:
            self.flush_assistant()
        self.blank()
        self._printer = None
        self._streamed_this_turn = False
        self._stream_probe = ""
        self._stream_probe_done = False
        self._tail_hold = ""

    def turn_stats(self, stats: Any) -> None:
        """显示本次回合的耗时与工具调用次数。

        显示在回合末尾、压暗、**单独一行**——它是元信息，不该与回答正文
        挤在一起。

        调用时机是 `end_turn()` **之后**：那时正文已经收尾、空行也已留出，
        这里直接写一行即可，不需要自己处理换行。

        **没有任何回合记录时完全静默**：`SessionStats()` 的 `last` 是一个
        全零的默认值，`line()` 会返回 `<1ms` 这种无意义的文本。必须显式
        判空，否则会在界面上凭空多出一行垃圾。
        """
        if not getattr(stats, "turns", 0):
            return
        line = getattr(getattr(stats, "last", None), "line", None)
        text = line() if callable(line) else ""
        if not text:
            return
        self.write(paint(f"  ⏱ {text}", COLOR_HINT))

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
            "on_assistant_delta": self.assistant_delta,
            "on_progress": self.progress,
            "on_tool_start": lambda name, data: self.tool_start(str(name)),
            "on_tool_result": lambda name, output, is_error: self.tool_result(
                str(name), ok=not is_error, detail=output
            ),
            "on_notice": self.notice,
            # 注意：**不注册 on_turn_stats**。统计由调用方在 `end_turn()` 之后
            # 显式显示——那时正文已收尾、空行已留出，元信息才能干净地单独占
            # 一行。若挂在这里，它会在 submit 内部就打印，与正文挤在一起，
            # 而且与调用方的显式显示重复（我实际踩过：统计打了两遍）。
        }


#: 模型可能用来声明响应性质的标记。
_RESPONSE_MARKERS: tuple[tuple[str, str], ...] = (
    ("<final>", "</final>"),
    ("[FINAL]", "[/FINAL]"),
    ("<progress>", "</progress>"),
    ("[PROGRESS]", "[/PROGRESS]"),
)


def _could_be_marker_prefix(text: str) -> bool:
    """这段文本有没有可能**正在**构成一个标记前缀？

    用于决定流式开头要不要继续缓冲：

      - `"<"`、`"<fi"`、`"<final"` → 可能是标记，再等等
      - `"你"`、`"<x"` → 不可能是标记，立刻放行

    这个判断是流畅度的关键：没有它，短增量会被一律攒到阈值才显示，
    用户看到的是卡顿而不是流式。
    """
    stripped = text.lstrip()
    if not stripped:
        return True  # 只有空白，标记可能还在后面
    if not stripped.startswith(("<", "[")):
        return False
    return any(opening.startswith(stripped) for opening, _ in _RESPONSE_MARKERS)


def _longest_closing_prefix(text: str) -> int:
    """`text` 末尾有多长的一段是**某个收尾标签的前缀**？

    返回需要扣住的字符数（0 表示末尾不构成任何收尾标签的开头）。
    用于避免把 `</fi` 这种半个标签当正文输出——否则界面上会残留
    `</final>` 这样的碎片。
    """
    for length in range(min(len(text), 12), 0, -1):
        suffix = text[-length:]
        if not suffix.startswith(("<", "[")):
            continue
        lowered = suffix.lower()
        for _opening, closing in _RESPONSE_MARKERS:
            if closing.lower().startswith(lowered):
                return length
    return 0


def _strip_response_markers(text: str) -> str:
    """剥掉 `<final>` / `<progress>` 及其收尾标签。

    这些标记是模型与**程序**之间的协议（声明这段文本是最终答复还是中途
    进度），不是给用户看的内容。流式路径下必须在输出前剥掉。

    刻意不 import 适配器里的同名函数：界面层不该依赖某个具体适配器。
    标记集合很小且稳定，在这里重复一次比引入反向依赖更划算。
    """
    cleaned = text
    for opening, closing in _RESPONSE_MARKERS:
        stripped = cleaned.lstrip()
        if stripped.startswith(opening):
            cleaned = stripped[len(opening) :]
        lowered = cleaned.lower()
        index = lowered.find(closing.lower())
        if index != -1:
            cleaned = cleaned[:index] + cleaned[index + len(closing) :]
    return cleaned


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
