"""SessionRuntime —— 把回合、Goal、Loop、Plan 编排成一个会话。

它是宿主（TUI / CLI）唯一需要面对的运行时门面。三个子状态机各自有生命周期，
但都必须经过**同一个 TurnRunner 单飞闸门**，因此它们天然互斥。

## 四道互斥闸门

Goal 与 Loop 都想要「自动跑回合」，所以必须有一套明确的优先级规则，
否则会出现「Goal 正在跑，Loop 又起一轮」这种互相踩踏。规则分布在四处，
缺一不可：

  1. `submit()`：Loop 在跑 → 拒绝新输入；Goal 活跃 → 拒绝新输入。
  2. `command()`：创建 Loop 前先检查 Goal 是否活跃。
  3. `assert_idle()`：`/goal resume` 与 `/goal <desc>` 之前统一检查
     「Loop 未启用、回合不忙、没有待回答的问题」。
  4. **`LoopScheduler` 的 `busy` 回调包含 `goal.enabled`** ——这是决定性的一条：
     Goal 活跃期间 Loop 永远不会被 kick。Goal 结束后由 `notify_idle()` 补 kick。

第 4 条是关键设计：与其让 Loop 自己判断「现在能不能跑」，不如把「Goal 是否活跃」
直接喂进它的 busy 判定，让互斥成为 Loop 的**内建性质**而不是外部约定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..abort import AbortSignal
from ..tooling import ToolRegistry
from ..types import AgentTurnResult
from .context import build_goal_context, build_loop_context
from .goal import GoalManager
from .goal_controller import CONTINUATION_TEXT, GoalController
from .loop import (
    DEFAULT_LOOP_INTERVAL_MS,
    LoopError,
    LoopScheduler,
    parse_loop_request,
)
from .plan import PlanManager
from .turn import TurnBusyError, TurnRequest, TurnRunner


class RuntimeError_(RuntimeError):
    """运行时状态不允许该操作。"""


@dataclass
class SessionRuntime:
    """一个会话的运行时门面。"""

    plan: PlanManager
    tools: ToolRegistry
    #: 真正执行一个回合的回调（由宿主编排：注入上下文、落盘、上报 UI）。
    execute: Callable[[TurnRequest], AgentTurnResult]
    #: 记录一条没有对应回合的回答（例如 Goal 暂停时用户先答了）。
    record_answer: Callable[[dict[str, Any]], None]
    notice: Callable[[str], None]
    settle_workers: Callable[[], None]
    is_busy: Callable[[], bool] | None = None
    loop_clock: Any | None = None

    def __post_init__(self) -> None:
        self.turns = TurnRunner()
        self.goal = GoalController(
            GoalManager(self.plan),
            turns=self.turns,
            run_turn=self._run,
            record_answer=self.record_answer,
            notice=self.notice,
            settle_workers=self.settle_workers,
        )
        self.loop = LoopScheduler(
            run_turn=self._run_loop_turn,
            busy=self._loop_busy,
            notice=self.notice,
            settle_workers=self.settle_workers,
            clock=self.loop_clock,
        )

    # -- 工具与上下文 -------------------------------------------------------

    def tools_for(self, mode: str | None) -> ToolRegistry:
        """Goal 模式下额外叠加 Goal 工具。

        这样 `update_goal` 只在确实有 Goal 时可见——没有 Goal 的时候，
        模型看不到这个工具，也就不会去尝试创建一个（它无权创建）。
        """
        from ..tools.goal import create_goal_tools

        if mode == "goal":
            combined = ToolRegistry(list(self.tools.list()))
            combined.add_tools(create_goal_tools(self.goal.manager))
            return combined
        return self.tools

    def context_for(self, mode: str | None) -> str:
        if mode == "goal":
            return build_goal_context(self.goal.manager)
        if mode == "loop":
            return build_loop_context(self.loop.get_snapshot())
        return ""

    # -- 用户输入 -----------------------------------------------------------

    def submit(self, content: str) -> AgentTurnResult:
        """提交一条普通用户输入。"""
        # 闸门 1：Loop / Goal 占用时拒绝普通输入。
        if self.loop.waiting_for_answer:
            self.turns.awaiting_user = False
            self.loop.answer(content)
            return _accepted()
        if self.loop.running:
            raise RuntimeError_("Loop 正在运行。用 /loop stop 后再开始新回合。")
        if self.goal.waiting_for_answer:
            self.goal.answer(content)
            self.turns.awaiting_user = False
            return _accepted()
        if False:
            raise RuntimeError_("Goal 正在运行。用 /goal pause 后再开始新回合。")

        return self._run(TurnRequest(input={"role": "user", "content": content}))

    def command(self, text: str) -> str | None:
        """处理 `/goal` 与 `/loop`。返回 None 表示不是这两个命令之一。

        只有这两个命令在运行时层处理，其余命令由宿主自行分派——因为其余的
        不涉及运行时状态。
        """
        stripped = text.strip()

        if stripped == "/loop" or stripped.startswith("/loop "):
            body = stripped[len("/loop") :].strip()
            if not body:
                return self.loop.format()
            if body == "stop":
                was_waiting = self.loop.waiting_for_answer
                self.loop.stop()
                if was_waiting:
                    self.turns.awaiting_user = False
                return "Loop 已停止。Plan 保留。"
            if body.split(None, 1)[0] in ("stop", "pause", "resume", "status"):
                return "用法：/loop、/loop [Nm|Nh] <prompt>，或 /loop stop。"

            # 闸门 2：创建 Loop 前先确认 Goal 没有占用。
            if self.goal.enabled:
                raise RuntimeError_(
                    "请先暂停或清除 Goal，再启动 Loop；并先回答或清除待处理的问题。"
                )
            prompt, interval = parse_loop_request(body)
            self.loop.create(prompt, interval)
            return self.loop.format()

        if stripped == "/goal" or stripped.startswith("/goal "):
            body = stripped[len("/goal") :].strip()
            if not body or body == "status":
                from .context import format_goal

                return format_goal(
                    self.goal.manager, self.plan.get_snapshot(), self.goal.waiting_for_answer
                )

            parts = body.split(None, 1)
            head = parts[0]
            argument = parts[1].strip() if len(parts) > 1 else ""

            if head == "pause":
                self.goal.pause(argument or "Paused by user.")
            elif head == "clear" and not argument:
                was_waiting = self.goal.waiting_for_answer
                self.goal.clear()
                if was_waiting:
                    self.turns.awaiting_user = False
                return "Goal 已清除。Plan 保留。"
            elif head == "resume" and not argument:
                # 闸门 3：恢复 Goal 前统一检查空闲。
                self.assert_idle()
                self.goal.resume()
            elif head in ("update", "edit", "add", "limit"):
                return (
                    "本 MVP 保持原始目标不变。请用 /goal clear 后再 /goal <description>。"
                )
            elif head in ("status", "resume", "clear"):
                return f"用法：/goal {head}"
            else:
                self.assert_idle()
                self.goal.create(body)

            from .context import format_goal

            return format_goal(
                self.goal.manager, self.plan.get_snapshot(), self.goal.waiting_for_answer
            )

        return None

    def assert_idle(self) -> None:
        """闸门 3：统一的前置检查。"""
        if self.loop.enabled:
            raise RuntimeError_("请先停止 Loop，再启动或恢复 Goal。")
        if self.turns.busy or self.goal.running or (self.is_busy and self.is_busy()):
            raise RuntimeError_("请等当前回合结束。")
        if self.turns.awaiting_user and not self.goal.waiting_for_answer:
            raise RuntimeError_("请先回答待处理的问题。")

    def notify_idle(self) -> None:
        """宿主在空闲时调用：给挂起的 Loop 一个补跑机会。"""
        self.loop.kick()

    # -- 停止与重置 ---------------------------------------------------------

    def stop(self) -> None:
        self.loop.stop()
        self.goal.pause("Execution stopped by user.", force=False)
        self.turns.stop()
        self.settle_workers()

    def reset(self) -> None:
        self.stop()
        self.goal.clear()
        self.turns.awaiting_user = False

    # -- 内部 ---------------------------------------------------------------

    def _loop_busy(self) -> bool:
        """闸门 4：Loop 的忙碌判定包含 Goal 是否活跃。

        这一条让「Goal 优先于 Loop」成为 Loop 的内建性质，而不是外部约定。
        """
        return (
            self.turns.busy
            or self.turns.awaiting_user
            or self.goal.enabled
            or bool(self.is_busy and self.is_busy())
        )

    def _run(self, request: TurnRequest) -> AgentTurnResult:
        """跑一个根回合，并处理 Loop 在普通回合失败后的暂停。"""
        try:
            result = self.turns.run(
                lambda signal: self.execute(
                    TurnRequest(input=request.input, mode=request.mode, signal=signal)
                ),
                request.signal,
            )
        except Exception as error:  # noqa: BLE001 - Loop 的失败暂停需要看到异常
            if request.mode is None and self.loop.enabled:
                self.loop.pause_after_failure(str(error))
            raise

        if (
            request.mode is None
            and self.loop.enabled
            and result.outcome in ("failed", "max_steps", "aborted")
        ):
            self.loop.pause_after_failure(result.error or result.outcome)
        self.loop.kick()
        return result

    def _run_loop_turn(
        self, payload: dict[str, Any], mode: str, signal: AbortSignal
    ) -> AgentTurnResult:
        return self._run(TurnRequest(input=payload, mode=mode, signal=signal))


def _accepted() -> AgentTurnResult:
    """构造一个「已受理但回合由别处跑」的结果占位。

    Goal / Loop 的 `answer()` 本身会启动后续回合，因此 `submit` 在这里
    不需要返回真实回合结果。
    """
    return AgentTurnResult(messages=[], outcome="awaiting_user", tool_calls=0)
