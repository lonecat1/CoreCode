"""中心化多 Agent 工具 —— **仅根 Agent 可见**的七个操作。

它们是「根 Agent 指挥 worker」的全部接口：

    spawn_agent   创建 worker，立即返回句柄（不阻塞）
    spawn_team    创建扁平角色分组
    list_agents   看所有 worker 的当前状态
    wait_agent    收报告（带超时，超时不关闭）
    close_agent   中断一个 worker
    inspect_agent_changes  审阅 worktree patch
    apply_agent_changes    审批后合并 patch

这些工具**不注入给子 Agent**，因此 worker 无法创建更多 worker——
这是防止「agent 自我繁殖」的结构性保证，不是靠提示词约束。
"""

from __future__ import annotations

import json
from typing import Any

from ..agents.manager import SubAgentError, SubAgentManager
from ..agents.types import (
    AGENT_MODES,
    AGENT_ROLES,
    MAX_SUB_AGENTS,
    MODE_RESEARCH,
    MODE_WORKTREE,
    ROLE_INVESTIGATOR,
    STATUS_COMPLETED,
)
from ..tooling import ToolContext, ToolDefinition, ToolResult


def _display(agent: Any) -> dict[str, Any]:
    return agent.to_dict()


def create_subagent_tools(manager: SubAgentManager) -> list[ToolDefinition]:
    def spawn_agent(data: Any, context: ToolContext) -> ToolResult:
        task = str((data or {}).get("task", "")).strip()
        mode = str((data or {}).get("mode") or MODE_RESEARCH)
        role = str((data or {}).get("role") or ROLE_INVESTIGATOR)
        team = str((data or {}).get("team") or "").strip()
        acceptance = str((data or {}).get("acceptance") or "").strip()
        try:
            agent = manager.spawn(
                task,
                context.signal,
                mode=mode,
                role=role,
                team_id=team,
                team_name=team,
                acceptance=acceptance,
            )
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
        team = str((data or {}).get("team") or "").strip()
        agents = manager.list()
        if team:
            agents = [
                item
                for item in agents
                if item.team_id == team or item.team_name == team
            ]
        return ToolResult.success(
            json.dumps(
                {
                    "running": manager.running_count,
                    "max_concurrent": manager.max_concurrent,
                    "agents": [_display(a) for a in agents],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def spawn_team(data: Any, context: ToolContext) -> ToolResult:
        name = str((data or {}).get("name") or "").strip()
        members = (data or {}).get("members")
        if not isinstance(members, list):
            return ToolResult.failure("members 必须是数组。")
        try:
            team_id, agents = manager.spawn_team(name, members, context.signal)
        except SubAgentError as error:
            return ToolResult.failure(str(error))
        return ToolResult.success(
            json.dumps(
                {
                    "team_id": team_id,
                    "team_name": name,
                    "agents": [_display(agent) for agent in agents],
                    "running": manager.running_count,
                    "max_concurrent": manager.max_concurrent,
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
        delete_worktree = bool((data or {}).get("delete_worktree", False))
        try:
            agent = manager.close(agent_id, delete_worktree=delete_worktree)
        except SubAgentError as error:
            return ToolResult.failure(str(error))
        return ToolResult.success(
            json.dumps({"agent": _display(agent)}, ensure_ascii=False, indent=2)
        )

    def inspect_agent_changes(data: Any, context: ToolContext) -> ToolResult:
        agent_id = str((data or {}).get("agent_id", "")).strip()
        try:
            inspected = manager.inspect_changes(agent_id)
        except SubAgentError as error:
            return ToolResult.failure(str(error))
        return ToolResult.success(
            json.dumps(inspected, ensure_ascii=False, indent=2)
        )

    def apply_agent_changes(data: Any, context: ToolContext) -> ToolResult:
        agent_id = str((data or {}).get("agent_id", "")).strip()
        cleanup = bool((data or {}).get("cleanup_worktree", False))
        if not agent_id:
            return ToolResult.failure("agent_id 不能为空。")

        # 结构上要求主 Agent 先审阅 patch；随后仍走根权限审批。
        try:
            state = manager.get(agent_id)
        except SubAgentError as error:
            return ToolResult.failure(str(error))
        if state.mode != MODE_WORKTREE:
            return ToolResult.failure(f"{agent_id} 不是 worktree worker。")
        if state.status != STATUS_COMPLETED:
            return ToolResult.failure(
                f"只有 completed worktree worker 才能合并；当前状态 {state.status}。"
            )
        if not state.acceptance:
            return ToolResult.failure("缺少 acceptance 验收标准，拒绝合并。")
        if not state.inspected:
            return ToolResult.failure("必须先调用 inspect_agent_changes 审阅 patch。")

        if context.permissions is not None:
            try:
                context.permissions.ensure_command(
                    "git",
                    ["apply", "--binary", "-"],
                    context.cwd,
                    force_prompt_reason=(
                        f"Apply inspected changes from sub-agent {agent_id} "
                        "to the root workspace"
                    ),
                )
            except Exception as error:  # noqa: BLE001 - 审批拒绝要变成工具失败
                return ToolResult.failure(f"合并审批未通过：{error}")

        try:
            applied = manager.apply_changes(agent_id)
        except SubAgentError as error:
            return ToolResult.failure(str(error))

        if cleanup:
            try:
                applied["agent"] = _display(manager.remove_worktree(agent_id))
            except SubAgentError as error:
                applied["cleanup_error"] = str(error)
        return ToolResult.success(
            json.dumps(applied, ensure_ascii=False, indent=2)
        )

    return [
        ToolDefinition(
            name="spawn_agent",
            description=(
                "启动一个受主 Agent 控制的子 Agent。research/fork 只读；"
                "worktree 在隔离 Git 工作树内编码，必须提供 acceptance 验收标准。"
                f"最多同时运行 {MAX_SUB_AGENTS} 个。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "一个具体、自包含的任务。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(AGENT_MODES),
                        "description": "research=只读调研，fork=对话快照分叉，worktree=隔离编码。",
                    },
                    "role": {
                        "type": "string",
                        "enum": list(AGENT_ROLES),
                        "description": "扁平团队中的职责标签。",
                    },
                    "team": {
                        "type": "string",
                        "description": "可选团队名；同名 workers 会被主 Agent 归为一组。",
                    },
                    "acceptance": {
                        "type": "string",
                        "description": "worktree 模式必填的可验收标准。",
                    },
                },
                "required": ["task"],
            },
            validator=lambda data: (
                data if isinstance(data, dict) else {}
            ),
            run=spawn_agent,
        ),
        ToolDefinition(
            name="spawn_team",
            description=(
                "一次创建 1-3 个扁平 Agent Team 成员。成员间没有消息通道；"
                "主 Agent 统一等待、审阅和合并。worktree 成员必须提供 acceptance。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "团队名称。"},
                    "members": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_SUB_AGENTS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "mode": {
                                    "type": "string",
                                    "enum": list(AGENT_MODES),
                                },
                                "role": {
                                    "type": "string",
                                    "enum": list(AGENT_ROLES),
                                },
                                "acceptance": {"type": "string"},
                            },
                            "required": ["task"],
                        },
                    },
                },
                "required": ["name", "members"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=spawn_team,
        ),
        ToolDefinition(
            name="list_agents",
            description="列出本进程中创建过的子 Agent；可按 team 筛选。",
            input_schema={
                "type": "object",
                "properties": {
                    "team": {"type": "string", "description": "可选 team_id 或 team_name。"}
                },
            },
            validator=lambda data: data if isinstance(data, dict) else {},
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
                "或不应继续消耗模型调用时。可同时删除其 worktree。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "要停止的子 Agent id。"},
                    "delete_worktree": {
                        "type": "boolean",
                        "description": "是否丢弃并删除隔离 worktree，默认 false。",
                    },
                },
                "required": ["agent_id"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=close_agent,
        ),
        ToolDefinition(
            name="inspect_agent_changes",
            description=(
                "主 Agent 审阅 worktree worker 的 patch、改动文件和质量报告。"
                "只有调用过它之后才允许合并。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "worktree worker id。"}
                },
                "required": ["agent_id"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=inspect_agent_changes,
        ),
        ToolDefinition(
            name="apply_agent_changes",
            description=(
                "把已审阅的 worktree patch 应用到根工作区。会请求用户审批；"
                "应用前先做 git apply --check，失败不会留下半合并状态。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "worktree worker id。"},
                    "cleanup_worktree": {
                        "type": "boolean",
                        "description": "合并成功后是否删除隔离 worktree，默认 false。",
                    },
                },
                "required": ["agent_id"],
            },
            validator=lambda data: data if isinstance(data, dict) else {},
            run=apply_agent_changes,
        ),
    ]
