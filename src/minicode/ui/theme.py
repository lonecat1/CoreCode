"""配色与字形 —— 仿 Claude Code 的视觉语言。

## 为什么单独一层

颜色是**外观契约**：换一个色号在不同终端上的观感就变了，而这类改动很难被
测试发现。集中在一处之后，「哪些颜色被用到」是可以被检查的，而不是散落在
各个渲染函数里的字面量。

## Claude Code 的视觉特征

  - 主色是**暗灰**（次要信息）与**亮白**（正文），不滥用颜色。
  - 工具调用用 `⏺` 起头，结果用 `⎿` 缩进承接——这是它最有辨识度的记号。
  - 提示符是 `>`，配合**圆角输入框**。
  - 强调色偏暖（橙/红），用于错误与需要注意的状态。
  - 不用背景色块（很多终端对背景色支持不一致，容易糊成一片）。

## 降级

`NO_COLOR` 环境变量被设置时（这是一个被广泛遵守的约定），全部颜色退化为
空串。Windows 老控制台对 ANSI 支持不佳时也能靠它自救。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 基础 SGR
#
# **只保留真正被用到的。** 这里此前还定义着 `ITALIC` / `UNDERLINE`，
# 以及一批来自已移除的全屏 TUI 的颜色（`BLACK` / `BRIGHT_GREEN` /
# `SOFT_ORANGE` / `BORDER_FOCUS` …）。它们零引用。
#
# 本模块开头写着「哪些颜色被用到是可以被检查的」——那就得真的可检查。
# 留着一堆没人用的色号会让这句话失效：改动者看到 `BORDER_FOCUS` 会以为
# 聚焦态有专门的颜色，而实际上没有任何代码读它。
#
# `tools/check_constants.py` 现在会报出这类零引用常量，本文件保持 0 个。
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"

#: 标准前景色
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
WHITE = "\x1b[37m"

#: 亮色前景
BRIGHT_RED = "\x1b[91m"
BRIGHT_GREEN = "\x1b[92m"
BRIGHT_YELLOW = "\x1b[93m"
BRIGHT_BLUE = "\x1b[94m"
BRIGHT_CYAN = "\x1b[96m"
BRIGHT_WHITE = "\x1b[97m"

#: 256 色：Claude Code 的暖橙强调色与冷灰边框。
ORANGE = "\x1b[38;5;209m"
BORDER = "\x1b[38;5;240m"      # 中灰边框
MUTED = "\x1b[38;5;245m"       # 次要文字
SUBTLE = "\x1b[38;5;238m"      # 更淡的提示

# ---------------------------------------------------------------------------
# 语义色（渲染层只用这些，不直接用上面的字面量）
# ---------------------------------------------------------------------------

COLOR_USER = BRIGHT_WHITE
COLOR_ASSISTANT = ""          # 正文用终端默认色，最耐看
COLOR_PROGRESS = MUTED
COLOR_TOOL = CYAN
COLOR_TOOL_OK = GREEN
COLOR_TOOL_FAIL = RED
COLOR_NOTICE = BRIGHT_BLUE
COLOR_ERROR = BRIGHT_RED
COLOR_STATUS = MUTED
COLOR_BANNER_TITLE = BOLD + BRIGHT_WHITE
COLOR_HINT = SUBTLE

# ---------------------------------------------------------------------------
# 字形（Claude Code 的记号）
# ---------------------------------------------------------------------------

GLYPH_PROMPT = ">"
GLYPH_USER = ">"
GLYPH_TOOL = "⏺"
GLYPH_RESULT = "⎿"
GLYPH_OK = "✓"
GLYPH_FAIL = "✗"
GLYPH_BULLET = "·"

#: 输入框与面板的圆角边框。
BOX_TOP_LEFT = "╭"
BOX_TOP_RIGHT = "╮"
BOX_BOTTOM_LEFT = "╰"
BOX_BOTTOM_RIGHT = "╯"
BOX_HORIZONTAL = "─"
BOX_VERTICAL = "│"

#: 转圈动画帧。
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧")


def color_enabled() -> bool:
    """是否启用颜色。

    三个条件**同时**成立才上色：

      1. 没有被 `_color_override` 显式关掉（CLI 发现 stdout 不是终端时会关）；
      2. 环境变量 `NO_COLOR` 未设置（https://no-color.org/，被广泛遵守，
         也是用户在色彩显示异常时唯一的自救手段）；
      3. 环境变量 `TERM` 不是 `dumb`。

    **只读，不写。** 早先的实现在探测到终端不支持 ANSI 时用
    `os.environ.setdefault("NO_COLOR", "1")` 把它设进去，结果污染了整个进程
    ——连边框字符都被当成「不该上色」而丢失，而且这个副作用会泄漏到子进程。
    运行时状态应当用参数表达，不要借环境变量传递。
    """
    if _color_override is False:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if (os.environ.get("TERM") or "").strip().lower() == "dumb":
        return False
    return True


#: 显式覆盖。`None` = 按环境判断；`False` = 强制关闭。
#: 由 CLI 在启动时设置（stdout 不是终端就关掉，避免污染重定向的文件）。
_color_override: bool | None = None


def set_color_enabled(enabled: bool | None) -> None:
    """显式开关颜色。传 `None` 恢复为按环境判断。"""
    global _color_override
    _color_override = enabled


def paint(text: str, *codes: str) -> str:
    """给文本上色。`NO_COLOR` 生效时原样返回。

    参数顺序是 `(文本, 颜色…)`。**必须只接受关键字之外的这一种顺序**——
    早先我在多处写成了 `paint(COLOR_STATUS, BOX_HORIZONTAL * n)`（把颜色码
    当成了正文），于是边框字符变成了转义序列字面量，被截断函数剥掉后
    什么都不剩，输入框看起来「消失了」。这类错误不会报错，只会静默画错。

    因此这里加一道防线：如果第一个参数看起来是转义序列而后续参数里有
    明显的文本，就断言失败——宁可炸掉也不要静默画错。
    """
    if codes and text.startswith("\x1b[") and not any(c.startswith("\x1b[") for c in codes):
        raise AssertionError(
            f"paint() 的参数顺序疑似写反：第一个参数是转义序列 {text!r}，"
            f"而后面的参数 {codes!r} 里没有转义序列。"
            "正确顺序是 paint(文本, 颜色…)。"
        )
    active = [code for code in codes if code]
    if not active or not color_enabled():
        return text
    return "".join(active) + text + RESET
