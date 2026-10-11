"""面板与徽章渲染 —— TUI 的「外观契约」。

这些函数是**纯字符串变换**：输入数据，输出可直接写进终端的文本（含 ANSI）。
不碰 stdout、不读终端尺寸，因此可以完整测试。终端尺寸由调用方传入。

## 为什么坚持纯函数

原 TS 实现里这些函数直接读 `process.stdout.columns`，于是「渲染成什么样」
取决于运行环境，无法断言。把尺寸变成参数后，同一份输入永远给出同一份输出，
面板对齐问题就能被测试抓住——而不是等用户看到错位的边框。

## 颜色是契约

`BORDER` 用 256 色的 `38;5;31`，其余用标准 SGR。逐一照搬，不「顺手优化」：
换一个色号在不同终端上的观感会变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..context.tokens import js_round
from .width import (
    pad_plain,
    string_display_width,
    strip_ansi,
    truncate_plain,
    wrap_plain,
)

# ---------------------------------------------------------------------------
# 颜色（与 TS 逐字节一致）
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
BOLD = "\x1b[1m"
REVERSE = "\x1b[7m"
BRIGHT_GREEN = "\x1b[92m"
BRIGHT_RED = "\x1b[91m"
BRIGHT_CYAN = "\x1b[96m"
BRIGHT_YELLOW = "\x1b[93m"
BORDER = "\x1b[38;5;31m"

#: 上下文占用等级 → 颜色。
WARNING_COLORS: dict[str, str] = {
    "normal": GREEN,
    "warning": YELLOW,
    "critical": RED,
    "blocked": BRIGHT_RED,
}

MIN_PANEL_WIDTH = 60
DEFAULT_PANEL_WIDTH = 100


# ---------------------------------------------------------------------------
# 基础零件
# ---------------------------------------------------------------------------


def color_badge(label: str, value: str, color: str) -> str:
    return f"{color}[{label}]{RESET} {BOLD}{value}{RESET}"


def border_line(kind: str, width: int) -> str:
    inner = max(0, width - 2)
    if kind == "top":
        return f"{BORDER}╭{'─' * inner}╮{RESET}"
    return f"{BORDER}╰{'─' * inner}╯{RESET}"


def panel_row(left: str, width: int, right: str | None = None) -> str:
    inner = max(0, width - 4)
    right_text = right or ""
    left_width = string_display_width(left)
    right_width = string_display_width(right_text)
    gap = max(1, inner - left_width - right_width) if right_text else 0

    left_text = left
    if left_width + right_width + gap > inner:
        left_text = truncate_plain(left, max(0, inner - right_width - 1))

    padding = " " * max(0, inner - string_display_width(left_text) - right_width)
    return f"{BORDER}│{RESET} {left_text}{padding}{right_text} {BORDER}│{RESET}"


def empty_panel_row(width: int) -> str:
    return f"{BORDER}│{RESET}{' ' * max(0, width - 2)}{BORDER}│{RESET}"


def wrap_panel_body_line(line: str, width: int) -> list[str]:
    """按面板内宽折行。

    注意：折行基于**去掉 ANSI 后的纯文本**，因此颜色会在折行处丢失——
    这与原实现一致。折行时保住颜色需要跟踪 SGR 状态，那是另一个复杂度层级，
    而这里的内容通常是纯文本。
    """
    inner = max(0, width - 4)
    if inner <= 0:
        return [""]
    plain = strip_ansi(line)
    if string_display_width(plain) <= inner:
        return [line]
    return wrap_plain(plain, inner)


def join_segments_within_width(
    segments: list[str], separator: str, max_width: int
) -> str:
    """把片段用分隔符拼起来，直到超出宽度为止。"""
    if max_width <= 0 or not segments:
        return ""
    output = ""
    for segment in segments:
        candidate = f"{output}{separator}{segment}" if output else segment
        if string_display_width(candidate) <= max_width:
            output = candidate
            continue
        break
    return output


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------


@dataclass
class PanelOptions:
    right_title: str | None = None
    min_body_lines: int = 0
    show_title: bool = True
    frame: bool = True


def render_panel(
    title: str,
    body: str,
    width: int | None = None,
    options: PanelOptions | None = None,
) -> str:
    opts = options or PanelOptions()
    panel_width = max(MIN_PANEL_WIDTH, width or DEFAULT_PANEL_WIDTH)

    body_lines = body.split("\n") if body else []
    rendered: list[str] = []
    for line in body_lines:
        rendered.extend(wrap_panel_body_line(line, panel_width))

    while len(rendered) < opts.min_body_lines:
        rendered.append("")

    if not opts.frame:
        return "\n".join(rendered)

    rows = [border_line("top", panel_width)]
    if opts.show_title:
        right = None
        if opts.right_title:
            limit = max(10, int(panel_width * 0.3))
            right = f"{DIM}{truncate_plain(opts.right_title, limit)}{RESET}"
        rows.append(panel_row(f"{BRIGHT_CYAN}{BOLD}{title}{RESET}", panel_width, right))
        # 标题与正文之间的空行只有标题存在时才加。
        rows.append(empty_panel_row(panel_width))
    rows.extend(panel_row(line, panel_width) for line in rendered)
    rows.append(border_line("bottom", panel_width))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 徽章与状态
# ---------------------------------------------------------------------------


def render_context_badge(stats: Any) -> str:
    """上下文占用徽章：`[ctx] 62% ▓▓▓▓▓▓░░░░ usage`。"""
    utilization = getattr(stats, "utilization", 0.0)
    warning_level = getattr(stats, "warning_level", "normal")
    accounting = getattr(stats, "accounting", None)

    percent = js_round(utilization * 100)
    color = WARNING_COLORS.get(warning_level, GREEN)

    filled = js_round(utilization * 10)
    # 夹在 0..10：利用率可能因计量误差略微超过 1。
    filled = max(0, min(10, filled))
    bar = "▓" * filled + "░" * (10 - filled)

    source = getattr(accounting, "source", "") if accounting else ""
    source_label = {
        "provider_usage": "usage",
        "provider_usage_plus_estimate": "usage+est",
        "estimate_only": "est",
    }.get(source, "")
    suffix = f" {source_label}" if source_label else ""

    return color_badge("ctx", f"{percent}% {bar}{suffix}", color)


def render_status_dots(animation_frame: int = 0) -> str:
    """转圈动画：四帧循环。"""
    frames = ("⠋", "⠙", "⠹", "⠸")
    return frames[animation_frame % len(frames)]


def render_status_line(status: str | None, animation_frame: int = 0) -> str:
    if not status:
        return DIM + "ready" + RESET
    return f"{CYAN}{render_status_dots(animation_frame)}{RESET} {status}"


def render_permission_summary_line(summary: list[str]) -> str:
    """把权限摘要折成一行徽章，放不下就省略。"""
    if not summary:
        return f"{DIM}[permissions] none{RESET}"
    badges = [f"{CYAN}[{item}]{RESET}" for item in summary]
    joined = join_segments_within_width(badges, " ", 60)
    return joined or f"{DIM}[permissions] {len(summary)} rules{RESET}"


@dataclass
class BannerStats:
    """横幅需要的数据。刻意用普通数据而非宿主对象，方便测试。"""

    model: str = "not-configured"
    session_id: str = ""
    cwd: str = ""
    transcript_count: int = 0
    message_count: int = 0
    skill_count: int = 0
    mcp_total: int = 0
    mcp_connected: int = 0
    mcp_connecting: int = 0
    mcp_error: int = 0
    context_stats: Any = None


def render_banner(stats: BannerStats, width: int | None = None) -> str:
    """顶部横幅：模型 / 会话 / 各类计数 / 上下文徽章。"""
    panel_width = max(MIN_PANEL_WIDTH, width or DEFAULT_PANEL_WIDTH)

    segments = [
        f"{BOLD}{stats.model}{RESET}",
        f"{DIM}session {stats.session_id or '-'}{RESET}",
        f"{DIM}msgs {stats.message_count}{RESET}",
    ]
    if stats.transcript_count:
        segments.append(f"{DIM}turns {stats.transcript_count}{RESET}")
    if stats.skill_count:
        segments.append(f"{DIM}skills {stats.skill_count}{RESET}")
    if stats.mcp_total:
        mcp_text = f"{stats.mcp_connected}/{stats.mcp_total} mcp"
        if stats.mcp_error:
            segments.append(f"{RED}{mcp_text} ({stats.mcp_error} err){RESET}")
        elif stats.mcp_connecting:
            segments.append(f"{YELLOW}{mcp_text} connecting{RESET}")
        else:
            segments.append(f"{GREEN}{mcp_text}{RESET}")

    left = join_segments_within_width(segments, f"{DIM} · {RESET}", panel_width - 20)
    right = render_context_badge(stats.context_stats) if stats.context_stats else ""

    return "\n".join(
        [
            border_line("top", panel_width),
            panel_row(left, panel_width, right),
            panel_row(f"{DIM}{truncate_plain(stats.cwd, panel_width - 8)}{RESET}", panel_width),
            border_line("bottom", panel_width),
        ]
    )


def render_tool_panel(
    tool_name: str,
    detail: str,
    *,
    ok: bool | None = None,
    width: int | None = None,
) -> str:
    """一次工具调用的单行展示。`ok=None` 表示仍在运行。"""
    panel_width = max(MIN_PANEL_WIDTH, width or DEFAULT_PANEL_WIDTH)
    if ok is None:
        icon = f"{CYAN}{render_status_dots()}{RESET}"
    elif ok:
        icon = f"{BRIGHT_GREEN}✓{RESET}"
    else:
        icon = f"{BRIGHT_RED}✗{RESET}"

    body = f"{icon} {BOLD}{tool_name}{RESET}"
    if detail:
        body += f" {DIM}{detail}{RESET}"
    return truncate_plain(body, panel_width)


def render_footer_bar(
    hint: str, width: int | None = None, *, scrolling: bool = False
) -> str:
    panel_width = max(MIN_PANEL_WIDTH, width or DEFAULT_PANEL_WIDTH)
    if scrolling:
        hint = f"滚动中 · {hint}"
    return f"{REVERSE}{pad_plain(truncate_plain(hint, panel_width), panel_width)}{RESET}"


def render_slash_menu(matches: list[tuple[str, str]], width: int | None = None) -> str:
    """斜杠命令候选菜单。传入 `(usage, description)` 列表。"""
    if not matches:
        return ""
    panel_width = max(MIN_PANEL_WIDTH, width or DEFAULT_PANEL_WIDTH)
    rows = []
    for usage, description in matches:
        line = f"  {BRIGHT_CYAN}{usage}{RESET}  {DIM}{description}{RESET}"
        rows.append(truncate_plain(line, panel_width))
    return "\n".join(rows)


def format_session_stats(stats: BannerStats) -> str:
    """一行统计摘要，给 `/status` 之类的场景复用。"""
    parts = [
        f"消息 {stats.message_count}",
        f"工具调用 {stats.transcript_count}",
        f"技能 {stats.skill_count}",
        f"MCP {stats.mcp_connected}/{stats.mcp_total}",
    ]
    return " · ".join(parts)
