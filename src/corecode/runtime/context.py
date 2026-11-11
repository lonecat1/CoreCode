"""注入了模型上下文的三种「运行时状态」渲染。

这些都是**行为契约**：改了文案就会改变模型的行为。同时它们是三个不同的
注入点，作用范围不同，不要混淆：

    inject_plan_context    每回合都注入（Plan 属于长期状态）
    inject_runtime_context Goal / Loop 模式下按需注入（本回合的模式）
    format_goal / format_loop   给用户看的，不注入模型
"""

from __future__ import annotations

import json
from typing import Any

from .goal import GoalManager
from .plan import PlanDocument, format_plan

# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

GOAL_CONTEXT_LINES = [
    "This is a user-created Goal. Follow its original description and the latest Plan.",
    "If needsReplan is true, call update_plan with a nonempty plan, then update_goal set_criteria.",
    "An ordinary final ends this turn, not the Goal. Active Goals continue in another turn.",
    "Use ask_user for missing information. Use update_goal blocked with a reason when unable to proceed.",
    "When all Todos and criteria are satisfied, use update_goal completed with a summary and "
    "one concrete check explanation per criterion.",
    "Check explanations must describe actual results; this MVP checks structure, not evidence references.",
    "Never change the original objective, invent user approval, or treat Todo completion alone "
    "as Goal completion.",
]

def build_goal_context(manager: GoalManager) -> str:
    """Goal 模式下注入 system 消息的运行时上下文。"""
    snapshot = manager.get_snapshot()
    lines = ["<current_goal>", json.dumps(_goal_to_dict(snapshot), ensure_ascii=False), "</current_goal>"]
    lines.extend(GOAL_CONTEXT_LINES)
    return "\n".join(lines)

def format_goal(
    manager: GoalManager, plan: PlanDocument, waiting: bool = False
) -> str:
    """给用户看的 Goal 状态（含计划快照）。"""
    goal = manager.get_snapshot()
    if goal is None:
        return "No Goal. Use /goal <description> to create one."

    waiting_suffix = " (waiting for your answer)" if waiting else ""
    lines: list[Any] = [
        f"Goal: {goal.status}{waiting_suffix}",
        goal.description,
    ]
    lines.extend(
        f"{index + 1}. {criterion}"
        for index, criterion in enumerate(goal.completion_criteria)
    )
    if goal.needs_replan:
        lines.append("Planning required.")
    if goal.stop_reason:
        lines.append(goal.stop_reason)
    if goal.completion_summary:
        lines.append(goal.completion_summary)
    lines.append(format_plan(plan))
    return "\n".join(str(line) for line in lines if line)

def _goal_to_dict(goal: Any) -> dict[str, Any] | None:
    """

    ## 为什么要委托给 `goal_to_json`

    这个函数原来自己写了一份字段映射，而 `runtime/goal.py` 的 `goal_to_json`
    是给工具输出用的。**两份序列化器**意味着同一份 Goal 在模型眼里有两种形状：
    上下文注入里 `stopReason: null`、`completion: null` 都会出现，工具输出里
    却被省略；`completion` 还少了 `checkedAt`。

    `JSON.stringify` 对 `undefined` 的行为是**省略该键**，不是写 `null`。
    凡是「可能没有」的字段都不该以 `null` 出现——模型会以为「这个字段有值但为
    空」，而不是「这件事还没发生」。
    """
    from .goal import goal_to_json

    return goal_to_json(goal)

def _loop_to_dict(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Loop 快照 → 省略空值的字典。

    `nextRunAt` / `stopReason` 是可选字段，`JSON.stringify` 会**省略**它们；
    """
    if not snapshot:
        return {}
    return {key: value for key, value in snapshot.items() if value is not None}

# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

LOOP_CONTEXT_LINES = [
    "Execute this recurring prompt once, then return a final. The next run is scheduled "
    "after the interval.",
    "Use the shared Plan if useful. Do not create or update a Goal. Ask the user when input is needed.",
    "The user can stop this Loop with /loop stop. There is no model-side stop tool in this MVP.",
]

def build_loop_context(snapshot: dict[str, Any] | None) -> str:
    """Loop 模式的运行时上下文。

    """
    lines = [
        "<current_loop>",
        json.dumps(_loop_to_dict(snapshot), ensure_ascii=False),
        "</current_loop>",
    ]
    lines.extend(LOOP_CONTEXT_LINES)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------

def inject_runtime_context(
    messages: list[dict[str, Any]], context: str
) -> list[dict[str, Any]]:
    """把一段运行时上下文追加进 system 消息；context 为空则原样返回。"""
    if not context:
        return messages
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            updated = list(messages)
            updated[index] = {
                **message,
                "content": f"{message.get('content', '')}\n\n{context}",
            }
            return updated
    return [{"role": "system", "content": context}, *messages]
