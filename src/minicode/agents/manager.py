"""SubAgentManager —— 只读子 Agent 的生命周期管理。

## 四个教学要点

这个 MVP 刻意很小，但它包含了多 Agent 系统的四个本质概念：

  1. **隔离的上下文**：每个 worker 有自己的 `messages[]`，不共享历史。
     它只看到自己的系统提示与任务，因此不会被根 Agent 的对话污染。
  2. **受限的工具**：worker 拿到的是 `ToolRegistry.subset(SUBAGENT_TOOL_NAMES)`，
     **注册表层**就没有写工具。根 Agent 独占一切改动。
  3. **后台并发**：worker 在自己的线程里跑，`spawn` 立即返回句柄，
     根 Agent 可以继续干活，之后用 `wait_agent` 收报告。
  4. **生命周期控制**：`running → completed / failed / closed`，
     上限 3 并发，`close` 会中断正在进行的模型请求。

## 一个容易做错的地方

`close` 必须能覆盖 `completed`：worker 正在跑的时候被关闭，它的收尾逻辑
**不能**再把状态改回 completed。所以 `_run` 在写状态前先检查「是不是已经被关了」。
少了这一条，`close_agent` 会在报告里看到一个「已完成」的 agent——
而它实际上是被中断的。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..abort import AbortSignal
from ..agent_loop import IdentityContextPipeline, run_agent_turn
from ..types import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    ChatMessage,
    ModelAdapter,
)

if TYPE_CHECKING:  # pragma: no cover - 仅在类型检查时需要
    # 运行时不导入：`tools/__init__.py` 会导入本模块，
    # 这里再反向导入 `..tooling` 只是类型需求，不必参与运行时依赖图。
    from ..tooling import ToolRegistry
from .types import (
    MAX_SUB_AGENTS,
    MAX_SUB_AGENT_STEPS,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SubAgentSnapshot,
    WaitResult,
)
from .worker_prompt import build_subagent_prompt


class SubAgentError(Exception):
    """spawn / close 的参数或状态不合法。"""


@dataclass
class _Record:
    id: str
    task: str
    status: str
    started_at: float
    signal: AbortSignal
    thread: threading.Thread | None = None
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None

    def snapshot(self) -> SubAgentSnapshot:
        return SubAgentSnapshot(
            id=self.id,
            task=self.task,
            status=self.status,
            started_at=self.started_at,
            finished_at=self.finished_at,
            result=self.result,
            error=self.error,
        )


class SubAgentManager:
    """进程内的一组只读 worker。"""

    max_concurrent = MAX_SUB_AGENTS

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        cwd: str,
        memory_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._model = model
        # 这里收到的应当是**已经裁剪过的只读子集**；再裁一次是双保险。
        self._tools = tools
        self._cwd = cwd
        self._memory_provider = memory_provider

        self._records: dict[str, _Record] = {}
        self._lock = threading.RLock()
        self._listeners: set[Callable[[], None]] = set()
        self._next_id = 1

    # -- 查询 ---------------------------------------------------------------

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.status == STATUS_RUNNING)

    def list(self) -> list[SubAgentSnapshot]:
        with self._lock:
            return [r.snapshot() for r in self._records.values()]

    def get(self, agent_id: str) -> SubAgentSnapshot:
        with self._lock:
            return self._require(agent_id).snapshot()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅状态变化（TUI 用它刷新页脚）。

        监听器存在 `set` 里，所以调用方传进来的必须是可哈希对象
        （函数、闭包、绑定方法都可以；`dataclass` 实例不行）。
        """
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    # -- 生命周期 -----------------------------------------------------------

    def spawn(self, task: str, parent_signal: AbortSignal | None = None) -> SubAgentSnapshot:
        """创建一个后台 worker 并立即返回它的句柄。"""
        normalized = (task or "").strip()
        if not normalized:
            raise SubAgentError("子 Agent 的任务不能为空。")
        if parent_signal is not None:
            parent_signal.raise_if_aborted()

        with self._lock:
            if self.running_count >= self.max_concurrent:
                raise SubAgentError(
                    f"最多同时运行 {self.max_concurrent} 个子 Agent。"
                )
            record = _Record(
                id=f"agent_{self._next_id}",
                task=normalized,
                status=STATUS_RUNNING,
                started_at=_now(),
                signal=AbortSignal(),
            )
            self._next_id += 1
            self._records[record.id] = record

        # 父信号取消 → 这个 worker 也取消。
        if parent_signal is not None:
            def cancel() -> None:
                with self._lock:
                    if record.status == STATUS_RUNNING:
                        record.status = STATUS_CLOSED
                record.signal.abort(f"父回合被取消：{parent_signal.reason}")

            thread = threading.Thread(target=lambda: (parent_signal.wait(), cancel()), daemon=True)
            thread.start()

        worker = threading.Thread(
            target=self._run, args=(record,), name=f"minicode-{record.id}", daemon=True
        )
        record.thread = worker
        worker.start()
        self._notify()
        return record.snapshot()

    def wait(self, agent_ids: list[str], timeout_ms: int = 30_000) -> WaitResult:
        """等指定 worker 结束。

        超时**不关闭**它们——只返回当前状态，让根 Agent 决定继续等还是关掉。
        """
        with self._lock:
            records = [self._require(agent_id) for agent_id in agent_ids]

        deadline = _now() + max(0.0, timeout_ms / 1000.0)
        for record in records:
            remaining = deadline - _now()
            if remaining <= 0:
                break
            thread = record.thread
            if thread is not None:
                thread.join(remaining)

        with self._lock:
            timed_out = any(r.status == STATUS_RUNNING for r in records)
            return WaitResult(timed_out=timed_out, agents=[r.snapshot() for r in records])

    def close(self, agent_id: str) -> SubAgentSnapshot:
        """中断一个 worker 并等它真正停下。"""
        with self._lock:
            record = self._require(agent_id)
            if record.status == STATUS_RUNNING:
                # 先改状态再 abort：`_run` 的收尾逻辑靠这个状态判断「不该覆盖」。
                record.status = STATUS_CLOSED
                record.signal.abort("Sub-agent closed by root agent")

        thread = record.thread
        if thread is not None:
            # **带超时地等 worker 停下（D43）。**
            #
            # 原版是 `await record.completion`——无条件等。在 JS 里那是有界的：
            # abort 会让 await 中的 Promise 走 catch 分支结束。
            #
            # Python 线程**不能从外部取消**：abort 只是把 signal 置位，worker
            # 要自己检查到才会退出。如果它正卡在不检查 signal 的阻塞调用里
            # （网络、子进程、死循环），无条件 join 会让 `/new`、`/exit`、
            # 以及 `close_all` **一起永久挂住**。
            #
            # 5 秒是经验值：正常收尾在毫秒级。超时后我们仍然把状态标成
            # `closed` 并返回——**用户能继续操作**比「保证线程真的停了」更重要，
            # 而残留线程因为 signal 已置位，会在下一个检查点自行退出。
            thread.join(5.0)

        with self._lock:
            if record.finished_at is None:
                record.finished_at = _now()
            self._notify()
            return record.snapshot()

    def close_all(self) -> None:
        with self._lock:
            running_ids = [
                r.id for r in self._records.values() if r.status == STATUS_RUNNING
            ]
        for agent_id in running_ids:
            try:
                self.close(agent_id)
            except SubAgentError:
                continue

    # -- 内部 ---------------------------------------------------------------

    def _require(self, agent_id: str) -> _Record:
        record = self._records.get(agent_id)
        if record is None:
            raise SubAgentError(f"未知的子 Agent：{agent_id}")
        return record

    def _run(self, record: _Record) -> None:
        try:
            memory_text = ""
            if self._memory_provider is not None:
                try:
                    memory_text = self._memory_provider(self._cwd) or ""
                except Exception:  # noqa: BLE001 - 记忆加载失败不该让 worker 起不来
                    memory_text = ""

            messages: list[ChatMessage] = [
                {"role": ROLE_SYSTEM, "content": build_subagent_prompt(self._cwd, memory_text)},
                {"role": ROLE_USER, "content": record.task},
            ]

            result = run_agent_turn(
                model=self._model,
                tools=self._tools,
                messages=messages,
                cwd=self._cwd,
                signal=record.signal,
                max_steps=MAX_SUB_AGENT_STEPS,
                context_pipeline=IdentityContextPipeline(),
            )

            with self._lock:
                # 被关闭过的 worker 状态不再改写——否则 close_agent 会看到「已完成」。
                if record.status != STATUS_CLOSED:
                    if result.outcome in ("failed", "aborted"):
                        # `run_agent_turn` 把异常转成了 outcome，而不是抛出来。
                        # 不检查这里，失败会被当成成功——那是假的报告。
                        record.status = STATUS_FAILED
                        record.error = (
                            result.error or f"worker 回合以 {result.outcome} 结束"
                        )
                    elif result.outcome == "max_steps":
                        record.status = STATUS_FAILED
                        record.error = (
                            f"worker 达到 {MAX_SUB_AGENT_STEPS} 步上限仍未给出结论"
                        )
                    else:
                        record.status = STATUS_COMPLETED
                        record.result = _last_assistant_message(result.messages)
        except Exception as error:  # noqa: BLE001 - worker 失败必须变成状态而不是崩溃
            with self._lock:
                if record.status != STATUS_CLOSED:
                    record.status = STATUS_FAILED
                    record.error = str(error)
        finally:
            with self._lock:
                record.finished_at = _now()
            self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _last_assistant_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.get("role") == ROLE_ASSISTANT:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return "Sub-agent finished without an assistant report."


def _now() -> float:
    import time

    return time.time()
