"""TUI 应用状态与打字机输入。

关键决定：**状态与按键处理都不碰终端**。

`AppState` 是纯数据；`handle_key()` 是纯函数（状态 + 键 → 新状态 + 命令）；
`compose_frame()` 把状态渲染成整屏文本。只有 `run_tui()` 里那不到 40 行
真正接触终端。

这样「输入行编辑、历史浏览、滚动、斜杠菜单」这些容易出错的交互逻辑全部
可测——它们是用户每天都会碰的部分，也是最不该靠手感验证的部分。

## 滚动语义

`scroll_offset` 是「从底部往上数几行」。0 表示贴在底部（跟随最新输出）。
新内容到来时保持这个偏移，因此用户在往上翻看历史时不会被新输出拽回底部
——这是长会话里最烦人的体验问题之一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .frames import FrameState
from .panels import (
    BOLD,
    BRIGHT_CYAN,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    REVERSE,
    YELLOW,
    BannerStats,
    PanelOptions,
    render_banner,
    render_footer_bar,
    render_panel,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
)
from .terminal import (
    KEY_BACKSPACE,
    KEY_CTRL_C,
    KEY_CTRL_D,
    KEY_DOWN,
    KEY_END,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_HOME,
    KEY_LEFT,
    KEY_PAGE_DOWN,
    KEY_PAGE_UP,
    KEY_RIGHT,
    KEY_TAB,
    KEY_UP,
    TerminalSize,
)
from .width import string_display_width, truncate_plain, wrap_plain

#: 命令分发的最小信息量。
_COMMANDS = (
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
    ("/clear", "清屏"),
    ("/exit", "退出"),
)

MAX_HISTORY = 200
MAX_TRANSCRIPT = 2000


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


@dataclass
class TranscriptEntry:
    kind: str  # user | assistant | progress | tool | notice | error
    text: str
    ok: bool | None = None
    collapsed: bool = False

    def render(self, width: int) -> list[str]:
        """把一个条目渲染成若干行，**保证每行不超过 `width`**。

        折行是必须的：一条长回答如果原样输出，超出终端宽度的部分会被终端
        自己折行，于是行数与我们的记账不符——差分渲染会漏画、滚动会错位。
        所有条目都必须走这一步。
        """
        limit = max(20, width - 4)
        if self.kind == "user":
            return [f"{REVERSE}› {truncate_plain(self.text, limit)}{RESET}"]
        if self.kind == "assistant":
            return [
                f"{BOLD}{line}{RESET}"
                for line in _wrap_entry_text(self.text, limit)
            ]
        if self.kind == "progress":
            return [f"{DIM}· {line}{RESET}" for line in _wrap_entry_text(self.text, limit)]
        if self.kind == "notice":
            return [f"{CYAN}{line}{RESET}" for line in _wrap_entry_text(self.text, limit)]
        if self.kind == "error":
            return [f"{RED}{line}{RESET}" for line in _wrap_entry_text(self.text, limit)]
        if self.kind == "tool":
            # `text` 是工具名；collapse 时不再重复显示它。
            detail = "" if self.collapsed else self.text
            return [render_tool_panel(self.text, detail, ok=self.ok, width=width)]
        return [self.text]


def _wrap_entry_text(text: str, limit: int) -> list[str]:
    """折行，但保留空行（段落分隔有意义）。"""
    lines: list[str] = []
    for line in text.split("\n"):
        if line == "":
            lines.append("")
            continue
        lines.extend(wrap_plain(line, limit))
    return lines or [""]


@dataclass
class AppState:
    """界面全部可变状态。纯数据，没有任何终端依赖。"""

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
    slash_menu_visible: bool = True
    footer_hint: str = "Enter 发送 · Ctrl-C 中断 · Ctrl-D 退出 · ↑↓ 历史"
    running: bool = False

    #: 转录区末尾渲染后有多少行。`handle_key` 滚动时需要它，
    #: 因此由 `compose_frame` 回填，避免调用方为了滚动而重新渲染一遍。
    last_total_lines: int = 0

    # -- 记录 ---------------------------------------------------------------

    def push(self, kind: str, text: str, *, ok: bool | None = None) -> None:
        self.transcript.append(TranscriptEntry(kind=kind, text=text, ok=ok))
        if len(self.transcript) > MAX_TRANSCRIPT:
            # 截断最旧的：界面历史不该无限增长。
            del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT]

    def update_running_tool(self, text: str, ok: bool | None) -> bool:
        """把最后一个「运行中」的工具条目收尾。返回是否有可收尾的条目。"""
        for entry in reversed(self.transcript):
            if entry.kind == "tool" and entry.ok is None:
                entry.text = text
                entry.ok = ok
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
        # 连续重复不记两次。
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        if len(self.history) > MAX_HISTORY:
            del self.history[: len(self.history) - MAX_HISTORY]
        self.history_index = -1
        self.history_draft = ""

    # -- 滚动 ---------------------------------------------------------------

    def scroll(self, delta: int, viewport_lines: int, total_lines: int) -> None:
        """`delta > 0` 向上（看更早的内容）。"""
        max_offset = max(0, total_lines - viewport_lines)
        self.scroll_offset = max(0, min(max_offset, self.scroll_offset + delta))

    def jump_to_bottom(self) -> None:
        self.scroll_offset = 0

    # -- 斜杠菜单 -----------------------------------------------------------

    def slash_matches(self) -> list[tuple[str, str]]:
        if not self.input.startswith("/") or " " in self.input:
            return []
        return [(usage, desc) for usage, desc in _COMMANDS if usage.startswith(self.input)]


# ---------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------

#: 没有变化的行数上限（避免极端终端尺寸下的负值）。
MIN_VIEWPORT_LINES = 3


@dataclass
class Layout:
    width: int
    height: int
    transcript_lines: int


def compute_layout(size: TerminalSize) -> Layout:
    """预留横幅、输入行、状态行、页脚之后，留给转录区的行数。"""
    width = max(40, size.columns)
    # 横幅 4 行 + 输入 1 行 + 状态 1 行 + 页脚 1 行 + 间隔 2 行
    reserved = 9
    return Layout(
        width=width,
        height=size.rows,
        transcript_lines=max(MIN_VIEWPORT_LINES, size.rows - reserved),
    )


def compose_transcript_lines(state: AppState, width: int) -> list[str]:
    lines: list[str] = []
    for entry in state.transcript:
        lines.extend(entry.render(width))
    return lines


def compose_frame(
    state: AppState,
    size: TerminalSize,
    *,
    mcp_waiting: bool = False,
) -> str:
    """把状态渲染成整屏文本（含 ANSI）。

    宽度做成参数而不是去读终端，因此「面板在 80 列下是否溢出」这类问题
    可以被测试直接断言。
    """
    layout = compute_layout(size)
    width = layout.width

    all_lines = compose_transcript_lines(state, width - 2)
    viewport = layout.transcript_lines
    state.last_total_lines = len(all_lines)
    max_offset = max(0, len(all_lines) - viewport)
    offset = max(0, min(max_offset, state.scroll_offset))

    if offset == 0:
        visible = all_lines[-viewport:] if all_lines else []
    else:
        end = len(all_lines) - offset
        start = max(0, end - viewport)
        visible = all_lines[start:end]

    parts: list[str] = [render_banner(state.banner, width)]

    # 转录区**固定占满视口**：内容不足时用空行补足。
    #
    # 不补的话，输入行会紧跟在内容后面浮在屏幕中间，新消息一来整块往下跳
    # ——这是最影响观感的一类问题，而且只有真的渲染出来才看得到。
    if not visible:
        parts.append(render_panel("会话", f"{DIM}还没有内容。输入一条消息开始。{RESET}", width))
        pad = max(0, viewport - 3)
    else:
        parts.append("\n".join(visible))
        pad = max(0, viewport - len(visible))
    if pad:
        parts.append("\n" * pad)

    menu = state.slash_matches() if state.slash_menu_visible else []
    if menu:
        parts.append(render_slash_menu(menu[:6], width))

    # 输入行
    prompt = "› "
    available = width - string_display_width(prompt) - 1
    shown = state.input
    if string_display_width(shown) > available:
        # 只显示尾部：光标总在末尾附近。
        shown = truncate_plain(shown, available) if available > 3 else shown[-available:]
    parts.append(f"{BRIGHT_CYAN}{prompt}{RESET}{shown}")

    status = state.status
    if mcp_waiting and not status:
        status = "MCP 连接中"
    parts.append(render_status_line(status, state.animation_frame))

    hint = state.footer_hint
    if offset > 0:
        hint = f"已上翻 {offset} 行 · End 回到底部 · {hint}"
    parts.append(render_footer_bar(hint, width))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 按键
# ---------------------------------------------------------------------------


@dataclass
class KeyOutcome:
    """处理一个键的结果。"""

    submit: str | None = None
    interrupt: bool = False
    quit: bool = False
    redraw: bool = True


def handle_key(state: AppState, key: str, size: TerminalSize) -> KeyOutcome:
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
        if not state.input:
            return KeyOutcome(quit=True)
        return KeyOutcome()

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
        # 命中了斜杠菜单时，↑ 仍然走历史（菜单是提示性的，不抢按键）。
        state.previous_history()
        return KeyOutcome()

    if key == KEY_DOWN:
        state.next_history()
        return KeyOutcome()

    if key == KEY_PAGE_UP:
        layout = compute_layout(size)
        state.scroll(layout.transcript_lines, layout.transcript_lines, state.last_total_lines)
        return KeyOutcome()

    if key == KEY_PAGE_DOWN:
        layout = compute_layout(size)
        state.scroll(-layout.transcript_lines, layout.transcript_lines, state.last_total_lines)
        return KeyOutcome()

    if key == KEY_ESCAPE:
        if state.input:
            state.set_input("")
            return KeyOutcome()
        return KeyOutcome()

    if key == KEY_TAB:
        matches = state.slash_matches()
        if matches:
            # 补全到唯一候选；多个候选时补到最长公共前缀。
            usages = [usage for usage, _desc in matches]
            state.set_input(_common_prefix(usages))
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
