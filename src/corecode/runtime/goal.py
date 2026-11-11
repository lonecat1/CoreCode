"""Goal —— 用户拥有的长期目标。

## 为什么把它和 Plan 分开

Plan 是「这轮工作怎么拆」，模型可以自由改。Goal 是「用户到底要什么」，
**模型不能改**——它只能通过 `update_goal` 上报三件事之一：

    set_criteria   声明完成标准（必须先有一个非空的 Plan）
    blocked        报告卡住了及原因
    completed      报告完成，且必须为每条标准给出实际结果说明

这条边界是整个设计里最重要的约束：**目标的所有权在用户手里**。
缺了它，模型会倾向于自己「重新定义」目标然后宣称完成。

## 状态机

    (无) --create--> active
    active --set_criteria--> active (needs_replan = False)
    active --blocked--> blocked
    active --completed--> completed   (要求所有 Todo 已完成 + 标准数匹配)
    active/paused/blocked --pause--> paused --resume--> active
    * --clear--> (无)
    Plan 被替换 --> 非 active 的 Goal 变 paused + needs_replan

最后一条值得注意：计划换了，原来的完成标准就不再对应同一份工作，
因此 Goal 必须暂停并要求重新规划，而不是悄悄沿用旧标准。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .plan import PlanManager

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"

#: `update_goal` 允许的动作。
ACTION_SET_CRITERIA = "set_criteria"
ACTION_BLOCKED = "blocked"
ACTION_COMPLETED = "completed"

VALID_ACTIONS = frozenset({ACTION_SET_CRITERIA, ACTION_BLOCKED, ACTION_COMPLETED})

class GoalError(Exception):
    """Goal 更新不合法。"""

@dataclass(slots=True)
class GoalDocument:
    schema_version: int
    id: str
    session_id: str
    plan_id: str
    description: str
    completion_criteria: list[str] = field(default_factory=list)
    status: str = STATUS_ACTIVE
    needs_replan: bool = True
    stop_reason: str | None = None
    completion_summary: str | None = None
    completion_checks: list[str] = field(default_factory=list)
    #:
    #: 对象里；本实现用三个平铺字段存（更贴近 dataclass 的习惯），
    #: 但**序列化给模型时仍然还原成嵌套形状**——见 `goal_to_json()`。
    completion_checked_at: str | None = None
    updated_at: str = ""

class GoalManager:
    """用户创建的长期目标。**模型无法创建、无法改目标、无法暂停或恢复。**"""

    def __init__(self, plan: PlanManager) -> None:
        self._plan = plan
        self._document: GoalDocument | None = None
        self._plan_prepared = False
        self._listeners: set[Callable[[], None]] = set()

        # 订阅计划变化：计划被替换意味着 Goal 的完成标准已对应不上。
        self._unsubscribe_plan = plan.subscribe(self._on_plan_changed)

    # -- 查询 ---------------------------------------------------------------

    @property
    def plan(self) -> PlanManager:
        """与本 Goal 共享的计划。

        `JSON.stringify({ goal, plan })`）——只给 goal 的话，模型看不到 todo
        的 id 与状态，也就无法决定下一步。
        """
        return self._plan

    def get_snapshot(self) -> GoalDocument | None:
        """返回一份**副本**，调用方改它不会影响内部状态。

        ## 为什么用 `replace()` 而不是逐字段构造

        逐字段构造时，**新加的字段很容易被漏掉**——而且漏了不会报错，只是
        快照里少一个值（本项目在会话落盘上就吃过这个亏，见 D14）。
        加 `completion_checked_at` 时正好又踩到一次：构造函数里没有它，
        于是 `get_snapshot()` 返回的快照丢了刚记录的完成时刻。

        `dataclasses.replace()` 复制全部字段，只需要覆盖**需要深拷贝的容器**
        （两个 list）——这样新增字段时不必记得来改这里。
        """
        if self._document is None:
            return None
        return replace(
            self._document,
            completion_criteria=list(self._document.completion_criteria),
            completion_checks=list(self._document.completion_checks),
        )

    # -- 生命周期（用户侧） --------------------------------------------------

    def create(self, description: str) -> GoalDocument:
        if self._document is not None:
            raise GoalError("已存在一个 Goal，请先 /goal clear。")
        text = description.strip()
        if not text:
            raise GoalError("Goal 描述不能为空。")

        plan = self._plan.get_snapshot()
        self._plan_prepared = False
        self._document = GoalDocument(
            schema_version=1,
            id=str(uuid.uuid4()),
            session_id=plan.session_id,
            plan_id=plan.id,
            description=text,
            completion_criteria=[],
            status=STATUS_ACTIVE,
            needs_replan=True,
            updated_at=_now_iso(),
        )
        self._notify()
        return self.get_snapshot()  # type: ignore[return-value]

    def pause(self, reason: str, force: bool = False) -> None:
        goal = self._document
        if goal is None:
            return
        # 已完成的 Goal 不能被普通 pause 改回 paused（除非 force）。
        if goal.status == STATUS_COMPLETED and not force:
            return
        self._change(
            status=STATUS_PAUSED,
            stop_reason=reason,
            **({"completion_summary": None, "completion_checks": []} if force else {}),
        )

    def resume(self) -> None:
        goal = self._require()
        if goal.status == STATUS_COMPLETED:
            raise GoalError("该 Goal 已完成，请先 clear 再创建新的。")
        self._change(status=STATUS_ACTIVE, stop_reason=None)

    def clear(self) -> None:
        self._document = None
        self._plan_prepared = False
        self._notify()

    def dispose(self) -> None:
        self._unsubscribe_plan()

    # -- 模型侧：唯一的上报入口 ----------------------------------------------

    def update(self, payload: dict[str, Any]) -> GoalDocument:
        """处理 `update_goal` 工具调用。

        只有三种动作，且 `completed` 的校验最严——必须所有 Todo 完成、
        且为每条完成标准各给一条说明。这是「不许草率宣布完成」的实现方式。
        """
        if not isinstance(payload, dict):
            raise GoalError("update_goal 的输入必须是对象")

        action = payload.get("action")
        if action not in VALID_ACTIONS:
            raise GoalError(
                f"未知的 action：{action!r}（应为 {sorted(VALID_ACTIONS)}）"
            )

        goal = self._require()
        if goal.status != STATUS_ACTIVE:
            raise GoalError("Goal 必须处于 active 才能更新。")

        plan = self._plan.get_snapshot()

        if action == ACTION_SET_CRITERIA:
            criteria = _require_string_list(payload, "criteria")
            if not self._plan_prepared or not plan.todos:
                raise GoalError(
                    "请先用 update_plan 提交一个非空的计划，再设置完成标准。"
                )
            self._change(completion_criteria=criteria, needs_replan=False)
            return self.get_snapshot()  # type: ignore[return-value]

        if action == ACTION_BLOCKED:
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise GoalError("blocked 必须给出非空的 reason。")
            self._change(status=STATUS_BLOCKED, stop_reason=reason.strip())
            return self.get_snapshot()  # type: ignore[return-value]

        # completed
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise GoalError("completed 必须给出非空的 summary。")
        checks = _require_string_list(payload, "checks")

        if goal.needs_replan:
            raise GoalError("尚未完成规划（needs_replan 为真），无法宣布完成。")
        if not goal.completion_criteria:
            raise GoalError("尚未设置完成标准，无法宣布完成。")
        if not plan.todos:
            raise GoalError("计划为空，无法宣布完成。")
        unfinished = [t.title for t in plan.todos if t.status != "completed"]
        if unfinished:
            raise GoalError(f"仍有未完成的 Todo：{'、'.join(unfinished)}")
        if len(checks) != len(goal.completion_criteria):
            raise GoalError(
                f"需要为每条完成标准各给一条说明："
                f"{len(goal.completion_criteria)} 条标准，收到 {len(checks)} 条说明。"
            )

        self._change(
            status=STATUS_COMPLETED,
            stop_reason=None,
            completion_summary=summary.strip(),
            completion_checks=checks,
            completion_checked_at=_now_iso(),
        )
        return self.get_snapshot()  # type: ignore[return-value]

    # -- 订阅 ---------------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    # -- 内部 ---------------------------------------------------------------

    def _on_plan_changed(self) -> None:
        goal = self._document
        if goal is None:
            return
        plan = self._plan.get_snapshot()

        if goal.plan_id != plan.id:
            # 计划被整体替换（/new、/fork、reset）→ 目标失去依据。
            self.clear()
            return

        self._plan_prepared = True
        if goal.status != STATUS_ACTIVE:
            # 计划内容变了但 Goal 不在 active：要求重新规划。
            self._change(
                status=STATUS_PAUSED,
                needs_replan=True,
                completion_summary=None,
                stop_reason="Plan changed. Resume to review the plan and completion criteria.",
            )

    def _require(self) -> GoalDocument:
        if self._document is None:
            raise GoalError("当前没有 Goal。请先 /goal <description>。")
        return self._document

    def _change(self, **patch: Any) -> None:
        goal = self._require()
        for key, value in patch.items():
            setattr(goal, key, value)
        goal.updated_at = _now_iso()
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

def _require_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise GoalError(f"{key} 必须是非空字符串数组。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise GoalError(f"{key} 里存在空的条目。")
        result.append(item.strip())
    return result

def _now_iso() -> str:
    import time

    milliseconds = int(time.time() * 1000)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(milliseconds / 1000))
    return f"{stamp}.{milliseconds % 1000:03d}Z"

def goal_to_json(goal: GoalDocument | None) -> dict[str, Any] | None:
    """

    `schemaVersion` / `sessionId` / `planId` / `completionCriteria` /
    `needsReplan` / `stopReason` / `updatedAt`。模型见过的是这些名字。

    **② `completion` 是嵌套对象。** 本实现用三个平铺字段
    （`completion_summary` / `completion_checks` / `completion_checked_at`）
    存储——更贴近 dataclass 的习惯——但**发给模型时还原成**

    可选字段（`stopReason` / `completion`）在无值时**省略**，写 `null`
    会让模型以为「该字段有值但为空」。
    """
    if goal is None:
        return None
    payload: dict[str, Any] = {
        "schemaVersion": goal.schema_version,
        "id": goal.id,
        "sessionId": goal.session_id,
        "planId": goal.plan_id,
        "description": goal.description,
        "completionCriteria": list(goal.completion_criteria),
        "status": goal.status,
        "needsReplan": goal.needs_replan,
        "updatedAt": goal.updated_at,
    }
    if goal.stop_reason is not None:
        payload["stopReason"] = goal.stop_reason
    if goal.completion_summary is not None:
        payload["completion"] = {
            "summary": goal.completion_summary,
            "checks": list(goal.completion_checks),
            "checkedAt": goal.completion_checked_at or "",
        }
    return payload
