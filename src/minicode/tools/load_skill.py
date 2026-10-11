"""load_skill —— 按需加载一个技能的工作流正文。

技能**不**预先注入上下文：系统提示里只有名称与一行描述，模型判断某个技能
相关时才调用这个工具读取全文。这样即使装了 50 个技能，也只在实际用到时付费。

输出格式刻意固定（SKILL / SOURCE / PATH + 空行 + 正文），因为模型要靠
PATH 知道正文来自哪个文件，后续可能需要读它旁边的资源。
"""

from __future__ import annotations

from typing import Any

from ..skills import SkillError, load_skill, validate_skill_name
from ..tooling import ToolContext, ToolDefinition, ToolResult


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    return {"name": name.strip()}


def create_load_skill_tool(cwd: str) -> ToolDefinition:
    def run(data: Any, context: ToolContext) -> ToolResult:
        try:
            skill = load_skill(cwd, data["name"])
        except SkillError as error:
            return ToolResult.failure(str(error))

        if skill is None:
            return ToolResult.failure(f"Unknown skill: {data['name']}")

        return ToolResult.success(
            "\n".join(
                [
                    f"SKILL: {skill.name}",
                    f"SOURCE: {skill.source}",
                    f"PATH: {skill.path}",
                    "",
                    skill.content,
                ]
            )
        )

    return ToolDefinition(
        name="load_skill",
        description=(
            "读取一个具名 SKILL.md 的完整内容，以便准确遵循该工作流。"
            "系统提示里列出了可用技能的名称与描述。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（即目录名）"}
            },
            "required": ["name"],
        },
        validator=_validate,
        run=run,
    )
