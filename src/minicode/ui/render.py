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

from dataclasses import dataclass
from typing import Any

from .theme import (
    BOX_BOTTOM_LEFT,
    BOX_BOTTOM_RIGHT,
    BOX_HORIZONTAL,
    BOX_TOP_LEFT,
    BOX_TOP_RIGHT,
    BOX_VERTICAL,
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


def render_banner(
    model: str,
    session_id: str,
    cwd: str,
    *,
    tool_count: int = 0,
    readonly_tool_count: int = 0,
    skill_count: int = 0,
    mcp_summary: str = "",
    width: int | None = None,
) -> str:
    """启动横幅：圆角框 + 关键信息。

    只在会话开始时打印一次，之后不再重复——行模式的历史由终端滚动缓冲负责。

    `mcp_summary` 由 `mcp.status.summarize_mcp_servers(...).line()` 产生。
    原版把 MCP 计数放在横幅的徽章行里（`tui/chrome.ts:286-296`），本实现放在
    键值行里——行模式没有徽章，但信息量一致。
    """
    limit = width or output_width()
    inner = max(20, limit - 2)

    rows: list[tuple[str, str]] = [
        ("模型", model),
        ("会话", session_id),
        ("目录", cwd),
    ]
    if tool_count:
        detail = f"{tool_count} 个"
        if readonly_tool_count:
            detail += f"（子 Agent 可见 {readonly_tool_count} 个只读工具）"
        rows.append(("工具", detail))
    if skill_count:
        rows.append(("技能", f"{skill_count} 个"))
    if mcp_summary:
        rows.append(("MCP", mcp_summary))

    # **每一行的「框内内容」必须正好占满 `inner` 列。**
    #
    # 早先这里按 `inner - 2 - label_width - 2` 算可用宽度，比正确值少 1，
    # 于是内容行宽 75、边框行宽 76——右边框每一行都错开一列。启动横幅是用户
    # 看到的第一样东西，错位很显眼。
    #
    # 正确的口径：框内 = `" " + 标签 + 补齐 + "  " + 内容 + 填充`，总宽必须
    # 等于 `inner`。下面用 `_fill()` 统一收口，不再各处手算。
    label_width = max(string_display_width(label) for label, _ in rows)
    body: list[str] = []
    for label, value in rows:
        head = " " + label + " " * (label_width - string_display_width(label)) + "  "
        available = max(10, inner - string_display_width(head))
        shown = truncate_plain(value, available)
        body.append(
            paint(BOX_VERTICAL, COLOR_STATUS)
            + " "
            + paint(label, COLOR_HINT)
            + " " * (label_width - string_display_width(label))
            + "  "
            + shown
            + " " * max(0, inner - string_display_width(head) - string_display_width(shown))
            + paint(BOX_VERTICAL, COLOR_STATUS)
        )

    title = paint("MiniCode", COLOR_BANNER_TITLE)
    hint = paint("终端编码助手", COLOR_HINT)
    title_head = " MiniCode  "
    title_tail = "终端编码助手"
    title_line = (
        paint(BOX_VERTICAL, COLOR_STATUS)
        + " "
        + title
        + "  "
        + hint
        + " " * max(0, inner - string_display_width(title_head + title_tail))
        + paint(BOX_VERTICAL, COLOR_STATUS)
    )

    rule = paint(BOX_HORIZONTAL * inner, COLOR_STATUS)
    top = paint(BOX_TOP_LEFT, COLOR_STATUS) + rule + paint(BOX_TOP_RIGHT, COLOR_STATUS)
    bottom = (
        paint(BOX_BOTTOM_LEFT, COLOR_STATUS) + rule + paint(BOX_BOTTOM_RIGHT, COLOR_STATUS)
    )
    return "\n".join([top, title_line, *body, bottom])


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
