"""子 Agent 工具 —— **仅根 Agent 可见**的四个操作。

它们是「根 Agent 指挥 worker」的全部接口：

    spawn_agent   创建 worker，立即返回句柄（不阻塞）
    list_agents   看所有 worker 的当前状态
    wait_agent    收报告（带超时，超时不关闭）
    close_agent   中断一个 worker

这四个工具**不注入给子 Agent**，因此 worker 无法创建更多 worker——
这是防止「agent 自我繁殖」的结构性保证，不是靠提示词约束。
"""

from __future__ import annotations

import json
from typing import Any

from ..agents.manager import SubAgentError, SubAgentManager
from ..agents.types import MAX_SUB_AGENTS
from ..tooling import ToolContext, ToolDefinition, ToolResult


def _display(agent: Any) -> dict[str, Any]:
    return agent.to_dict()


def create_subagent_tools(manager: SubAgentManager) -> list[ToolDefinition]:
    def spawn_agent(data: Any, context: ToolContext) -> ToolResult:
        task = str((data or {}).get("task", "")).strip()
        try:
            agent = manager.spawn(task, context.signal)
        except SubAgentError as error:
            return ToolResult.failure(str(error))

        return ToolResult.success(
            json.dumps(
                {
                    "agent": _display(agent),
                    "running": manager.running_count,
                    "max_concurrent": manager.max_concurrent,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def list_agents(data: Any, context: ToolContext) -> ToolResult:
        return ToolResult.success(
            json.dumps(
                {
                    "running": manager.running_count,
                    "max_concurrent": manager.max_concurrent,
                    "agents": [_display(a) for a in manager.list()],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def wait_agent(data: Any, context: ToolContext) -> ToolResult:
        ids = (data or {}).get("agent_ids")
        if not isinstance(ids, list) or not ids:
            return ToolResult.failure("agent_ids 必须是非空数组。")
        timeout = (data or {}).get("timeout_ms", 30_000)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0:
            return ToolResult.failure("timeout_ms 必须是非负整数。")

        try:
            result = manager.wait([str(i) for i in ids], timeout)
        except SubAgentError as error:
            return ToolResult.failure(str(error))

        return ToolResult.success(
            json.dumps(
                {
                    "timed_out": result.timed_out,
                    "agents": [_display(a) for a in result.agents],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def close_agent(data: Any, context: ToolContext) -> ToolResult:
        agent_id = str((data or {}).get("agent_id", "")).strip()
        try:
            agent = manager.close(agent_id)
        except SubAgentError as error:
            return ToolResult.failure(str(error))
        return ToolResult.success(
            json.dumps({"agent": _display(agent)}, ensure_ascii=False, indent=2)
        )

    return [
        ToolDefinition(
            name="spawn_agent",
            description=(
                "为一个独立的调研任务启动只读子 Agent。它在后台运行，无法修改代码。"
                f"最多同时运行 {MAX_SUB_AGENTS} 个。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "一个具体、自包含的调研任务。",
                    }
                },
                "required": ["task"],
            },
            validator=lambda data: (
                data if isinstance(data, dict) else {}
            ),
            run=spawn_agent,
        ),
        ToolDefinition(
            name="list_agents",
            description="列出本进程中创建过的所有子 Agent 及其当前状态。",
            input_schema={"type": "object", "properties": {}},
            validator=lambda data: {},
            run=list_agents,
        ),
        ToolDefinition(
            name="wait_agent",
            description=(
                "等待指定的子 Agent 结束并返回它们的报告。"
                "超时只返回最新状态，**不会**关闭它们。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "spawn_agent 返回的子 Agent id。",
                    },
                    "timeout_ms": {
                        "type": "number",
                        "description": "最长等待毫秒数，默认 30000。",
                    },
                },
                "required": ["agent_ids"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=wait_agent,
        ),
        ToolDefinition(
            name="close_agent",
            description=(
                "停止一个正在运行的子 Agent。适用于它陷入循环、已无用、"
                "或不应继续消耗模型调用时。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "要停止的子 Agent id。"}
                },
                "required": ["agent_id"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=close_agent,
        ),
    ]
