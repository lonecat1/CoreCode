"""实时区域的组装与按键处理 —— 仿 Claude Code 的视觉布局。

## 布局（自下而上）

    ╭──────────────────────────────────────╮
    │ > 用户正在输入…                      │   ← 圆角输入框
    ╰──────────────────────────────────────╯
    ⠋ thinking…  ·  qwen3.8-27b  ·  ctx 12%     ← 状态行

    以上是「固定底部」。其上依次是转录区：

    > 帮我看看这个文件
                                               ← 消息之间空一行
    ⏺ read_file
      ⎿ 读取了 pyproject.toml
    这是助手的回答正文…

## 两条硬性约束

**① 每一行的可见宽度必须 ≤ 终端列数。**
内联重绘靠「行数记账」定位：区域有几行，就上移几行。若某行超宽，终端会把
它自动折成两行，而我们的记账仍按一行算——光标就会移错位置，画面逐帧错乱。
因此组装完成后有一道**强制截断**（`clamp_width`），它是不变式而非美化。

**② 区域高度必须 ≤ 终端行数 - 1。**
留一行给退出后的 shell 提示符，同时避免区域顶到屏幕顶端。

这两条把「绝对定位方案」里那个「帧高必须恰好等于终端高」的脆弱前提彻底
去掉了：内联模式只需要知道**自己有几行**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    GLYPH_FAIL,
    GLYPH_OK,
    GLYPH_PROMPT,
    GLYPH_RESULT,
    GLYPH_TOOL,
    GLYPH_USER,
    SPINNER,
    paint,
)
from .width import string_display_width, truncate_plain, wrap_plain

#: 区域高度上下限。
MIN_REGION_LINES = 8
MAX_REGION_LINES = 40

#: 底部固定部分的行数：输入框 3 行 + 状态行 1 行。
CHROME_LINES = 4

#: 转录区至少留几行。
MIN_TRANSCRIPT_LINES = 2

MAX_TRANSCRIPT_ENTRIES = 2000
MAX_HISTORY = 200

#: 斜杠命令候选（`/help` 会列出全部）。
_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "显示可用命令"),
    ("/tools", "列出可用工具"),
    ("/status", "显示状态"),
    ("/plan", "显示 Todo 清单"),
    ("/goal", "创建/控制 Goal"),
    ("/loop", "创建/停止 Loop"),
    ("/memory", "显示已加载的指令文件"),
    ("/skills", "列出技能"),
    ("/mcp", "显示 MCP 服务器"),
    ("/permissions", "显示权限路径"),
    ("/resume", "恢复会话"),
    ("/rename", "重命名会话"),
    ("/fork", "分叉会话"),
    ("/new", "开新会话"),
    ("/compact", "压缩上下文"),
    ("/collapse", "折叠旧上下文"),
    ("/snip", "剪裁中间历史"),
    ("/exit", "退出"),
)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


@dataclass
class TranscriptEntry:
    """一条转录记录。"""

    kind: str  # user | assistant | progress | tool | notice | error
    text: str
    ok: bool | None = None
    detail: str = ""

    def render(self, width: int) -> list[str]:
        """渲染成若干行，保证每行 ≤ width。"""
        limit = max(8, width)

        if self.kind == "user":
            return _prefixed(paint(GLYPH_USER + " ", COLOR_USER), self.text, limit, COLOR_USER)

        if self.kind == "assistant":
            # 正文用终端默认色，最耐看；不加前缀，靠空行与上一条分隔。
            return _plain(self.text, limit, COLOR_ASSISTANT)

        if self.kind == "progress":
            return _prefixed(
                paint(GLYPH_BULLET + " ", COLOR_PROGRESS), self.text, limit, COLOR_PROGRESS
            )

        if self.kind == "tool":
            # Claude Code 的记号：`⏺` 是工具调用**始终**的起头，状态靠颜色
            # 表达（运行中=青、成功=绿、失败=红），结果用 `⎿` 缩进承接。
            # 早先我按状态换字形（✓/✗），那会让「这是哪类条目」变得不稳定。
            if self.ok is None:
                color = COLOR_TOOL
            elif self.ok:
                color = COLOR_TOOL_OK
            else:
                color = COLOR_TOOL_FAIL
            lines = _clamp(f"{paint(GLYPH_TOOL, color)} {paint(self.text, color)}", limit)
            if self.detail:
                indent = "  " + paint(GLYPH_RESULT, COLOR_STATUS) + " "
                lines.extend(_prefixed(indent, self.detail, limit, COLOR_STATUS))
            return lines

        if self.kind == "notice":
            return _plain(self.text, limit, COLOR_NOTICE)

        if self.kind == "error":
            return _plain(self.text, limit, COLOR_ERROR)

        return _plain(self.text, limit, "")


def _clamp(line: str, width: int) -> list[str]:
    """硬截断到宽度内（不折行）。用于本来就该占一行的内容。"""
    if string_display_width(line) <= width:
        return [line]
    return [truncate_plain(line, width)]


def _plain(text: str, width: int, color: str) -> list[str]:
    """按宽度折行，可选上色。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        if raw == "":
            lines.append("")
            continue
        for piece in wrap_plain(raw, width):
            lines.append(paint(piece, color) if color else piece)
    return lines or [""]


def _prefixed(prefix: str, text: str, width: int, color: str) -> list[str]:
    """带前缀的折行：首行带前缀，后续行缩进对齐。"""
    prefix_width = string_display_width(prefix)
    available = max(4, width - prefix_width)
    indent = " " * prefix_width

    lines: list[str] = []
    for raw in text.split("\n"):
        if raw == "":
            lines.append("")
            continue
        for index, piece in enumerate(wrap_plain(raw, available)):
            body = paint(piece, color) if color else piece
            lines.append((prefix if index == 0 else indent) + body)
    return lines or [""]


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


@dataclass
class BannerStats:
    """横幅数据。刻意用普通数据而非宿主对象，方便测试。"""

    model: str = "未配置"
    session_id: str = ""
    cwd: str = ""
    message_count: int = 0
    tool_count: int = 0
    skill_count: int = 0
    mcp_total: int = 0
    mcp_connected: int = 0
    mcp_error: int = 0
    context_percent: int | None = None


@dataclass
class AppState:
    """界面全部可变状态。纯数据，没有终端依赖。"""

    banner: BannerStats = field(default_factory=BannerStats)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    input: str = ""
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    history_index: int = -1
    history_draft: str = ""
    scroll_offset: int = 0
    status: str | None = None
    animation_frame: int = 0
    running: bool = False
    hint: str = "Enter 发送 · Ctrl-C 中断 · Ctrl-D 退出 · ↑↓ 历史"
    last_transcript_lines: int = 0
    show_banner: bool = True

    # -- 记录 ---------------------------------------------------------------

    def push(
        self, kind: str, text: str, *, ok: bool | None = None, detail: str = ""
    ) -> None:
        self.transcript.append(
            TranscriptEntry(kind=kind, text=text, ok=ok, detail=detail)
        )
        if len(self.transcript) > MAX_TRANSCRIPT_ENTRIES:
            del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT_ENTRIES]

    def update_running_tool(self, text: str, ok: bool | None, detail: str = "") -> bool:
        for entry in reversed(self.transcript):
            if entry.kind == "tool" and entry.ok is None:
                entry.text = text
                entry.ok = ok
                if detail:
                    entry.detail = detail
                return True
        return False

    # -- 输入 ---------------------------------------------------------------

    def set_input(self, text: str) -> None:
        self.input = text
        self.cursor = len(text)

    def previous_history(self) -> None:
        if not self.history:
            return
        if self.history_index == -1:
            self.history_draft = self.input
            self.history_index = len(self.history) - 1
        elif self.history_index > 0:
            self.history_index -= 1
        self.set_input(self.history[self.history_index])

    def next_history(self) -> None:
        if self.history_index == -1:
            return
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.set_input(self.history[self.history_index])
        else:
            self.history_index = -1
            self.set_input(self.history_draft)

    def remember(self, text: str) -> None:
        if not text.strip():
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        if len(self.history) > MAX_HISTORY:
            del self.history[: len(self.history) - MAX_HISTORY]
        self.history_index = -1
        self.history_draft = ""

    # -- 滚动 ---------------------------------------------------------------

    def scroll(self, delta: int, viewport: int) -> None:
        maximum = max(0, self.last_transcript_lines - viewport)
        self.scroll_offset = max(0, min(maximum, self.scroll_offset + delta))

    def jump_to_bottom(self) -> None:
        self.scroll_offset = 0

    # -- 斜杠菜单 -----------------------------------------------------------

    def slash_matches(self) -> list[tuple[str, str]]:
        if not self.input.startswith("/") or " " in self.input:
            return []
        return [
            (usage, desc) for usage, desc in _COMMANDS if usage.startswith(self.input)
        ]


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


@dataclass
class Region:
    """实时区域的组装结果。"""

    lines: list[str]
    #: 光标应处的**区域内行号**（0 基）与该行列号（1 基）。
    cursor_row: int = 0
    cursor_column: int = 1

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def clamp_width(lines: list[str], columns: int) -> list[str]:
    """**不变式**：把每一行截断到终端宽度内。

    内联重绘靠行数记账定位；任何一行超宽都会被终端自动折行，而我们的记账
    仍按一行算，于是光标移错位置、画面逐帧错乱。因此这不是美化，是正确性
    前提——组装完成后必须过这一道。
    """
    limit = max(1, columns)
    return [
        line if string_display_width(line) <= limit else truncate_plain(line, limit)
        for line in lines
    ]


def compute_region_height(rows: int) -> int:
    """实时区域的总高度。

    上限 `rows - 1`：留一行给退出后的 shell 提示符，也避免顶到屏幕顶端。
    """
    return max(MIN_REGION_LINES, min(MAX_REGION_LINES, max(1, rows - 1)))


def transcript_viewport(rows: int) -> int:
    """转录区能显示几行。"""
    return max(MIN_TRANSCRIPT_LINES, compute_region_height(rows) - CHROME_LINES)


def compose_transcript_lines(state: AppState, width: int) -> list[str]:
    """把全部转录记录渲染成行。"""
    lines: list[str] = []
    for entry in state.transcript:
        if lines and entry.kind in ("user", "assistant"):
            lines.append("")  # 消息之间空一行，便于阅读
        lines.extend(entry.render(width))
    return lines


def render_input_box(state: AppState, width: int) -> list[str]:
    """圆角输入框（Claude Code 的标志性外观）。

    每行都**精确**是 `width` 列宽：内宽 `width - 2` 留给内容，两侧各一个
    边框字符。宽度必须精确——内联重绘靠行数记账定位，任何一行超宽都会让
    终端自动折行、把记账打乱。
    """
    inner = max(2, width - 2)
    label = paint(GLYPH_PROMPT + " ", COLOR_USER)

    # 内容区可用宽度 = 内宽 - 两侧各一个空格
    available = max(1, inner - 2 - string_display_width(label))
    shown = state.input
    if string_display_width(shown) > available:
        # 只显示尾部：光标总在末尾附近。
        shown = truncate_plain(shown, available) if available > 3 else shown[-available:]

    content = label + paint(shown, COLOR_USER)
    padding = " " * max(0, inner - 2 - string_display_width(label + shown))

    rule = paint(BOX_HORIZONTAL * inner, COLOR_STATUS)
    top = paint(BOX_TOP_LEFT, COLOR_STATUS) + rule + paint(BOX_TOP_RIGHT, COLOR_STATUS)
    bottom = (
        paint(BOX_BOTTOM_LEFT, COLOR_STATUS)
        + rule
        + paint(BOX_BOTTOM_RIGHT, COLOR_STATUS)
    )
    middle = (
        paint(BOX_VERTICAL, COLOR_STATUS)
        + " "
        + content
        + padding
        + " "
        + paint(BOX_VERTICAL, COLOR_STATUS)
    )
    return [top, middle, bottom]


def render_status_line(state: AppState, width: int, *, mcp_waiting: bool = False) -> str:
    """状态行：转圈 + 状态文本 · 模型 · 上下文占用 · MCP。

    空闲时显示操作提示，而不是只显示模型名——用户更需要知道「能按什么」。
    """
    banner = state.banner

    if state.running or mcp_waiting:
        spinner = SPINNER[state.animation_frame % len(SPINNER)]
        text = state.status or ("MCP 连接中" if mcp_waiting else "思考中…")
        head = paint(f"{spinner} {text}", COLOR_STATUS)
    elif state.status:
        head = paint(state.status, COLOR_STATUS)
    else:
        # 空闲：提示语优先，模型名放在后面作为背景信息。
        head = paint(state.hint, COLOR_HINT)

    parts: list[str] = [head]
    if banner.model and banner.model != "未配置":
        parts.append(paint(banner.model, COLOR_HINT))
    if banner.context_percent is not None:
        parts.append(paint(f"ctx {banner.context_percent}%", COLOR_HINT))
    if banner.mcp_total:
        mark = f"mcp {banner.mcp_connected}/{banner.mcp_total}"
        parts.append(paint(mark, COLOR_ERROR if banner.mcp_error else COLOR_HINT))

    separator = paint(f"  {GLYPH_BULLET}  ", COLOR_HINT)
    return truncate_plain(separator.join(parts), width)


def render_banner(state: AppState, width: int) -> str:
    """启动横幅：一行标题 + 计数。"""
    banner = state.banner
    session = banner.session_id or "-"
    left = paint("MiniCode", COLOR_BANNER_TITLE) + "  " + paint(f"session {session}", COLOR_HINT)
    if banner.tool_count:
        left += paint(f"  tools {banner.tool_count}", COLOR_HINT)
    if banner.skill_count:
        left += paint(f"  skills {banner.skill_count}", COLOR_HINT)
    return truncate_plain(left, width)


def compose_region(
    state: AppState,
    columns: int,
    rows: int,
    *,
    mcp_waiting: bool = False,
    banner: bool | None = None,
) -> Region:
    """组装实时区域。"""
    width = max(20, columns)
    height = compute_region_height(rows)
    viewport = transcript_viewport(rows)
    want_banner = state.show_banner if banner is None else banner

    # -- 底部固定部分 -------------------------------------------------------
    bottom = [*render_input_box(state, width), render_status_line(state, width, mcp_waiting=mcp_waiting)]

    # -- 转录区 -------------------------------------------------------------
    all_lines = compose_transcript_lines(state, width)
    state.last_transcript_lines = len(all_lines)

    head: list[str] = []
    if want_banner:
        head = [render_banner(state, width), ""]

    available = max(MIN_TRANSCRIPT_LINES, height - len(bottom) - len(head))

    if not all_lines:
        visible = [paint("输入一条消息开始。", COLOR_HINT)]
    elif state.scroll_offset <= 0:
        visible = all_lines[-available:]
    else:
        end = max(0, len(all_lines) - state.scroll_offset)
        start = max(0, end - available)
        visible = [
            paint(f"↑ 已上翻 {state.scroll_offset} 行 · End 回到底部", COLOR_HINT),
            *all_lines[start + 1 : end],
        ]

    # 转录区补空行：底部固定部分始终贴底，输入框不随内容多少上下跳。
    pad = max(0, available - len(visible))
    lines = [*head, *[""] * pad, *visible, *bottom]
    lines = clamp_width(lines, width)

    # 光标落在输入框的内容行上（底部固定部分的第二行）。
    cursor_row = len(lines) - len(bottom) + 1
    label_width = string_display_width(GLYPH_PROMPT + " ")
    inner = max(2, width - 2)
    available_input = max(1, inner - 2 - label_width)
    shown = state.input
    if string_display_width(shown) > available_input:
        shown = shown[-available_input:]
    # 列号 = 边框(1) + 空格(1) + 提示符 + 已输入内容 + 1。
    # 那个 +1 是「光标停在最后一个字符**之后**」，不是压在它上面。
    cursor_column = min(
        width, 2 + label_width + string_display_width(shown) + 1
    )

    return Region(lines=lines, cursor_row=cursor_row, cursor_column=cursor_column)


# ---------------------------------------------------------------------------
# 兼容层：旧的「整屏文本 + 绝对光标」接口
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """整屏文本 + 1 基的绝对光标位置。"""

    text: str
    cursor: tuple[int, int] = (1, 1)


def compose_frame(state: AppState, size: Any, *, mcp_waiting: bool = False) -> Frame:
    """兼容旧接口。新代码应直接用 `compose_region`（它给区域内相对坐标）。"""
    region = compose_region(state, size.columns, size.rows, mcp_waiting=mcp_waiting)
    return Frame(text=region.text, cursor=(region.cursor_row + 1, region.cursor_column))


def cursor_position(state: AppState, size: Any) -> tuple[int, int]:
    return compose_frame(state, size).cursor


# ---------------------------------------------------------------------------
# 按键
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


@dataclass
class KeyOutcome:
    """处理一个键的结果。"""

    submit: str | None = None
    interrupt: bool = False
    quit: bool = False
    redraw: bool = True


def handle_key(state: AppState, key: str, size: Any) -> KeyOutcome:
    """把一个键作用到状态上。纯函数式的状态变更 + 返回需要执行的动作。"""
    if key == KEY_ENTER:
        text = state.input
        if not text.strip():
            return KeyOutcome()
        state.remember(text)
        state.set_input("")
        state.jump_to_bottom()
        return KeyOutcome(submit=text)

    if key == KEY_CTRL_C:
        state.set_input("")
        return KeyOutcome(interrupt=True)

    if key == KEY_CTRL_D:
        return KeyOutcome(quit=True) if not state.input else KeyOutcome()

    if key == KEY_BACKSPACE:
        if state.cursor > 0:
            state.input = state.input[: state.cursor - 1] + state.input[state.cursor :]
            state.cursor -= 1
        return KeyOutcome()

    if key == KEY_LEFT:
        state.cursor = max(0, state.cursor - 1)
        return KeyOutcome()

    if key == KEY_RIGHT:
        state.cursor = min(len(state.input), state.cursor + 1)
        return KeyOutcome()

    if key == KEY_HOME:
        state.cursor = 0
        return KeyOutcome()

    if key == KEY_END:
        state.cursor = len(state.input)
        state.jump_to_bottom()
        return KeyOutcome()

    if key == KEY_UP:
        state.previous_history()
        return KeyOutcome()

    if key == KEY_DOWN:
        state.next_history()
        return KeyOutcome()

    if key == KEY_PAGE_UP:
        viewport = transcript_viewport(size.rows)
        state.scroll(viewport, viewport)
        return KeyOutcome()

    if key == KEY_PAGE_DOWN:
        viewport = transcript_viewport(size.rows)
        state.scroll(-viewport, viewport)
        return KeyOutcome()

    if key == KEY_ESCAPE:
        if state.input:
            state.set_input("")
        return KeyOutcome()

    if key == KEY_TAB:
        matches = state.slash_matches()
        if matches:
            state.set_input(_common_prefix([usage for usage, _desc in matches]))
        return KeyOutcome()

    if key is None:
        return KeyOutcome(redraw=False)

    if len(key) == 1 and key.isprintable():
        state.input = state.input[: state.cursor] + key + state.input[state.cursor :]
        state.cursor += 1
        return KeyOutcome()

    return KeyOutcome(redraw=False)


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while not value.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
