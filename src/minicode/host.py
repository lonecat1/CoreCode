"""会话宿主 —— 把上下文工程、会话持久化、权限、子 Agent 接到一个回合上。

**这是整条链路的接缝处。** 前面六个模块各自都是干净的小单元，但「跑一个回合」
需要有人把它们串起来，这个类就是那个人：

    一个回合的执行顺序
    ├─ permissions.begin_turn(signal)      让审批能被取消
    ├─ 构造 ContextPipeline（复用会话级状态）
    ├─ run_agent_turn(...)                 核心循环
    │    ├─ plan_context      注入 Plan 快照
    │    ├─ runtime_context   注入 Goal / Loop 状态
    │    └─ 工具通过 ToolContext 拿到 permissions
    ├─ 压缩回调 → append_compact_boundary / append_snip_boundary
    ├─ permissions.end_turn()
    └─ save_session(...)                   落盘
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .abort import AbortSignal
from .agent_loop import TurnHooks, run_agent_turn
from .agents.manager import SubAgentManager
from .config import RuntimeConfig
from .context.collapse import ContextCollapseState
from .context.pipeline import ContextPipeline
from .context.tool_result_storage import (
    ContentReplacementState,
    create_content_replacement_state,
)
from .evolution import EvolvedMemory, ReflectionResult
from .permissions import PermissionDenied, PermissionManager, PermissionRequest
from .runtime.context import (
    build_goal_context,
    build_loop_context,
    inject_runtime_context,
)
from .runtime.plan import PlanManager, inject_plan_context
from .runtime.session import SessionRuntime
from .runtime.turn import TurnRequest
from .skill_routing import SkillRouter
from .stats import SessionStats, TurnStats, TurnTimer
from .session import (
    append_compact_boundary,
    append_context_collapse_span,
    append_snip_boundary,
    load_context_collapse_state,
    load_session,
    save_session,
)
from .tooling import ToolContext, ToolRegistry
from .types import AgentTurnResult, ChatMessage, ModelAdapter
from .workspace import resolve_tool_path


@dataclass
class HostCallbacks:
    """宿主向 UI 暴露的观察点。全部可选。"""

    on_assistant: Callable[[str, bool], None] | None = None
    #: 流式正文增量。适配器支持流式时会被逐段调用；否则从不调用。
    on_assistant_delta: Callable[[str], None] | None = None
    on_progress: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, Any], None] | None = None
    on_tool_result: Callable[[str, str, bool], None] | None = None
    on_context_stats: Callable[[Any], None] | None = None
    on_notice: Callable[[str], None] | None = None
    #: 回合结束后回调，参数是累计统计（用于显示耗时与工具调用次数）。
    on_turn_stats: Callable[[Any], None] | None = None


@dataclass
class ToolCallCounter:
    """回合内的工具调用计数。

    单独成类而不是用 `nonlocal` 变量：计数要跨 `_hooks` 与 `execute` 传递，
    用一个可变对象比闭包捕获更清楚，也更容易测试。
    """

    count: int = 0


class SessionHost:
    """一个会话的全部运行时状态与接线。"""

    def __init__(
        self,
        *,
        cwd: str,
        model: ModelAdapter,
        runtime_config: RuntimeConfig | None,
        permissions: PermissionManager,
        tools: ToolRegistry,
        plan: PlanManager,
        session_id: str,
        messages: list[ChatMessage] | None = None,
        callbacks: HostCallbacks | None = None,
        subagents: SubAgentManager | None = None,
        evolved_memory: EvolvedMemory | None = None,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.runtime_config = runtime_config
        self.permissions = permissions
        self.tools = tools
        self.skill_router = SkillRouter(cwd, tool_definitions=tools.list())
        self.plan = plan
        self.session_id = session_id
        self.callbacks = callbacks or HostCallbacks()
        self.subagents = subagents
        #: 自进化记忆：回合后沉淀，下一回合/下一步按需召回。
        self.evolved_memory = evolved_memory or EvolvedMemory(cwd)
        self.last_reflection_error: str | None = None
        self.last_memory_error: str | None = None
        #: 会话累计统计（回合数 / 总耗时 / 工具调用次数）。
        self.stats = SessionStats()

        self.messages: list[ChatMessage] = list(messages or [])
        self.already_saved_count = 0
        self._lock = threading.RLock()
        self.is_busy = False
        #: MCP 后台连接的完成事件（由 CLI 注入）。None = 本次没有 MCP。
        self._mcp_done: threading.Event | None = None

        # 会话级状态：跨回合复用，因此「已 snip 过」「大结果已落盘」这些记忆不丢。
        self.content_replacement_state: ContentReplacementState = (
            create_content_replacement_state()
        )
        self.collapse_state = ContextCollapseState()
        self.pipeline = ContextPipeline(
            model_adapter=model,
            collapse_state=self.collapse_state,
            on_stats=self.callbacks.on_context_stats,
            on_snip=self._on_snip,
            on_collapse=self._on_collapse,
            on_auto_compact=self._on_auto_compact,
        )

        self.runtime = SessionRuntime(
            plan=plan,
            tools=tools,
            execute=self.execute,
            record_answer=self._record_answer,
            notice=self._notice,
            settle_workers=self._settle_workers,
            is_busy=lambda: self.is_busy,
        )
        if self.subagents is not None:
            self.subagents.set_fork_context_provider(self._fork_context_snapshot)
            self.subagents.set_skill_router(self.skill_router)

    def _fork_context_snapshot(self) -> list[ChatMessage]:
        """给 fork worker 一份只含文本对话的不可变快照。"""
        with self._lock:
            return [
                dict(message)
                for message in self.messages[1:]
                if message.get("role") in ("user", "assistant")
                and not message.get("internal")
            ]

    # -- 会话装载 -----------------------------------------------------------

    def resume(self, session_id: str) -> bool:
        """切换到另一个会话。成功返回 True。"""
        loaded = load_session(self.cwd, session_id)
        if not loaded:
            return False

        with self._lock:
            self.session_id = session_id
            self.plan.reset(session_id)
            # 恢复折叠状态（enabled / 失败计数不持久化，恢复后一律启用）。
            restored = load_context_collapse_state(self.cwd, session_id)
            self.collapse_state = restored or ContextCollapseState()
            self.pipeline.collapse_state = self.collapse_state

            self.messages = [
                {"role": "system", "content": self._system_prompt()},
                *loaded,
            ]
            self.already_saved_count = len(loaded)
        return True

    def fork(self, session_id: str) -> str | None:
        from .session import fork_session

        return fork_session(self.cwd, session_id)

    # -- 一个回合 -----------------------------------------------------------

    def execute(self, request: TurnRequest) -> AgentTurnResult:
        """跑一个根回合。这是 `SessionRuntime` 唯一调用的方法。

        计时覆盖整个回合（模型调用 + 工具执行 + 压缩），**不包含**用户思考
        的时间——那两者混在一起得出的数字没有意义。
        """
        with self._lock:
            self.is_busy = True
        timer = TurnTimer().start()
        counter = ToolCallCounter()
        try:
            signal = request.signal
            self.permissions.begin_turn(signal)
            try:
                with self._lock:
                    self.messages = [*self.messages, request.input]
                    turn_start = max(0, len(self.messages) - 1)

                result = run_agent_turn(
                    model=self.model,
                    tools=self.runtime.tools_for(request.mode),
                    messages=self.messages,
                    cwd=self.cwd,
                    permissions=self.permissions,
                    signal=signal,
                    max_steps=50 if request.mode else None,
                    model_name=(self.runtime_config.model if self.runtime_config else ""),
                    hooks=self._hooks(counter),
                    context_pipeline=self.pipeline,
                    content_replacement_state=self.content_replacement_state,
                    plan_context=lambda msgs: inject_plan_context(
                        msgs, self.plan.get_snapshot()
                    ),
                    runtime_context=(
                        (lambda msgs: inject_runtime_context(msgs, self._runtime_context(request.mode)))
                        if request.mode
                        else None
                    ),
                    memory_context=self._memory_context,
                    skill_context=self._skill_context,
                    stop_on_fatal_tool_error=bool(request.mode),
                )

                with self._lock:
                    self.messages = list(result.messages)
                self._persist()
                self._reflect_after_turn(
                    turn_start,
                    request.input,
                    result.outcome,
                )

                # 统计只记**根回合**的调用。`AgentTurnResult.tool_calls` 由
                # agent loop 自己累计，比在这里重新数一遍更准确——它知道
                # 哪些调用真的执行了。`counter` 作为兜底（适配器可能没填）。
                self.stats.record(
                    TurnStats(
                        duration_seconds=timer.stop(),
                        tool_calls=max(counter.count, int(result.tool_calls or 0)),
                        outcome=str(result.outcome),
                    )
                )
                if self.callbacks.on_turn_stats:
                    self.callbacks.on_turn_stats(self.stats)
                return result
            finally:
                self.permissions.end_turn()
                self.pipeline.on_turn_end()
        finally:
            try:
                self.evolved_memory.flush()
            except Exception:  # noqa: BLE001 - 访问统计落盘失败不能影响回合结果
                pass
            with self._lock:
                self.is_busy = False

    def _hooks(self, counter: "ToolCallCounter | None" = None) -> TurnHooks:
        callbacks = self.callbacks

        def on_tool_start(name: str, data: Any) -> None:
            if counter is not None:
                counter.count += 1
            if callbacks.on_tool_start:
                callbacks.on_tool_start(name, data)

        return TurnHooks(
            on_tool_start=on_tool_start,
            on_tool_result=callbacks.on_tool_result,
            on_assistant_message=callbacks.on_assistant,
            on_progress_message=callbacks.on_progress,
            # 流式回调直接透传。适配器不支持流式时它不会被调用，
            # 界面也就自动退化为「等完整回答」——功能不受影响。
            on_assistant_delta=callbacks.on_assistant_delta,
        )

    def _runtime_context(self, mode: str | None) -> str:
        if mode == "goal":
            return build_goal_context(self.runtime.goal.manager)
        if mode == "loop":
            return build_loop_context(self.runtime.loop.get_snapshot())
        return ""

    def _memory_context(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """按当前任务和最近工具错误召回长期记忆，只改模型可见投影。"""
        try:
            context = self.evolved_memory.build_context(messages)
            self.last_memory_error = None
        except Exception as error:  # noqa: BLE001 - 记忆库故障不能中断任务
            self.last_memory_error = f"{type(error).__name__}: {error}"
            return messages
        return inject_runtime_context(messages, context) if context else messages

    def _skill_context(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """按当前任务二阶段召回 Skill，并只注入精排后的紧凑卡片。"""
        try:
            context = self.skill_router.context_for_messages(messages)
        except Exception:  # noqa: BLE001 - 路由失败不能中断任务
            return messages
        return inject_runtime_context(messages, context) if context else messages

    def _reflect_after_turn(
        self,
        turn_start: int,
        user_message: ChatMessage,
        outcome: str,
    ) -> None:
        """回合结束后沉淀记忆；记忆故障不影响已经完成的回合。"""
        with self._lock:
            turn_messages = list(self.messages[turn_start:])
            session_id = self.session_id
        user_text = (
            ""
            if user_message.get("internal")
            else str(user_message.get("content") or "")
        )
        try:
            reflected: ReflectionResult = self.evolved_memory.reflect_turn(
                turn_messages,
                session_id=session_id,
                user_text=user_text,
                outcome=outcome,
            )
            self.last_reflection_error = None
        except Exception as error:  # noqa: BLE001 - 记忆沉淀失败不能回滚任务
            self.last_reflection_error = f"{type(error).__name__}: {error}"
            return
        if reflected.skills:
            self._refresh_skills()

    def _refresh_skills(self) -> None:
        """让本轮自动生长的 Skill 立即进入系统提示与 load_skill 目录。"""
        from .skills import discover_skills

        try:
            skills = [
                {
                    "name": item.name,
                    "description": item.description,
                    "path": item.path,
                    "source": item.source,
                }
                for item in discover_skills(self.cwd)
            ]
        except Exception:  # noqa: BLE001 - 技能刷新失败不影响工具执行
            return
        self.tools.set_skills(skills)
        self.skill_router.refresh(tool_definitions=self.tools.list())
        if self.subagents is not None:
            self.subagents.set_skill_router(self.skill_router)
        self.refresh_system_prompt()

    # -- 压缩回调（把边界事件落盘） ------------------------------------------

    def _on_snip(self, result: Any) -> None:
        boundary = getattr(result, "boundary_message", None)
        if boundary is not None:
            append_snip_boundary(self.cwd, self.session_id, boundary)
        self.already_saved_count = 0

    def _on_collapse(self, result: Any) -> None:
        for span in getattr(result, "spans", []) or []:
            append_context_collapse_span(self.cwd, self.session_id, span)

    def _on_auto_compact(self, result: Any) -> None:
        retained = [
            m
            for m in result.messages
            if m.get("role") != "system" and m is not result.summary
        ]
        append_compact_boundary(
            self.cwd,
            self.session_id,
            str(result.summary.get("content", "")),
            "auto",
            result.tokens_before,
            result.tokens_after,
            retained_messages=retained,
        )
        self.already_saved_count = max(0, len(result.messages) - 1)

    # -- 落盘 ---------------------------------------------------------------

    def _persist(self) -> None:
        try:
            save_session(
                self.cwd,
                self.session_id,
                self.messages,
                self.already_saved_count,
            )
        except OSError as error:
            # 落盘失败不应该让回合结果丢失——内存里仍然是完整的。
            # 但要给出**可诊断**的信息：最常见的原因是目录不可写（权限/沙箱），
            # 或 MINICODE_HOME 指向了不存在的位置。原先把异常吞掉只说
            # 「落盘失败」，排查时完全看不出原因。
            self._notice(f"警告：会话落盘失败（{type(error).__name__}: {error}）")
        self.already_saved_count = max(0, len(self.messages) - 1)

    def _record_answer(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.messages = [*self.messages, payload]
        self._persist()

    def _notice(self, message: str) -> None:
        if self.callbacks.on_notice:
            self.callbacks.on_notice(message)

    def _settle_workers(self) -> None:
        if self.subagents is not None:
            self.subagents.close_all()

    # -- MCP ----------------------------------------------------------------

    def attach_mcp_completion(self, done: threading.Event) -> None:
        """让宿主知道后台 MCP 连接何时完成（CLI 在装配时调用）。"""
        self._mcp_done = done

    def _mcp_wait(self, timeout: float = 10.0) -> bool:
        """等待后台 MCP 连接完成。返回是否已完成。

        存在的理由：MCP 在后台连接（避免阻塞启动），但脚本与测试需要知道
        「现在可以认为 MCP 就绪了」。没有这个等待点，调用方只能 sleep 猜时间。
        """
        if self._mcp_done is None:
            return True
        return self._mcp_done.wait(timeout)

    # -- system prompt ------------------------------------------------------

    def _system_prompt(self) -> str:
        from .prompt import build_system_prompt

        return build_system_prompt(
            self.cwd,
            self.permissions.get_summary(),
            skills=self.tools.skills() or None,
            mcp_servers=self.tools.mcp_servers() or None,
            subagents={"maxConcurrent": self.subagents.max_concurrent}
            if self.subagents is not None
            else None,
        )

    def refresh_system_prompt(self) -> None:
        """重建 system 消息（模型/技能/MCP 状态可能变过）。"""
        with self._lock:
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0] = {
                    "role": "system",
                    "content": self._system_prompt(),
                }
            else:
                self.messages = [
                    {"role": "system", "content": self._system_prompt()},
                    *self.messages,
                ]

    # -- 停止 ---------------------------------------------------------------

    def stop(self) -> None:
        self.runtime.stop()

    def reset(self) -> None:
        self.runtime.reset()
        with self._lock:
            self.messages = [{"role": "system", "content": self._system_prompt()}]
            self.already_saved_count = 0
            self.collapse_state = ContextCollapseState()
            self.pipeline.collapse_state = self.collapse_state
            self.content_replacement_state = create_content_replacement_state()
