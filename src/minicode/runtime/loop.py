"""Loop —— 固定延迟的重复任务。

语义是「跑完一轮，等 N 分钟，再跑下一轮」，**不是**「每 N 分钟触发一次」。
差别在忙时行为：固定延迟永远不会积累待跑的轮次，因此模型慢的时候不会
出现「积压十轮一起爆发」。

默认 10 分钟，最小 1 分钟。

## generation 计数

`create` 与 `stop` 都会把 `_generation` 加一。所有异步回调在动手前先比对自己
捕获的 generation——不匹配说明这个回调属于已经作废的 Loop，直接返回。
这一招解决了「stop 之后旧定时器仍然醒来」的经典竞态，而且不需要在每处
小心地 `clear_timeout`。
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..abort import AbortSignal
from ..types import AgentTurnResult

#: 默认间隔：10 分钟。
DEFAULT_LOOP_INTERVAL_MS = 10 * 60_000
#: 最小间隔：1 分钟。
MIN_LOOP_INTERVAL_MS = 60_000
#: 上限用 32 位有符号毫秒数（约 24.8 天），与原实现一致。
MAX_LOOP_INTERVAL_MS = 2_147_483_647

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"

#: 间隔写法：数字（**含小数**）+ 单位 `m` / `h`。
#:
#: 对应原版 `loop/scheduler.ts` 的 `/^(\d+(?:\.\d+)?)(m|h)$/`。
#: 小数是必须支持的：`/loop 1.5h <prompt>` 表示 90 分钟。
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)(m|h)$")

#: 「看起来像数字但格式不对」的首 token。
#:
#: 对应原版的 `/^[+-]?(?:\d|\.\d)|^(?:Infinity|NaN)(?:m|h)?$/i`。
#:
#: **这个分支不能省。** 少了它，`/loop 5x hello` 会被当成
#: 「prompt 是 `5x hello`、间隔用默认 10 分钟」——用户以为设了间隔，
#: 实际拿到的是默认值，而且 prompt 里混进了一段他没想发出去的文本。
#: 宁可明确报错。
_NUMERIC_LIKE_RE = re.compile(
    r"^[+-]?(?:\d|\.\d)|^(?:Infinity|NaN)(?:m|h)?$", re.IGNORECASE
)


class LoopError(Exception):
    """Loop 参数或状态不合法。"""


def parse_loop_request(body: str) -> tuple[str, int]:
    """解析 `/loop [Nm|Nh] <prompt>`，返回 `(prompt, interval_ms)`。

    逐条对应原版 `parseLoopRequest`（`loop/scheduler.ts:8-26`）：

    - 间隔可省略 → 默认 10 分钟；
    - 间隔写成 `<数字><m|h>`，**数字可以带小数**（`1.5h` = 90 分钟）；
    - 首 token「看起来像数字但格式不对」（`5x` / `-5m` / `Infinity` …）
      → **明确报错**，而不是把它当成 prompt；
    - 间隔必须在 1 分钟到 2147483647 毫秒之间；
    - 指定间隔后必须还有 prompt。

    ## 早先的文法不一样，而且**静默给出错误行为**

    之前用的是 `^(?:(\\d+)h)?(?:(\\d+)m)?$` 加 `(hours*60+minutes)*60000`，
    于是：

        /loop 1.5h do it  原版 → 间隔 90 分钟、prompt 是 "do it"
                         旧版 → 间隔 **10 分钟**、prompt 是 "1.5h do it"
        /loop 5x hello    原版 → 报错
                         旧版 → 间隔 10 分钟、prompt 是 "5x hello"

    用户想要 90 分钟的循环，实际拿到 10 分钟——**不报错，只是不对**。
    旧版还额外支持原版没有的 `1h30m` 写法（已去掉：它与「数字前缀就报错」
    这条规则冲突，而且两种写法表达同一件事没有必要）。
    """
    text = body.strip()
    if not text:
        raise LoopError("用法：/loop [Nm|Nh] <prompt>")

    parts = text.split(None, 1)
    first = parts[0]
    match = _INTERVAL_RE.match(first)

    if match:
        value = float(match.group(1))
        unit = match.group(2)
        interval = int(value * (3_600_000 if unit == "h" else 60_000))
        prompt = text[len(first) :].strip()
    elif _NUMERIC_LIKE_RE.match(first):
        # 见上面：这个分支防止「间隔写错了却被当成 prompt」。
        raise LoopError("间隔格式不对。用分钟（Nm）或小时（Nh），至少 1 分钟。")
    else:
        interval = DEFAULT_LOOP_INTERVAL_MS
        prompt = text

    if interval < MIN_LOOP_INTERVAL_MS or interval > MAX_LOOP_INTERVAL_MS:
        raise LoopError(
            "Loop 间隔必须在 1 分钟到 2147483647 毫秒之间。"
        )
    if not prompt:
        raise LoopError("用法：/loop [Nm|Nh] <prompt>")
    return prompt, interval


@dataclass
class LoopDocument:
    id: str
    prompt: str
    interval_ms: int
    status: str = STATUS_ACTIVE
    next_run_at: float | None = None
    stop_reason: str | None = None


@dataclass
class LoopClock:
    """可注入的时钟。测试用它把等待变成立即发生。"""

    now: Callable[[], float] = lambda: __import__("time").time()
    set_timer: Callable[[Callable[[], None], float], Any] = field(
        default=lambda callback, delay: threading.Timer(delay / 1000.0, callback).start()
    )
    clear_timer: Callable[[Any], None] = field(default=lambda timer: None)


class LoopScheduler:
    """进程内的固定延迟重复任务。"""

    def __init__(
        self,
        *,
        run_turn: Callable[[dict[str, Any], str, AbortSignal], AgentTurnResult],
        busy: Callable[[], bool],
        notice: Callable[[str], None],
        settle_workers: Callable[[], None],
        clock: LoopClock | None = None,
    ) -> None:
        self._run_turn = run_turn
        self._busy = busy
        self._notice = notice
        self._settle_workers = settle_workers
        self._clock = clock or LoopClock()

        self._document: LoopDocument | None = None
        self._timer: Any = None
        self._pending = False
        self._current: AbortSignal | None = None
        self._generation = 0
        self.waiting_for_answer = False

    # -- 状态 ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._current is not None

    @property
    def enabled(self) -> bool:
        return (
            self.running
            or self.waiting_for_answer
            or (self._document is not None and self._document.status == STATUS_ACTIVE)
        )

    @property
    def has_pending_trigger(self) -> bool:
        return self._pending

    def get_snapshot(self) -> dict[str, Any] | None:
        document = self._document
        if document is None:
            return None
        return {
            "id": document.id,
            "prompt": document.prompt,
            "intervalMs": document.interval_ms,
            "status": document.status,
            "nextRunAt": document.next_run_at,
            "stopReason": document.stop_reason,
        }

    # -- 生命周期 -----------------------------------------------------------

    def create(self, prompt: str, interval_ms: int) -> None:
        if self._document is not None or self._current is not None:
            raise LoopError("已存在一个 Loop，请先 /loop stop。")
        if not prompt.strip():
            raise LoopError("Loop 需要非空的 prompt。")
        if interval_ms < MIN_LOOP_INTERVAL_MS or interval_ms > MAX_LOOP_INTERVAL_MS:
            raise LoopError("Loop 间隔非法（最小 1 分钟）。")

        self._generation += 1
        self._document = LoopDocument(
            id=str(uuid.uuid4()),
            prompt=prompt,
            interval_ms=interval_ms,
            status=STATUS_ACTIVE,
        )
        # 第一轮立即可跑（待空闲时 kick）。
        self._pending = True
        self.kick()

    def kick(self) -> None:
        """有空闲时把待跑的一轮发出去。"""
        document = self._document
        if (
            not self._pending
            or self._current is not None
            or self.waiting_for_answer
            or self._busy()
            or document is None
            or document.status != STATUS_ACTIVE
        ):
            return
        self._pending = False
        self._launch({"role": "user", "content": document.prompt})

    def answer(self, content: str) -> None:
        if not self.waiting_for_answer or self._current is not None:
            raise LoopError("Loop 当前不接受回答。")
        self.waiting_for_answer = False
        self._launch({"role": "user", "content": content})

    def pause_after_failure(self, reason: str) -> None:
        if self._document is None:
            return
        self._cancel_timer()
        self._pending = False
        self._document.status = STATUS_PAUSED
        self._document.next_run_at = None
        self._document.stop_reason = reason
        self._notice(f"Loop 已暂停：{reason} 请用 /loop stop 后再创建新的。")

    def stop(self) -> None:
        self._generation += 1
        self._cancel_timer()
        self._pending = False
        self.waiting_for_answer = False
        self._document = None
        controller = self._current
        if controller is not None:
            controller.abort("Loop stopped by user.")

    def format(self) -> str:
        document = self._document
        if document is None:
            return "Loop 正在停止…" if self._current else "没有 Loop。用法：/loop [Nm|Nh] <prompt>。"
        state = ""
        if self.waiting_for_answer:
            state = " (waiting for your answer)"
        elif self._current is not None:
            state = " (running)"
        elif self._pending:
            state = " (pending until idle)"
        lines = [
            f"Loop: {document.status}{state}",
            f"Interval: {document.interval_ms // 60_000}m after completion",
            document.prompt,
        ]
        if document.stop_reason:
            lines.append(document.stop_reason)
        return "\n".join(lines)

    # -- 内部 ---------------------------------------------------------------

    def _launch(self, payload: dict[str, Any]) -> None:
        generation = self._generation
        controller = AbortSignal()
        self._current = controller
        schedule_next = False

        try:
            if controller.is_set() or generation != self._generation:
                return
            result = self._run_turn(payload, "loop", controller)
            if controller.is_set() or generation != self._generation:
                return

            if result.outcome == "awaiting_user":
                self.waiting_for_answer = True
                self._notice("Loop 正在等待你的回答。")
            elif result.outcome == "final":
                schedule_next = True
            else:
                self.pause_after_failure(result.error or result.outcome)
        except Exception as error:  # noqa: BLE001 - 一轮失败不该终结整个 Loop 机制
            if not controller.is_set() and generation == self._generation:
                self.pause_after_failure(str(error))
        finally:
            controller.abort("Loop turn finished.")
            self._settle_workers()
            self._current = None

            document = self._document
            if (
                schedule_next
                and generation == self._generation
                and document is not None
                and document.status == STATUS_ACTIVE
            ):
                # 固定延迟**从本轮结束后**起算（含保存与 worker 结算）。
                delay = document.interval_ms
                document.next_run_at = self._clock.now() + delay / 1000.0

                def fire() -> None:
                    if generation != self._generation:
                        return
                    current = self._document
                    if current is None or current.status != STATUS_ACTIVE:
                        return
                    self._timer = None
                    current.next_run_at = None
                    self._pending = True
                    self.kick()

                self._timer = self._clock.set_timer(fire, delay)

            self.kick()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._clock.clear_timer(self._timer)
            self._timer = None
