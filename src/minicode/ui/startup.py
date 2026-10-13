"""启动动画 —— 终端里的像素画。

## 设计取向

Claude Code 启动时有一段简短的像素动画。本实现的做法是**用方块字符拼出
字形，再逐帧「点亮」**——不依赖图片、不依赖第三方库，纯字符串。

    帧 1      帧 2      帧 3      帧 4
    ····      █···      ██··      ████
    ····      ····      █···      ████
    ····      ····      ····      ████

## 三条硬约束

**① 帧高固定。** 多行动画靠「上移 N 行」重绘，高度一变就会漂移。
所有帧必须行数相同——`_pad` 负责补齐。

**② 行宽 ≤ 终端宽度。** 超宽会触发终端自动折行，同样破坏行数记账。

**③ 总时长有上限。** 启动动画绝不能拖慢程序启动。默认 0.6 秒，
且用户一开始输入就立刻中断（`should_abort`）。

## 降级

非终端环境（管道/重定向/CI）下 `enabled=False`，`play` 直接返回，
**不输出任何东西**——连空行都不留，避免污染重定向的文件。
"""

from __future__ import annotations

from dataclasses import dataclass

from .animate import BlockAnimator, hint_line
from .theme import BOLD, BRIGHT_CYAN, COLOR_HINT, MUTED, ORANGE, paint

#: 像素画的「点亮」字符与「未点亮」字符。
#: 用方块而不是点阵，在等宽字体里更整齐。
PIXEL_ON = "█"
PIXEL_OFF = "·"

#: 字形定义：用 1/0 表示哪些位置最终会被点亮。
#:
#: 拼的是 "MINI" —— 用 5×5 点阵逐字展开，横排显示。
#: 选择字母而不是图形的原因：字母在等宽字体里天然对齐，而且一眼能认出来。
#: 5×5 是点阵字体的最小可用尺寸，再小就认不出字形了。
_FONT: dict[str, tuple[str, ...]] = {
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001"),
    "C": ("01110", "10001", "10000", "10001", "01110"),
    "O": ("01110", "10001", "10001", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "11110", "10000", "11111"),
}

#: 启动动画显示的词。太长会让动画显得拖沓。
_WORD = "MINI"


def _compose_word(word: str) -> tuple[str, ...]:
    """把单词按点阵横向拼起来（字母间空一列）。"""
    glyphs = [_FONT[letter] for letter in word if letter in _FONT]
    if not glyphs:
        return ()
    rows: list[str] = []
    for row_index in range(len(glyphs[0])):
        rows.append("0".join(glyph[row_index] for glyph in glyphs))
    return tuple(rows)


_GLYPH = _compose_word(_WORD)


def _glyph_frames(
    glyph: tuple[str, ...], *, steps: int = 5, hold: int = 0
) -> list[list[str]]:
    """把字形展开成逐帧「点亮」动画。

    `steps` 是点亮的批次数量。批次越多动画越细腻，但帧数也越多。
    点亮顺序按行优先——看起来像「从上往下刷」。

    `hold` 是在最后一帧之后**额外重复**的次数（用于收尾停顿）。
    刻意用重复最后一帧实现，而不是加一个内容相同的新帧——前者语义清晰
    （「停一拍」），后者会让人以为漏了什么变化。
    """
    # 收集所有需要点亮的位置，按行优先排序
    targets: list[tuple[int, int]] = [
        (row, column)
        for row, line in enumerate(glyph)
        for column, char in enumerate(line)
        if char == "1"
    ]
    if not targets:
        return []

    frames: list[list[str]] = []
    for step in range(1, steps + 1):
        lit = set(targets[: max(1, len(targets) * step // steps)])
        rows: list[str] = []
        for row, line in enumerate(glyph):
            rows.append(
                "".join(
                    PIXEL_ON if (row, column) in lit else PIXEL_OFF
                    for column in range(len(line))
                )
            )
        frames.append(rows)

    for _ in range(max(0, hold)):
        frames.append(list(frames[-1]))
    return frames


@dataclass
class StartupAnimation:
    """启动动画。播放失败或环境不支持时静默跳过。"""

    width: int = 80
    height: int = 5
    enabled: bool = True
    stream: object | None = None
    #: 总时长上限（秒）。启动动画不该拖慢启动。
    max_seconds: float = 0.6
    #: 每帧间隔。
    interval: float = 0.07

    def frames(self) -> list[list[str]]:
        """动画帧序列（每帧固定 `height` 行）。

        终端太窄时返回**空列表**——像素画被截断会变成一堆无意义的方块，
        不如直接不播。这是「宁可没有动画」原则的又一体现。
        """
        natural = max((len(row) for row in _GLYPH), default=0)
        if natural == 0 or natural > self.width:
            return []
        raw = _glyph_frames(_GLYPH, steps=4, hold=1)
        return [self._pad(frame) for frame in raw]

    def _pad(self, frame: list[str]) -> list[str]:
        rows = list(frame[: self.height])
        while len(rows) < self.height:
            rows.append("")
        return rows

    def play(self, *, should_abort: object = None) -> None:
        """播放。非终端环境下什么都不做。"""
        if not self.enabled:
            return
        animator = BlockAnimator(
            height=self.height, width=self.width, stream=self.stream, enabled=True
        )
        animator.play(
            self.frames(),
            interval=self.interval,
            color=BRIGHT_CYAN,
            max_seconds=self.max_seconds,
            should_abort=should_abort,  # type: ignore[arg-type]
        )


def banner_title_lines(width: int = 80) -> list[str]:
    """静态标题行（动画结束后的定格内容）。

    动画只是开场；最终留在屏幕上的是这个——一行标题加一行提示。
    这样即便动画被跳过，用户仍能看到完整信息。
    """
    title = paint("MiniCode", BOLD + BRIGHT_CYAN)
    tagline = paint("终端编码助手", COLOR_HINT)
    return [f"{title}  {tagline}"]


def startup_hint() -> str:
    return hint_line("输入 /help 查看命令 · /exit 退出")
