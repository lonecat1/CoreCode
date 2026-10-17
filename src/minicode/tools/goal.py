"""`update_plan` 与 `update_goal` —— 模型写 Plan、上报 Goal 的唯一入口。

两者都是**控制位工具**的成功范例：

  - `update_plan` 是普通的成功/失败；
  - `update_goal` 在报告 completed / blocked 时返回 `stop=True`，
    这会结束当前回合（受控结束），因为它意味着「这件事有结论了，不必再推进」。

这样 agent_loop 里不需要任何 `if tool_name == "update_goal"` 的特例分支。
"""

from __future__ import annotations

import json
from typing import Any

from ..runtime.goal import GoalError, GoalManager
from ..runtime.plan import PlanError, PlanManager
from ..tooling import ToolContext, ToolDefinition, ToolResult


def _plan_to_json(document: Any) -> dict[str, Any]:
    """把 PlanDocument 转成与原版一致的 JSON 形状。

    字段名用**驼峰**（`schemaVersion` / `sessionId` / `updatedAt`）而不是
    snake_case：这是模型可见的输出，原版用的是 JSON.stringify 直接序列化
    驼峰字段。改成 snake_case 会让「同一条计划在两种实现下的表示不同」，
    而模型对字段名的预期来自它见过的训练数据。
    """
    return {
        "schemaVersion": getattr(document, "schema_version", 1),
        "id": document.id,
        "sessionId": document.session_id,
        "todos": [
            {"id": todo.id, "title": todo.title, "status": todo.status}
            for todo in document.todos
        ],
        "updatedAt": getattr(document, "updated_at", ""),
    }


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

        # **输出整份 plan 文档的 JSON**，而不是一句「N/M 完成」。
        #
        # 对应原版 `tools/plan.ts:34-38`：
        #     JSON.stringify({ ...updated, explanation: input.explanation })
        #
        # 为什么不能是一行摘要：模型下一次要**改已有条目**就必须知道它们的 id。
        # 只回「1/3 completed」时模型拿不到 id，于是只能靠「省略 id」新增——
        # 想更新却静默地变成了新增，ToDo 越改越多。
        # `explanation` 也一并回显：schema 里有这个字段，模型传了却收不到
        # 任何反馈，等于让它以为写进去了。
        explanation = (data or {}).get("explanation")
        payload = _plan_to_json(document)
        if isinstance(explanation, str) and explanation.strip():
            payload["explanation"] = explanation.strip()
        return ToolResult.success(json.dumps(payload, ensure_ascii=False))

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
