"""取消信号。

回合的取消必须是**贯穿式**的：一个信号的触发要能同时打断
正在进行的模型请求、正在执行的工具，以及正在等待用户批准的提示。
因此这里不用 `asyncio`，而用一个显式的、可被任意层轮询的信号对象——
工具执行大多是同步阻塞调用（文件、子进程），把它们全部 async 化得不偿失。
"""

from __future__ import annotations

import threading
from typing import Any


class AbortError(Exception):
    """携带取消原因的中断信号。"""

    def __init__(self, reason: Any = "Execution stopped") -> None:
        super().__init__(str(reason))
        self.reason = reason


class AbortSignal:
    """可共享的取消信号。

    与 `threading.Event` 的区别：它记住取消原因，并且提供 `raise_if_aborted()`。
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: Any = None

    # -- 状态 ---------------------------------------------------------------

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Any:
        return self._reason

    # -- 动作 ---------------------------------------------------------------

    def abort(self, reason: Any = "Execution stopped") -> None:
        if not self._event.is_set():
            self._reason = reason
        self._event.set()

    def raise_if_aborted(self) -> None:
        if self._event.is_set():
            raise AbortError(self._reason)

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    # -- 组合 ---------------------------------------------------------------

    def linked(self, child: AbortSignal) -> AbortSignal:
        """返回一个「父或子任一触发即触发」的新信号。

        对应 TypeScript 版的 `AbortSignal.any([self, parent])`：用于让
        TurnRunner 自身的取消与调用方传入的取消合并。
        """
        merged = AbortSignal()
        if self.is_set():
            merged.abort(self.reason)
            return merged
        if child.is_set():
            merged.abort(child.reason)
            return merged

        def watch(source: AbortSignal) -> None:
            if source.wait():
                merged.abort(source.reason)

        for source in (self, child):
            threading.Thread(target=watch, args=(source,), daemon=True).start()
        return merged


def throw_if_aborted(signal: AbortSignal | None) -> None:
    if signal is not None:
        signal.raise_if_aborted()
