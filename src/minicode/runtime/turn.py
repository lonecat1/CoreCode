"""TurnRunner —— 单飞闸门。

整个运行时里**只有 35 行**，但它是「多个状态机不会互相踩踏」的全部原因。

它保证两件事：

  1. **同一时刻只有一个根回合。** 已在运行时的第二次调用直接被拒绝，
     而不是排队（排队会导致用户连按回车就跑出两个并发回合）。
  2. **取消信号可合并。** 自身控制器与调用方传入的信号任一触发即取消。
     这样 `/exit` 或 Ctrl-C 能打断正在进行的一切。

Goal 与 Loop 都复用同一个 TurnRunner 实例，因此它们的回合天然互斥——
不需要在 Goal 里判断「Loop 是不是在跑」，也不需要反过来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..abort import AbortSignal
from ..types import AgentTurnResult


@dataclass(slots=True)
class TurnRequest:
    """一次根回合请求。"""

    input: dict[str, Any]
    #: `goal` / `loop` 用于决定注入哪套运行时上下文与是否叠加 Goal 工具。
    mode: str | None = None
    signal: AbortSignal | None = None


class TurnBusyError(RuntimeError):
    """已有根回合在运行。"""


class TurnRunner:
    """同一时刻一个根回合，含取消传播。"""

    def __init__(self) -> None:
        self._controller: AbortSignal | None = None
        self._done: Any = None
        self.awaiting_user = False

    @property
    def busy(self) -> bool:
        return self._controller is not None

    def run(
        self,
        work: Callable[[AbortSignal], AgentTurnResult],
        parent_signal: AbortSignal | None = None,
    ) -> AgentTurnResult:
        """执行一个根回合。已有在跑时抛 `TurnBusyError`。"""
        if self._controller is not None:
            raise TurnBusyError("A root turn is still running.")

        controller = AbortSignal()
        # 合并自身与父信号：任一触发都能取消这个回合。
        signal = controller.linked(parent_signal) if parent_signal is not None else controller

        self.awaiting_user = False
        self._controller = controller
        try:
            result = work(signal)
            self.awaiting_user = result.outcome == "awaiting_user"
            return result
        finally:
            self._controller = None

    def stop(self, reason: str = "Execution stopped by user") -> None:
        """取消正在进行的回合。无回合时是空操作。"""
        controller = self._controller
        if controller is not None:
            controller.abort(reason)
