"""ask_user —— 唯一一个「不做事、只挂起」的工具。

它体现了工具契约里控制位的价值：模型需要澄清时调用它，agent loop 检测到
`await_user` 就结束当前回合并等待用户输入，模型侧不需要任何特殊分支。

注意：系统提示要求模型**必须**用它提问，而不是把问题写成普通 assistant 文本——
因为普通文本会终结回合，用户看到问题时上下文已经交回了，语义不如显式挂起清晰。
"""

from __future__ import annotations

from typing import Any

from ..tooling import ToolContext, ToolDefinition, ToolResult


def _run_ask_user(data: Any, context: ToolContext) -> ToolResult:
    question = str((data or {}).get("question", "")).strip()
    return ToolResult(
        ok=True,
        output=question,
        await_user=True,
    )


def _validate_ask_user(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("字段 question 必须是非空字符串")
    return {"question": question.strip()}


ask_user_tool = ToolDefinition(
    name="ask_user",
    description="向用户提一个澄清问题，并挂起当前回合直到用户回答。",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
    validator=_validate_ask_user,
    run=_run_ask_user,
)
