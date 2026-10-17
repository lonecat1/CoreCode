"""终端美化输出 —— 仿 Claude Code 的**行模式**外观。

## 为什么是行模式而不是全屏 TUI

Claude Code 的默认界面本身就是**美化过的行模式 REPL**：逐行输出、历史交给
终端的滚动缓冲、输入用普通回车提交。它不做 raw mode、不做光标定位、不做
全屏重绘。

这个选择不只是审美——它决定了可靠性。全屏 TUI 依赖三样与终端实现强耦合的
能力：

    1. raw mode（Windows 用 msvcrt / POSIX 用 termios）
    2. 非阻塞读键（kbhit / select）
    3. 光标定位与原地重绘

任何一环在用户的实际终端里不成立，界面就表现为「画出来了但按键没反应」。
本项目为此反复失败过多次（Git Bash 下 `msvcrt.kbhit()` 永远返回 False 就是
典型）。行模式把这三样依赖**全部消除**，只剩「往 stdout 写文本」——
这在任何终端、任何重定向、任何 CI 里都成立。

## 设计约束

本模块**只做字符串变换，不碰 stdout**。所有函数接收数据、返回文本，由调用方
决定何时打印。因此：

  - 可以被完整测试（不需要伪终端）；
  - 可以在管道/CI 里安全使用；
  - 颜色可一键关闭（`NO_COLOR`），退化后布局不变。

## 视觉语言（对齐 Claude Code）

    ⏺  工具调用起头
    ⎿  工具结果承接（缩进）
    >  用户输入与提示符
    ╭─╮ 圆角框（仅用于启动横幅与输入提示）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .theme import (
    BLUE,
    BOLD,
    BOX_BOTTOM_LEFT,
    BOX_BOTTOM_RIGHT,
    BOX_HORIZONTAL,
    BOX_TOP_LEFT,
    BOX_TOP_RIGHT,
    BOX_VERTICAL,
    BRIGHT_CYAN,
    BRIGHT_GREEN,
    BRIGHT_RED,
    BRIGHT_YELLOW,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    YELLOW,
    COLOR_ASSISTANT,
    COLOR_BANNER_TITLE,
    COLOR_ERROR,
    COLOR_HINT,
    COLOR_NOTICE,
    COLOR_PROGRESS,
    COLOR_STATUS,
    COLOR_TOOL,
    COLOR_TOOL_FAIL,
    COLOR_TOOL_OK,
    COLOR_USER,
    GLYPH_BULLET,
    GLYPH_PROMPT,
    GLYPH_RESULT,
    GLYPH_TOOL,
    GLYPH_USER,
    SPINNER,
    color_enabled,
    paint,
)
from .width import string_display_width, truncate_plain, wrap_plain

#: 输出宽度上限。终端很宽时不让正文拉得太长——超长行极难阅读。
#: 80 是排版上的经典值，也是 Claude Code 的观感。
DEFAULT_WIDTH = 80

#: 正文最大宽度（超过则折行）。
MAX_BODY_WIDTH = 100


# ---------------------------------------------------------------------------
# 宽度决策
# ---------------------------------------------------------------------------


def terminal_columns() -> int | None:
    """终端的原始列数；问不到时用 `COLUMNS` 环境变量；都没有则 `None`。

    与 `output_width()` 的区别：这里**不封顶、不设下限**，返回「终端到底多宽」，
    由调用方自己决定怎么用（横幅需要 `max(60, …)` 且不封顶）。
    """
    try:
        return os.get_terminal_size().columns
    except (OSError, ValueError):
        pass
    raw = os.environ.get("COLUMNS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def output_width(columns: int | None = None) -> int:
    """决定正文用多宽。

    不直接问终端：管道/重定向下问不到，而且极宽的终端会让长段落难以阅读。
    因此取「终端宽度」与 `MAX_BODY_WIDTH` 的较小值，并且不低于 40。
    """
    if columns is None:
        import os

        try:
            columns = os.get_terminal_size().columns
        except (OSError, ValueError):
            columns = int(os.environ.get("COLUMNS") or 0) or DEFAULT_WIDTH
    return max(40, min(int(columns), MAX_BODY_WIDTH))


# ---------------------------------------------------------------------------
# 基本块
# ---------------------------------------------------------------------------


def wrap_body(text: str, width: int | None = None) -> list[str]:
    """把正文按宽度折行（保留空行）。"""
    limit = width or output_width()
    lines: list[str] = []
    for raw in text.split("\n"):
        if raw == "":
            lines.append("")
            continue
        lines.extend(wrap_plain(raw, limit))
    return lines or [""]


def _border_line(top: bool, width: int) -> str:
    """`╭─…─╮` / `╰─…─╯`，等宽 `width`。"""
    inner = max(0, width - 2)
    left = BOX_TOP_LEFT if top else BOX_BOTTOM_LEFT
    right = BOX_TOP_RIGHT if top else BOX_BOTTOM_RIGHT
    return (
        paint(left, COLOR_STATUS)
        + paint(BOX_HORIZONTAL * inner, COLOR_STATUS)
        + paint(right, COLOR_STATUS)
    )


def _panel_row(left: str, width: int, right: str = "") -> str:
    """框内一行：`│ ` + left + 填充 + right + ` │`，等宽 `width`。

    对应原版 `panelRow()`（`tui/chrome.ts`）：`inner = width - 4`，
    左右各有 1 列内边距，所以**总宽仍是 `width`**。
    """
    inner = max(0, width - 4)
    left_width = string_display_width(left)
    right_width = string_display_width(right)
    gap = max(1, inner - left_width - right_width) if right else 0
    shown = left
    if left_width + right_width + gap > inner:
        shown = truncate_plain(left, max(0, inner - right_width - 1))
    pad = max(0, inner - string_display_width(shown) - right_width)
    return (
        paint(BOX_VERTICAL, COLOR_STATUS)
        + " "
        + shown
        + " " * pad
        + right
        + " "
        + paint(BOX_VERTICAL, COLOR_STATUS)
    )


def _color_badge(label: str, value: str, color: str) -> str:
    """`[label] value`。对应原版 `colorBadge()`。"""
    return paint(f"[{label}]", color) + " " + paint(value, BOLD)


def _join_segments_within_width(
    segments: list[str], separator: str, max_width: int
) -> str:
    """把徽章拼进给定宽度，放不下时以 ` ...` 收尾。

    对应原版 `joinSegmentsWithinWidth()`。逐段累加，第一段就放不下时**截断它**；
    否则尝试带省略号，还放不下就停在上一段。
    """
    if max_width <= 0 or not segments:
        return ""

    output = ""
    for segment in segments:
        candidate = f"{output}{separator}{segment}" if output else segment
        if string_display_width(candidate) <= max_width:
            output = candidate
            continue
        if not output:
            return truncate_plain(strip_ansi(segment), max_width)
        with_ellipsis = f"{output}{separator}{paint('...', DIM)}"
        if string_display_width(with_ellipsis) <= max_width:
            return with_ellipsis
        return output
    return output


def render_banner(
    model: str,
    session_id: str,
    cwd: str,
    *,
    tool_count: int = 0,
    readonly_tool_count: int = 0,
    skill_count: int = 0,
    mcp_summary: str = "",
    provider: str = "",
    message_count: int = 1,
    event_count: int = 0,
    mcp_total: int = 0,
    mcp_connected: int = 0,
    mcp_connecting: int = 0,
    mcp_error: int = 0,
    width: int | None = None,
) -> str:
    """启动横幅。**布局与字段逐条对齐原版**（`tui/chrome.ts` 的 `renderBanner`）。

    ## 原版的结构

        ╭─(width-2)─╮
        │ MiniCode
        │                         ← 标题后固定一个空行
        │ [session] local  [provider] offline  [model] …  [skills] 0  [mcp] 0/0
        ╰─(width-2)─╯

    中间那行是**徽章串**，字段顺序固定：

        session(local) · provider · model · messages · events
        · [context] · skills · mcp=connected/total · [mcp-wait] · [mcp-err]

    面板宽度 = `max(60, 终端列数 ?? 100)`。

    ## 早先这里是另一套排版，而且我给它写了个错误的理由

    旧实现是「中文键值对」：模型 / 会话 / 目录 / 工具。docstring 里写着
    「原版把 MCP 计数放在徽章行里，本实现放在键值行里——**信息量一致**」。

    并不一致。缺了 `provider`、`messages`、`events`，多出「目录」「工具」。
    横幅是用户启动后看到的第一样东西，字段不对等于「这个工具报告的事和原版
    不一样」。原版的徽章行里 `provider` 尤其重要：它告诉用户这次连的是
    哪个端点、还是离线。

    ## 参数

    `session_id` / `cwd` / `tool_count` / `mcp_summary` 保留但**不再展示**：
    原版的徽章行里没有这三项（`_cwd` 与 `_permissionSummary` 在原版签名里
    就是下划线开头的未用参数）。留着是为了不破坏调用方，注释说明它们不参与渲染。
    """
    # **横幅有自己的宽度规则**，与正文不同：
    #
    #     原版：Math.max(60, process.stdout.columns ?? 100)
    #
    # 两者不一样：正文宽度被 `MAX_BODY_WIDTH` 封顶（长段落超过 100 列很难读），
    # 而横幅**不封顶**——终端有多宽就用多宽，并且有 60 列下限。
    #
    # 早先这里复用了 `output_width()`，于是 200 列的终端也只画 100 列宽的框，
    # 与原版不一致。
    panel_width = max(60, width if width is not None else (terminal_columns() or 100))
    inner = max(0, panel_width - 4)

    shown_model = model or "not-configured"
    shown_provider = _provider_label(provider)

    badges = [
        _color_badge("session", "local", BRIGHT_YELLOW),
        _color_badge("provider", shown_provider, CYAN),
        _color_badge("model", shown_model, GREEN),
        _color_badge("messages", str(message_count), BRIGHT_CYAN),
        _color_badge("events", str(event_count), BLUE),
        _color_badge("skills", str(skill_count), BRIGHT_GREEN),
        _color_badge("mcp", f"{mcp_connected}/{mcp_total}", MAGENTA),
    ]
    if mcp_connecting > 0:
        badges.append(_color_badge("mcp-wait", str(mcp_connecting), YELLOW))
    if mcp_error > 0:
        badges.append(_color_badge("mcp-err", str(mcp_error), BRIGHT_RED))

    meta_line = _join_segments_within_width(badges, "  ", inner)

    title = paint("MiniCode", COLOR_BANNER_TITLE)
    return "\n".join(
        [
            _border_line(True, panel_width),
            _panel_row(title, panel_width),
            _panel_row("", panel_width),
            _panel_row(meta_line, panel_width),
            _border_line(False, panel_width),
        ]
    )


def _provider_label(provider: str) -> str:
    """`provider` 徽章的值。

    对应原版：

        runtime?.baseUrl
          ? runtime.baseUrl.replace(/^https?:\\/\\//, '').split('/')[0] || 'custom'
          : 'offline'

    也就是「有 baseUrl 就取主机名，否则 `offline`」。
    """
    raw = (provider or "").strip()
    if not raw:
        return "offline"
    without_scheme = raw.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0]
    return host or "custom"


def render_user(text: str, width: int | None = None) -> str:
    """回显用户输入：`> 内容`，后续行缩进对齐。"""
    limit = width or output_width()
    prefix = paint(GLYPH_USER + " ", COLOR_USER)
    prefix_width = string_display_width(GLYPH_USER + " ")
    lines = wrap_body(text, max(10, limit - prefix_width))
    rendered = [
        (prefix if index == 0 else " " * prefix_width) + paint(line, COLOR_USER)
        for index, line in enumerate(lines)
    ]
    return "\n".join(rendered)


def render_assistant(text: str, width: int | None = None) -> str:
    """助手正文。

    刻意**不加前缀、不用彩色**：正文是阅读量最大的部分，终端默认前景色
    最耐看，加装饰反而干扰。
    """
    limit = width or output_width()
    return "\n".join(
        paint(line, COLOR_ASSISTANT) if COLOR_ASSISTANT else line
        for line in wrap_body(text, limit)
    )


def render_progress(text: str, width: int | None = None) -> str:
    """中途进度：`· 内容`，整体压暗，与最终答复区分开。"""
    limit = width or output_width()
    prefix = paint(GLYPH_BULLET + " ", COLOR_PROGRESS)
    lines = wrap_body(text, max(10, limit - 2))
    return "\n".join(
        (prefix if index == 0 else "  ") + paint(line, COLOR_PROGRESS)
        for index, line in enumerate(lines)
    )


def render_tool_start(name: str, detail: str = "", width: int | None = None) -> str:
    """工具调用开始：`⏺ 工具名`。"""
    limit = width or output_width()
    head = f"{paint(GLYPH_TOOL, COLOR_TOOL)} {paint(name, COLOR_TOOL)}"
    if detail:
        head += " " + paint(detail, COLOR_HINT)
    return truncate_plain(head, limit)


def render_tool_result(
    summary: str, *, ok: bool = True, detail: str = "", width: int | None = None
) -> str:
    """工具结果：`  ⎿ 摘要`。

    `⎿` 是 Claude Code 最有辨识度的记号——它让「结果属于上面那次调用」
    在视觉上一目了然，而不需要额外的缩进层级或分隔线。
    """
    limit = width or output_width()
    indent = "  " + paint(GLYPH_RESULT, COLOR_STATUS) + " "
    available = max(10, limit - string_display_width("  ⎿ "))
    color = COLOR_TOOL_OK if ok else COLOR_TOOL_FAIL
    lines = [indent + paint(truncate_plain(summary, available), color)]
    if detail:
        for piece in wrap_body(detail, max(10, available - 2)):
            lines.append("    " + paint(piece, COLOR_HINT))
    return "\n".join(lines)


def render_notice(text: str, width: int | None = None) -> str:
    limit = width or output_width()
    return "\n".join(paint(line, COLOR_NOTICE) for line in wrap_body(text, limit))


def render_error(text: str, width: int | None = None) -> str:
    limit = width or output_width()
    return "\n".join(paint(line, COLOR_ERROR) for line in wrap_body(text, limit))


def render_question(text: str, width: int | None = None) -> str:
    """模型的反问：用方框框起来，与普通输出明确区分。

    这是 `ask_user` 的展示。行模式没有可交互的待批准面板，因此必须靠视觉
    上的强区分来告诉用户「现在需要你回答」。用普通工具结果的样式显示会让
    人以为程序卡住了。
    """
    limit = width or output_width()
    inner = max(20, limit - 2)

    body: list[str] = []
    for raw in text.split("\n"):
        if raw == "":
            body.append("")
            continue
        for piece in wrap_plain(raw, max(10, inner - 2)):
            body.append(piece)

    rule = paint(BOX_HORIZONTAL * inner, COLOR_NOTICE)
    top = paint(BOX_TOP_LEFT, COLOR_NOTICE) + rule + paint(BOX_TOP_RIGHT, COLOR_NOTICE)
    bottom = (
        paint(BOX_BOTTOM_LEFT, COLOR_NOTICE) + rule + paint(BOX_BOTTOM_RIGHT, COLOR_NOTICE)
    )
    rows = []
    for line in body:
        pad = " " * max(0, inner - 2 - string_display_width(line))
        rows.append(
            paint(BOX_VERTICAL, COLOR_NOTICE)
            + " "
            + paint(line, COLOR_USER)
            + pad
            + " "
            + paint(BOX_VERTICAL, COLOR_NOTICE)
        )
    return "\n".join([top, *rows, bottom])


def render_status_line(
    *,
    model: str = "",
    context_percent: int | None = None,
    mcp_summary: str = "",
    spinner_frame: int | None = None,
    label: str = "",
) -> str:
    """状态行：转圈 + 文本 · 模型 · 上下文占用 · MCP。

    `spinner_frame=None` 表示不显示转圈（静态状态）。
    """
    parts: list[str] = []
    if spinner_frame is not None:
        glyph = SPINNER[spinner_frame % len(SPINNER)]
        parts.append(paint(f"{glyph} {label or '处理中…'}", COLOR_STATUS))
    elif label:
        parts.append(paint(label, COLOR_STATUS))
    if model:
        parts.append(paint(model, COLOR_HINT))
    if context_percent is not None:
        parts.append(paint(f"ctx {context_percent}%", COLOR_HINT))
    if mcp_summary:
        parts.append(paint(mcp_summary, COLOR_HINT))
    separator = paint(f"  {GLYPH_BULLET}  ", COLOR_HINT)
    return separator.join(parts)


def render_separator(width: int | None = None) -> str:
    """回合之间的细分隔线。比空行更能标示边界，又不喧宾夺主。"""
    limit = width or output_width()
    return paint("─" * min(limit, 60), COLOR_HINT)


def render_help(commands: list[tuple[str, str]], width: int | None = None) -> str:
    """命令列表：左对齐用法、右对齐说明。"""
    limit = width or output_width()
    if not commands:
        return paint("没有可用命令。", COLOR_HINT)
    usage_width = max(string_display_width(usage) for usage, _ in commands)
    lines: list[str] = []
    for usage, description in commands:
        pad = " " * (usage_width - string_display_width(usage))
        line = f"  {paint(usage, COLOR_USER)}{pad}  {paint(description, COLOR_HINT)}"
        lines.append(truncate_plain(line, limit))
    return "\n".join(lines)


def render_key_value(
    rows: list[tuple[str, str]], width: int | None = None, *, wrap_values: bool = False
) -> str:
    """对齐的键值列表（给 /status、/plan、/config-paths 之类复用）。

    `wrap_values=False`（默认）把过长的值**截断**——适合 `/status` 那种
    「值是一句摘要」的场景，截断后仍能看懂。

    `wrap_values=True` 改为**折行**。给路径这类值用：`/config-paths` 的全部
    意义就是让用户看到完整路径去复制，截断成 `C:\\Users\\...\\se...` 会让这条
    命令彻底没用。折行的代价是占更多屏幕，但那是值得的。
    """
    limit = width or output_width()
    if not rows:
        return ""
    key_width = max(string_display_width(key) for key, _ in rows)
    # 每行前缀：两空格 + 键 + 补齐 + 两空格
    prefix_width = 2 + key_width + 2
    lines: list[str] = []
    for key, value in rows:
        pad = " " * (key_width - string_display_width(key))
        head = f"  {paint(key, COLOR_HINT)}{pad}  "
        if not wrap_values:
            lines.append(truncate_plain(head + value, limit))
            continue
        available = max(10, limit - prefix_width)
        pieces = wrap_plain(value, available) or [""]
        lines.append(head + pieces[0])
        # 续行缩进到与首行值对齐，读起来才知道它属于同一个键。
        continuation = " " * prefix_width
        for piece in pieces[1:]:
            lines.append(continuation + piece)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 流式输出
# ---------------------------------------------------------------------------


@dataclass
class StreamPrinter:
    """把增量文本拼成段落输出。

    模型是**流式**返回的：回调可能被调用很多次，每次都只是一小段。直接
    逐段 `print` 会把一句话拆得七零八落，所以这里按行缓冲——遇到换行才
    真正输出，最后 `flush()` 把残留的尾巴吐出来。

    这类「看起来只是打印」的代码最容易在小地方出错（重复输出、丢尾、
    空行处理），因此逻辑单独成类并配了测试。
    """

    width: int = DEFAULT_WIDTH
    color: str = COLOR_ASSISTANT
    indent: str = ""
    _buffer: str = ""
    _printed_any: bool = False

    def feed(self, chunk: str) -> str:
        """喂入一段增量文本，返回**现在就该输出**的内容（可能为空串）。"""
        if not chunk:
            return ""
        self._buffer += chunk
        if "\n" not in self._buffer:
            return ""

        complete, _, self._buffer = self._buffer.rpartition("\n")
        self._printed_any = True
        return self._render(complete)

    def flush(self) -> str:
        """把残留的尾巴输出掉。回合结束时必须调用。"""
        if not self._buffer:
            return ""
        tail, self._buffer = self._buffer, ""
        self._printed_any = True
        return self._render(tail)

    @property
    def has_pending(self) -> bool:
        return bool(self._buffer)

    def _render(self, text: str) -> str:
        lines = wrap_body(text, max(10, self.width - string_display_width(self.indent)))
        return "\n".join(
            self.indent + (paint(line, self.color) if self.color else line)
            for line in lines
        )


def supports_color(stream: Any = None) -> bool:
    """是否该给这个流上色。

    除了 `NO_COLOR`，还要确认目标**是个终端**：重定向到文件或管道时输出
    转义序列会污染文件内容。这与 TUI 不同——行模式必须能在管道里安全使用。
    """
    if not color_enabled():
        return False
    if stream is None:
        import sys

        stream = sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False
