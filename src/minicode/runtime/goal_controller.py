"""GoalController —— 把「目标」变成跨回合的自动推进。

## 一个 final 不是终点

这是本模块最反直觉、也最关键的一点：

> **模型给出一个正常的 final，只是结束了「这一回合」，不是结束了「这个目标」。**

用户说「把项目重构完」，模型不可能在一个回合里做完。因此 Goal 模式下，
控制器会在一轮结束后**继续起下一轮**，每轮注入一句
`Continue the active Goal using its latest state and Plan.`，直到：

  - 模型调用 `update_goal completed` 或 `blocked`；
  - 模型 `ask_user`（挂起等你回答）；
  - 出现失败 / 达到步数上限 / 被取消；
  - 连续 3 轮「没调用任何工具」——说明它在空转，停下来。

最后那条尤其重要：没有它，一个卡住的模型会永远重复「我继续努力」。

## 线程模型

`TurnRunner.run` 是**阻塞**的，所以 Goal 的推进跑在独立线程里：

    主线程：接收输入、处理命令、保持 UI 响应
    Goal 线程：串行地跑一个个回合

两者靠同一把 TurnRunner 单飞闸门互斥，因此不会并发跑回合。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..abort import AbortSignal
from ..types import AgentTurnResult
from .goal import GoalManager
from .turn import TurnBusyError, TurnRequest, TurnRunner

#: continuation 注入的文案。**行为契约**，改写会改变模型的续跑方式。
CONTINUATION_TEXT = (
    "Continue the active Goal using its latest state and Plan. Take the next concrete "
    "step, ask_user if needed, or explicitly report completed/blocked."
)

#: 连续多少轮「没有工具调用」后自动暂停。
MAX_EMPTY_CONTINUATION_TURNS = 3

#: 一次 `_start` 最多自动推进多少轮。
#:
#: 这是**兜底闸门**，不是常规限制：正常路径由 completed / blocked / ask_user
#: 结束。但若模型每轮都调用工具、又永不宣布完成，没有这个上限就会无限跑下去
#: （而且要花钱）。空转检测挡不住这种情况——它只看「有没有工具调用」。
MAX_AUTOMATIC_TURNS = 50


class GoalController:
    """Goal 的自动推进循环。"""

    def __init__(
        self,
        manager: GoalManager,
        *,
        turns: TurnRunner,
        run_turn: Callable[[TurnRequest], AgentTurnResult],
        record_answer: Callable[[dict[str, Any]], None],
        notice: Callable[[str], None],
        settle_workers: Callable[[], None],
    ) -> None:
        self.manager = manager
        self._turns = turns
        self._run_turn = run_turn
        self._record_answer = record_answer
        self._notice = notice
        self._settle_workers = settle_workers

        self._controller: AbortSignal | None = None
        self._thread: threading.Thread | None = None
        self.waiting_for_answer = False

    # -- 状态 ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def enabled(self) -> bool:
        """Goal 是否占据着「活跃工作」的位置。

        这个判定被 Loop 的互斥逻辑使用：Goal 活跃期间 Loop 不会起跑。
        """
        if self.running or self.waiting_for_answer:
            return True
        snapshot = self.manager.get_snapshot()
        return snapshot is not None and snapshot.status == "active"

    def when_idle(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- 用户侧 -------------------------------------------------------------

    def create(self, description: str) -> None:
        """`/goal <description>`：创建并以描述作为首轮输入。"""
        self.manager.create(description)
        self._start({"role": "user", "content": f"/goal {description}"})

    def resume(self) -> None:
        if self.running:
            raise RuntimeError("Goal 仍在运行或正在停止。")
        if self.waiting_for_answer:
            raise RuntimeError("请先回答待处理的问题，再恢复 Goal。")
        self.manager.resume()
        self._start({"role": "user", "content": CONTINUATION_TEXT})

    def answer(self, content: str) -> None:
        """用户回答了 `ask_user` 提出的问题。"""
        if self.running:
            raise RuntimeError("请等当前回合结束。")
        payload: dict[str, Any] = {"role": "user", "content": content}
        snapshot = self.manager.get_snapshot()
        if snapshot is not None and snapshot.status == "paused":
            # 暂停态下只记下回答，不自动续跑——用户要显式 /goal resume。
            self._record_answer(payload)
            self.waiting_for_answer = False
            self._notice("回答已保存。Goal 仍处于暂停，用 /goal resume 继续。")
            return
        self.waiting_for_answer = False
        self._start(payload)

    def pause(self, reason: str = "Paused by user.", force: bool = True) -> None:
        self.manager.pause(reason, force=force)
        controller = self._controller
        if controller is not None:
            controller.abort(reason)
        self.when_idle(timeout=5.0)

    def clear(self) -> None:
        self.pause("Goal cleared.")
        self.manager.clear()
        self.waiting_for_answer = False

    # -- 内部 ---------------------------------------------------------------

    def _start(self, payload: dict[str, Any]) -> None:
        if self.running:
            raise RuntimeError("Goal 仍在运行。")

        controller = AbortSignal()
        self._controller = controller

        def body() -> None:
            next_payload = payload
            empty_turns = 0
            automatic_turns = 0
            try:
                while (
                    not controller.is_set()
                    and (snapshot := self.manager.get_snapshot()) is not None
                    and snapshot.status == "active"
                ):
                    try:
                        result = self._run_turn(
                            TurnRequest(input=next_payload, mode="goal", signal=controller)
                        )
                    except TurnBusyError:
                        # 单飞闸门拒绝了：说明别的回合在跑。稍后重试而不是崩掉。
                        if controller.wait(0.2):
                            break
                        continue

                    if result.outcome == "awaiting_user":
                        self.waiting_for_answer = True
                        self._notice("Goal 正在等待你的回答。")
                        break
                    if controller.is_set():
                        break
                    if result.outcome in ("failed", "max_steps", "aborted"):
                        self.manager.pause(
                            result.error or f"Goal stopped: {result.outcome}.", force=True
                        )
                        break

                    snapshot = self.manager.get_snapshot()
                    if snapshot is None or snapshot.status != "active":
                        break
                    if result.outcome != "final":
                        self.manager.pause("Goal stopped without an ordinary final.")
                        break

                    # 空转检测：连续多轮没调用工具说明卡住了。
                    is_internal = bool(next_payload.get("internal"))
                    if is_internal and result.tool_calls == 0:
                        empty_turns += 1
                    else:
                        empty_turns = 0
                    if empty_turns >= MAX_EMPTY_CONTINUATION_TURNS:
                        self.manager.pause(
                            f"Paused after {MAX_EMPTY_CONTINUATION_TURNS} automatic turns "
                            "without a tool call."
                        )
                        break

                    automatic_turns += 1
                    if automatic_turns >= MAX_AUTOMATIC_TURNS:
                        self.manager.pause(
                            f"Paused after reaching the automatic turn budget "
                            f"({MAX_AUTOMATIC_TURNS} turns). Resume to continue."
                        )
                        break

                    # 让出控制权：既给取消信号一个生效窗口，也给其他线程机会。
                    if controller.wait(0):
                        break
                    next_payload = {
                        "role": "user",
                        "content": CONTINUATION_TEXT,
                        "internal": "goal",
                    }
            except Exception as error:  # noqa: BLE001 - 控制器不能把异常抛到线程外
                if not controller.is_set():
                    self.manager.pause(str(error), force=True)
            finally:
                controller.abort("Goal execution stopped.")
                self._settle_workers()
                snapshot = self.manager.get_snapshot()
                if snapshot is not None and not self.waiting_for_answer:
                    detail = snapshot.completion_summary or snapshot.stop_reason or ""
                    self._notice(f"Goal {snapshot.status}: {detail}")
                self._controller = None

        thread = threading.Thread(target=body, name="minicode-goal", daemon=True)
        self._thread = thread
        thread.start()
