"""`update_plan` 与 `update_goal` —— 模型写 Plan、上报 Goal 的唯一入口。

两者都是**控制位工具**的成功范例：

  - `update_plan` 是普通的成功/失败；
  - `update_goal` 在报告 completed / blocked 时返回 `stop=True`，
    这会结束当前回合（受控结束），因为它意味着「这件事有结论了，不必再推进」。

这样 agent_loop 里不需要任何 `if tool_name == "update_goal"` 的特例分支。
"""

from __future__ import annotations

from typing import Any

from ..runtime.goal import GoalError, GoalManager
from ..runtime.plan import PlanError, PlanManager
from ..tooling import ToolContext, ToolDefinition, ToolResult


# ---------------------------------------------------------------------------
# update_plan
# ---------------------------------------------------------------------------

PLAN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "完整清单。更新已有条目时带上其 id；新增条目省略 id。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "已有 Todo 的 id"},
                    "title": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["title", "status"],
            },
        },
        "explanation": {"type": "string", "description": "可选的整体说明"},
    },
    "required": ["todos"],
}


def create_update_plan_tool(plan: PlanManager) -> ToolDefinition:
    def run(data: Any, context: ToolContext) -> ToolResult:
        todos = (data or {}).get("todos")
        if not isinstance(todos, list):
            return ToolResult.failure("todos 必须是数组。")
        try:
            document = plan.update(todos)
        except PlanError as error:
            return ToolResult.failure(str(error))

        completed = sum(1 for todo in document.todos if todo.status == "completed")
        return ToolResult.success(
            f"Plan updated: {completed}/{len(document.todos)} completed."
        )

    return ToolDefinition(
        name="update_plan",
        description=(
            "提交本会话的完整 Todo 清单以跟踪多步工作。"
            "更新已有条目时带上其 id，新增条目省略 id。"
            "同一时刻最多一个条目处于 in_progress。"
        ),
        input_schema=PLAN_TOOL_SCHEMA,
        validator=lambda data: data if isinstance(data, dict) else {},
        run=run,
    )


# ---------------------------------------------------------------------------
# update_goal
# ---------------------------------------------------------------------------

GOAL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["set_criteria", "blocked", "completed"],
            "description": "set_criteria 声明完成标准；blocked 报告受阻；completed 报告完成。",
        },
        "criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "action=set_criteria 时必填：完成标准列表。",
        },
        "reason": {
            "type": "string",
            "description": "action=blocked 时必填：无法继续的原因。",
        },
        "summary": {
            "type": "string",
            "description": "action=completed 时必填：完成情况概述。",
        },
        "checks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "action=completed 时必填：为每条完成标准各给一条实际结果说明。",
        },
    },
    "required": ["action"],
}


def create_goal_tools(manager: GoalManager) -> list[ToolDefinition]:
    """Goal 模式专用的两个工具。没有 Goal 时模型看不到它们。"""

    def update_goal(data: Any, context: ToolContext) -> ToolResult:
        try:
            document = manager.update(data if isinstance(data, dict) else {})
        except GoalError as error:
            return ToolResult.failure(str(error))

        if document.status in ("completed", "blocked"):
            # 控制位：有结论了 → 受控结束当前回合。
            detail = document.completion_summary or document.stop_reason or document.status
            return ToolResult(
                ok=True,
                output=f"Goal {document.status}: {detail}",
                stop=True,
            )
        return ToolResult.success(
            f"Goal 已更新（{document.status}），完成标准 {len(document.completion_criteria)} 条。"
        )

    def get_goal(data: Any, context: ToolContext) -> ToolResult:
        snapshot = manager.get_snapshot()
        if snapshot is None:
            return ToolResult.success("没有 Goal。")
        return ToolResult.success(
            "\n".join(
                [
                    f"status: {snapshot.status}",
                    f"description: {snapshot.description}",
                    f"needs_replan: {snapshot.needs_replan}",
                    f"criteria: {len(snapshot.completion_criteria)}",
                ]
            )
        )

    return [
        ToolDefinition(
            name="update_goal",
            description=(
                "上报当前 Goal 的状态。你**不能**创建、修改或取消 Goal，"
                "只能声明完成标准、报告受阻、或报告完成。"
                "报告 completed 前必须让所有 Todo 完成，并为每条完成标准各给一条实际结果说明。"
            ),
            input_schema=GOAL_TOOL_SCHEMA,
            validator=lambda data: data if isinstance(data, dict) else {},
            run=update_goal,
        ),
        ToolDefinition(
            name="get_goal",
            description="查看当前 Goal 的状态、描述与完成标准数量。",
            input_schema={"type": "object", "properties": {}},
            validator=lambda data: {},
            run=get_goal,
        ),
    ]
