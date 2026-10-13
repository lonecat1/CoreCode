"""内联实时区域渲染 —— 仿 Claude Code 的终端策略。

## 与「全屏备用屏幕」的根本差别

原实现（以及我最初照搬的做法）用**备用屏幕 + 绝对行定位**：

    ESC[?1049h         进入备用屏幕
    ESC[H              光标回左上
    ESC[<行>;1H        绝对定位到第 N 行
    退出时 ESC[?1049l  回到原来的屏幕

这套方案有一个致命脆弱点：**它要求「渲染出的行数」恰好等于「终端行数」**。
一旦不等（哪怕只多一行），写入就会触发终端滚动，屏幕整体上移，之后所有
绝对行号全部错位，画面必然错乱。而「恰好相等」需要精确预测换行，任何
宽度估算偏差、CJK 宽度差异、终端边距都会破坏它。

Claude Code 用的是另一条路：**内联模式**——不占用备用屏幕，只在自己占据的
那几行里原地重绘：

    光标上移 N 行  →  逐行擦除并重写  →  光标回到输入处

它**不需要知道终端总行数**，因此不存在「多一行就崩」的问题。历史内容留在
正常滚动缓冲里（用户仍可向上滚动查看），退出后终端状态天然干净。

## 只重绘变化的行

逐行比对上一帧：没变的行只发「下移一行」（一个字节的 `\\n`），变了的行才发
擦除 + 内容。长对话滚动时写入量与变化量成正比，不闪。

## 为什么不用绝对定位

`ESC[<行>;1H` 只在「帧高已知且稳定」时正确。内联模式里实时区域会随内容
增高而向上推，绝对行号没有意义。相对移动（上移/下移）才是稳的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ANSI 序列
# ---------------------------------------------------------------------------

ERASE_LINE = "\x1b[2K"
ERASE_TO_END = "\x1b[J"
CARRIAGE_RETURN = "\r"
CURSOR_UP = "\x1b[{n}A"
CURSOR_DOWN = "\x1b[{n}B"
CURSOR_COLUMN = "\x1b[{n}G"

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

#: 备用屏幕（**默认不使用**，保留给将来可能的全屏模式）。
ENTER_ALT_SCREEN = "\x1b[?1049h"
EXIT_ALT_SCREEN = "\x1b[?1049l"
DISABLE_MOUSE_TRACKING = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"


def cursor_up(count: int) -> str:
    return CURSOR_UP.format(n=count) if count > 0 else ""


def cursor_down(count: int) -> str:
    return CURSOR_DOWN.format(n=count) if count > 0 else ""


def cursor_column(column: int) -> str:
    """把光标移到本行第 `column` 列（1 基）。"""
    return CURSOR_COLUMN.format(n=max(1, column))


# ---------------------------------------------------------------------------
# 渲染状态
# ---------------------------------------------------------------------------


@dataclass
class LiveRegion:
    """一个「原地重绘」的实时区域。

    它记住上一帧画了什么、占了几行，从而能精确地把光标移回去重画。
    所有定位都是**相对于区域顶部**的，因此不依赖终端总行数。
    """

    lines: list[str] = field(default_factory=list)
    #: 上一帧把光标停在了区域内第几行。用来判断「只改光标位置」这种情况。
    cursor_row: int = 0

    def reset(self) -> None:
        self.lines = []
        self.cursor_row = 0

    @property
    def height(self) -> int:
        return len(self.lines)

    def clear_sequence(self) -> str:
        """清掉当前区域并让光标停在区域起始行。

        调用方必须在**区域最后一行**上（即刚刚画完的状态）。
        """
        if not self.lines:
            return ""
        # 上移到区域首行，然后擦到屏幕末尾。
        return cursor_up(len(self.lines) - 1) + CARRIAGE_RETURN + ERASE_TO_END

    def render(self, new_lines: list[str], *, cursor_row: int = 0) -> str:
        """把区域更新成 `new_lines`，返回要写出的序列。

        `cursor_row` 是希望光标停留的**区域内行号**（0 基）。区域画完后光标
        在最后一行的末尾，需要按需上移。

        **内容与光标位置都没变时返回空串。** 这一点很重要：内联模式每 30ms
        重绘一次，若「什么都没变」也照发一串光标移动序列，那既是无谓的写入，
        也破坏了「无变化则零输出」这条可测试的性质。
        """
        if not new_lines:
            new_lines = [""]

        last_row = len(new_lines) - 1
        target = max(0, min(cursor_row, last_row))

        if self.lines == new_lines and self.cursor_row == target:
            return ""

        output: list[str] = []
        first_render = not self.lines

        if first_render:
            # 首次渲染：直接写，不需要上移。
            output.append("\n".join(new_lines))
        else:
            # 回到区域首行，准备逐行比对。
            output.append(cursor_up(len(self.lines) - 1))
            output.append(CARRIAGE_RETURN)

            previous = self.lines
            for index, line in enumerate(new_lines):
                if index > 0:
                    output.append("\n")
                old = previous[index] if index < len(previous) else None
                if old == line:
                    # 没变：只移动，不重写。这是「不闪」的关键。
                    continue
                # 变了（或新增）：擦掉整行再写。
                output.append(CARRIAGE_RETURN + ERASE_LINE + line)

            # 新帧比旧帧短：把多出来的旧行擦掉。
            if len(new_lines) < len(previous):
                output.append(CARRIAGE_RETURN + ERASE_TO_END)
                # 擦除会让光标停在新帧末尾，而旧行还在下方——需要补换行
                # 把它们推出可视范围之外（内联模式无法直接删除行）。
                for _ in range(len(previous) - len(new_lines)):
                    output.append("\n")
                    output.append(CARRIAGE_RETURN + ERASE_LINE)
                output.append(cursor_up(len(previous) - len(new_lines)))

        # 定位光标到目标行。
        #
        # 首帧也要做：写完 `"\n".join(lines)` 之后光标停在**最后一行**的末尾，
        # 而目标行通常在中间（输入框那一行），因此仍需上移。
        if target < last_row:
            output.append(cursor_up(last_row - target))

        self.lines = list(new_lines)
        self.cursor_row = target
        return "".join(output)


def split_for_display(text: str) -> list[str]:
    """把整屏文本切成行。

    末尾的换行不产生额外空行——否则每帧都会多一行，把布局越推越高。
    """
    if not text:
        return [""]
    lines = text.split("\n")
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines
