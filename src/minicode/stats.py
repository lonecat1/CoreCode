"""会话统计 —— 回合耗时与工具调用次数。

## 为什么单独成模块

统计逻辑有两个容易出错的地方，都不该散在界面代码里：

  1. **计时的起止点**。一次回合的耗时应当从「用户提交」算到「模型交回控制权」，
     包含工具执行与压缩，但**不包含**用户自己思考的时间。把两者混在一起会
     得出毫无意义的数字。
  2. **工具调用的归属**。子 Agent 内部也会调工具，但那是它自己的回合。
     本模块只统计**根回合**的调用，否则数字会随委派行为大幅跳动。

统计本身是纯数据累加，因此可以脱离界面与模型单独测试。

## 展示口径

  - 单次回合：`⏱ 3.2s · 4 次工具调用`
  - 会话累计：`5 回合 · 18.4s · 23 次工具调用`
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def format_duration(seconds: float) -> str:
    """把秒数格式化成人能读的形式。

    规则刻意简单：小于 1 秒用毫秒，小于 60 秒用一位小数的秒，
    更长则用「分:秒」——比「123.4s」好读得多。
    """
    if seconds < 0:
        seconds = 0.0
    milliseconds = seconds * 1000
    # 显示 `0ms` 会让人以为计时坏了；不足 1 毫秒时明确说「不到 1 毫秒」。
    if milliseconds < 1:
        return "<1ms"
    if seconds < 1.0:
        return f"{int(milliseconds)}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class TurnStats:
    """单次回合的统计。"""

    duration_seconds: float = 0.0
    tool_calls: int = 0
    steps: int = 0
    outcome: str = ""

    def line(self) -> str:
        """一行摘要，附在回合输出之后。"""
        parts = [format_duration(self.duration_seconds)]
        if self.tool_calls:
            parts.append(f"{self.tool_calls} 次工具调用")
        if self.steps > 1:
            parts.append(f"{self.steps} 步")
        return " · ".join(parts)


@dataclass
class SessionStats:
    """会话累计统计。

    只累加，不做聚合计算——聚合在展示时做，这样原始数据不会丢，
    将来想换一种展示口径也不用改数据层。
    """

    turns: int = 0
    total_seconds: float = 0.0
    tool_calls: int = 0
    steps: int = 0
    #: 最近一次回合的统计，用于在回合结束后立即展示。
    last: TurnStats = field(default_factory=TurnStats)

    # -- 记录 ---------------------------------------------------------------

    def record(self, stats: TurnStats) -> None:
        self.turns += 1
        self.total_seconds += max(0.0, stats.duration_seconds)
        self.tool_calls += max(0, stats.tool_calls)
        self.steps += max(0, stats.steps)
        self.last = stats

    def reset(self) -> None:
        self.turns = 0
        self.total_seconds = 0.0
        self.tool_calls = 0
        self.steps = 0
        self.last = TurnStats()

    # -- 展示 ---------------------------------------------------------------

    @property
    def average_seconds(self) -> float:
        return self.total_seconds / self.turns if self.turns else 0.0

    def summary_line(self) -> str:
        """会话累计的一行摘要。"""
        if not self.turns:
            return "还没有回合"
        parts = [
            f"{self.turns} 回合",
            format_duration(self.total_seconds),
        ]
        if self.tool_calls:
            parts.append(f"{self.tool_calls} 次工具调用")
        if self.turns > 1:
            parts.append(f"平均 {format_duration(self.average_seconds)}")
        return " · ".join(parts)


class TurnTimer:
    """回合计时器。

    用上下文管理器保证「无论怎么退出都记时」——包括异常与取消。
    手动 `start()`/`stop()` 容易在异常路径上漏掉 stop，得到荒谬的耗时。
    """

    def __init__(self) -> None:
        self._started_at: float | None = None

    def start(self) -> "TurnTimer":
        self._started_at = time.monotonic()
        return self

    def stop(self) -> float:
        if self._started_at is None:
            return 0.0
        elapsed = time.monotonic() - self._started_at
        self._started_at = None
        return elapsed

    @property
    def running(self) -> bool:
        return self._started_at is not None

    def __enter__(self) -> "TurnTimer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
