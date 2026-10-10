"""Agent 回合引擎——全项目的核心。

一个「回合」= 一次用户输入到模型交回控制权之间的全部工作。回合内部是一个
**多步工具循环**：

    for step in range(max_steps):
        压缩上下文 → 拼装请求 → model.next() → 解析 AgentStep
          ├─ assistant  → 收尾，返回 final
          └─ tool_calls → 逐个执行工具 → 回填 tool_result → 继续下一轮

这个模块刻意**不知道任何 UI、会话持久化、权限提示的存在**。它只依赖
types / tooling / abort，以及一个可选的 context 管理器。宿主通过回调参数
接收全部输出行为：

    on_tool_start / on_tool_result / on_assistant_message / on_progress_message
    on_context_stats / on_compaction

这条边界是整份代码最重要的设计决定：同一个循环可以接到全屏 TUI、接到行模式
REPL、接到测试里，而循环本身一行都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .abort import AbortSignal, throw_if_aborted
from .context.tool_result_storage import (
    ContentReplacementState,
    PendingToolResult,
    apply_tool_result_budget,
    create_content_replacement_state,
    replace_large_tool_result,
)
from .tooling import ToolContext, ToolRegistry
from .types import (
    ROLE_ASSISTANT,
    ROLE_ASSISTANT_PROGRESS,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    AgentStep,
    AgentTurnResult,
    ChatMessage,
    ModelAdapter,
    ProviderUsage,
    ToolCall,
)

# ---------------------------------------------------------------------------
# 续跑提示词
#
# 当模型「话说到一半就停了」时，循环会注入一条内部 user 消息把它推回工作。
# 这些文案是行为契约的一部分，不要随意改写：它们明确告诉模型「你还没做完」，
# 并强调只有真正完成时才允许给出 <final>。
# ---------------------------------------------------------------------------

CONTINUE_AFTER_PROGRESS = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

CONTINUE_AFTER_TOOL_RESULT = (
    "Continue from your progress update. You have already used tools in this turn, "
    "so treat plain status text as progress, not a final answer. Respond with the "
    "next concrete tool call, code change, or an explicit <final> answer only if "
    "the task is truly complete."
)

RESUME_AFTER_PAUSE = (
    "Resume from the previous pause_turn and continue the task immediately. Produce "
    "the next concrete tool call, code change, or an explicit <final> answer only if "
    "the task is complete."
)

RESUME_AFTER_MAX_TOKENS = (
    "Your previous response hit max_tokens during thinking before producing the next "
    "actionable step. Resume immediately and continue with the next concrete tool "
    "call, code change, or an explicit <final> answer only if the task is complete. "
    "Do not repeat the earlier plan."
)

CONTINUE_AFTER_EMPTY_WITH_TOOLS = (
    "Your last response was empty after recent tool results. Continue immediately by "
    "trying the next concrete step, adapting to any tool errors, or giving an explicit "
    "<final> answer only if the task is complete."
)

CONTINUE_AFTER_EMPTY = (
    "Your last response was empty. Continue immediately with concrete tool calls, code "
    "changes, or an explicit <final> answer only if the task is complete."
)

#: 空响应最多重试几次，之后判定回合失败。
MAX_EMPTY_RESPONSE_RETRIES = 2
#: thinking 被截断（pause_turn / max_tokens）最多续跑几次。
MAX_THINKING_RETRIES = 3


# ---------------------------------------------------------------------------
# 回调面
# ---------------------------------------------------------------------------


@dataclass
class TurnHooks:
    """宿主观察回合内部事件的唯一入口。全部可选。"""

    on_tool_start: Callable[[str, Any], None] | None = None
    on_tool_result: Callable[[str, str, bool], None] | None = None
    on_assistant_message: Callable[[str, bool], None] | None = None
    on_progress_message: Callable[[str], None] | None = None
    on_context_stats: Callable[[Any], None] | None = None
    on_compaction: Callable[[Any], None] | None = None

    def tool_start(self, name: str, data: Any) -> None:
        if self.on_tool_start:
            self.on_tool_start(name, data)

    def tool_result(self, name: str, output: str, is_error: bool) -> None:
        if self.on_tool_result:
            self.on_tool_result(name, output, is_error)

    def assistant(self, content: str, is_final: bool = True) -> None:
        if self.on_assistant_message:
            self.on_assistant_message(content, is_final)

    def progress(self, content: str) -> None:
        if self.on_progress_message:
            self.on_progress_message(content)

    def stats(self, stats: Any) -> None:
        if self.on_context_stats:
            self.on_context_stats(stats)

    def compacted(self, result: Any) -> None:
        if self.on_compaction:
            self.on_compaction(result)

    def merge(self, other: TurnHooks) -> TurnHooks:
        """把另一个 hooks 合并进来（未设置的回调以 other 为准）。"""

        def pick(a: Any, b: Any) -> Any:
            return a if a is not None else b

        return TurnHooks(
            on_tool_start=pick(self.on_tool_start, other.on_tool_start),
            on_tool_result=pick(self.on_tool_result, other.on_tool_result),
            on_assistant_message=pick(
                self.on_assistant_message, other.on_assistant_message
            ),
            on_progress_message=pick(
                self.on_progress_message, other.on_progress_message
            ),
            on_context_stats=pick(self.on_context_stats, other.on_context_stats),
            on_compaction=pick(self.on_compaction, other.on_compaction),
        )


# ---------------------------------------------------------------------------
# 上下文工程挂载点
#
# 四级压缩的确切语义见 docs/spec-context.md。循环只需要知道一个接缝：
# `prepare()` 返回 (真实消息, 模型可见投影)。真正的实现见 context/pipeline.py；
# 这里默认使用恒等变换，因此没有配置上下文工程时循环照常工作。
# ---------------------------------------------------------------------------


class IdentityContextPipeline:
    """不做任何压缩。测试与无模型配置时使用。"""

    def prepare(
        self, messages: list[ChatMessage], *, step: int, model_name: str
    ) -> tuple[list[ChatMessage], list[ChatMessage]]:
        """返回 `(真实消息, 模型可见消息)`。

        两者不同是 context collapse 的核心：collapse 只改写模型可见的投影，
        真实 transcript 保持完整。默认实现让两者相同。
        """
        return messages, messages

    def on_turn_start(self) -> None:
        pass

    def on_turn_end(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: Any | None = None,
    signal: AbortSignal | None = None,
    max_steps: int | None = None,
    model_name: str = "",
    hooks: TurnHooks | None = None,
    context_pipeline: Any | None = None,
    content_replacement_state: ContentReplacementState | None = None,
    plan_context: Any | None = None,
    runtime_context: Any | None = None,
    stop_on_fatal_tool_error: bool = False,
) -> AgentTurnResult:
    """跑完一个回合。永远是「返回结果」而不是「抛异常」。

    调用方通过 `result.outcome` 判断回合为什么结束，通过 `result.messages`
    取得应当写回会话的完整消息列表。

    `content_replacement_state` 建议由宿主在会话级创建并复用：它记住哪些
    大结果已经落盘过，从而保证同一个 toolUseId 永远得到字节相同的替换文本。
    不传则本次回合内部新建（回合结束即忘）。
    """
    hooks = hooks or TurnHooks()
    pipeline = context_pipeline or IdentityContextPipeline()
    replacement_state = content_replacement_state or create_content_replacement_state()
    current = list(messages)

    tool_calls_made = 0
    tool_error_count = 0
    saw_tool_result = False
    empty_response_retries = 0
    thinking_retries = 0

    def finish(outcome: str, error: str | None = None) -> AgentTurnResult:
        # 通知管线回合结束：它要清掉「本回合已 snip」这类回合级标记。
        if hasattr(pipeline, "on_turn_end"):
            pipeline.on_turn_end()
        return AgentTurnResult(
            messages=current,
            outcome=outcome,  # type: ignore[arg-type]
            tool_calls=tool_calls_made,
            error=error,
        )

    # 通知管线回合开始。snip 的「每回合最多成功一次」就是靠这个边界重置的。
    if hasattr(pipeline, "on_turn_start"):
        pipeline.on_turn_start()

    step = 0
    while max_steps is None or step < max_steps:
        try:
            throw_if_aborted(signal)

            # --- 1. 上下文工程：拿到「真实消息」与「模型可见投影」 -------------
            current, model_messages = pipeline.prepare(
                current, step=step, model_name=model_name
            )
            throw_if_aborted(signal)

            # --- 2. 运行时上下文注入（只改投影） -----------------------------
            # 两个注入点都只影响模型可见视图，不写回真实转录——否则每回合都会
            # 往历史里塞一份计划快照，上下文会被自己的状态撑爆。
            if plan_context is not None:
                model_messages = plan_context(model_messages)
            if runtime_context is not None:
                model_messages = runtime_context(model_messages)

            # --- 2. 请求模型 -------------------------------------------------
            next_step = model.next(model_messages, tools=tools.model_tools(), signal=signal)
            throw_if_aborted(signal)
            step += 1

            # --- 3. 模型给出文本 --------------------------------------------
            if next_step.type == "assistant":
                outcome = _handle_assistant_step(
                    step_result=next_step,
                    current=current,
                    hooks=hooks,
                    saw_tool_result=saw_tool_result,
                    empty_response_retries=empty_response_retries,
                    thinking_retries=thinking_retries,
                    tool_error_count=tool_error_count,
                )
                current = outcome.messages
                if outcome.retry_kind == "empty":
                    empty_response_retries += 1
                    continue
                if outcome.retry_kind == "thinking":
                    thinking_retries += 1
                    continue
                if outcome.retry_kind in ("progress", "progress_after_tools"):
                    continue
                return finish(outcome.final_outcome or "final", outcome.error)

            # --- 4. 模型要求调用工具 -----------------------------------------
            assistant_note = _append_tool_call_intent(next_step, current)
            current = assistant_note.messages
            if assistant_note.progress_text:
                hooks.progress(assistant_note.progress_text)
            if assistant_note.emitted_text:
                hooks.assistant(assistant_note.emitted_text, False)

            if not next_step.calls:
                # 既没有文本也没有工具调用：视为模型交回控制权。
                if next_step.content:
                    return finish("final")
                if empty_response_retries < MAX_EMPTY_RESPONSE_RETRIES:
                    empty_response_retries += 1
                    current = _with_continuation(
                        current,
                        CONTINUE_AFTER_EMPTY_WITH_TOOLS
                        if saw_tool_result
                        else CONTINUE_AFTER_EMPTY,
                    )
                    continue
                return finish("failed", "Model returned an empty response.")

            # 先为每个调用占位，保证 tool_use / tool_result 严格配对。
            calls = list(next_step.calls)
            current = current + [
                {
                    "role": ROLE_TOOL_RESULT,
                    "toolUseId": call.id,
                    "toolName": call.toolName,
                    "content": "Cancelled before execution.",
                    "isError": True,
                }
                for call in calls
            ]

            # --- 5. 逐个执行工具，收集结果 -----------------------------------
            batch_outcome: str | None = None
            executed: list[PendingToolResult] = []
            for call in calls:
                if batch_outcome is not None or (signal is not None and signal.is_set()):
                    continue
                tool_calls_made += 1
                hooks.tool_start(call.toolName, call.input)

                result = tools.execute(
                    call.toolName,
                    call.input,
                    ToolContext(cwd=cwd, permissions=permissions, signal=signal),
                )
                saw_tool_result = True
                if not result.ok:
                    tool_error_count += 1

                # 每个调用都已占位，这里只回填内容，保证配对不破。
                current = _replace_tool_result(current, call.id, result.output, not result.ok)
                hooks.tool_result(call.toolName, result.output, not result.ok)

                executed.append(
                    PendingToolResult(
                        role=ROLE_TOOL_RESULT,
                        toolUseId=call.id,
                        toolName=call.toolName,
                        content=result.output,
                        isError=not result.ok,
                    )
                )

                if result.await_user:
                    batch_outcome = "awaiting_user"
                elif result.stop:
                    batch_outcome = "controlled_stop"
                elif result.fatal and stop_on_fatal_tool_error:
                    batch_outcome = "failed"

            # --- 5b. 大结果落盘 ----------------------------------------------
            # 两道串联的闸门：单条超 50K 字符先落盘，整批可见量超 200K 再落盘。
            # 顺序不能反：单条阈值处理完，批预算的输入才是最终大小。
            # 注意这会让 `current` 里的 tool_result 内容变成「预览 + 路径」，
            # 但 `hooks.tool_result` 已经把完整输出交给宿主展示了。
            if executed:
                replaced = [
                    replace_large_tool_result(item, replacement_state)
                    for item in executed
                ]
                budgeted = apply_tool_result_budget(replaced, replacement_state)
                for item in budgeted.results:
                    current = _replace_tool_result(
                        current, item.toolUseId, item.content, item.isError
                    )

            # --- 6. 处理批次的特殊结局 ---------------------------------------
            if batch_outcome == "awaiting_user":
                question = _find_awaiting_question(current)
                if question:
                    hooks.assistant(question, True)
                    current = current + [
                        {"role": ROLE_ASSISTANT, "content": question}
                    ]
                return finish("awaiting_user")

            if signal is not None and signal.is_set():
                return finish("aborted", str(signal.reason or "Execution stopped"))
            if batch_outcome is not None:
                return finish(batch_outcome)

        except Exception as error:  # noqa: BLE001 - 回合失败必须变成结果而不是崩溃
            aborted = signal is not None and signal.is_set()
            return finish("aborted" if aborted else "failed", str(error))

    current = current + [
        {
            "role": ROLE_ASSISTANT,
            "content": "达到最大工具步数限制，已停止当前回合。",
        }
    ]
    hooks.assistant("达到最大工具步数限制，已停止当前回合。", True)
    return finish("max_steps")


# ---------------------------------------------------------------------------
# assistant 分支
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AssistantOutcome:
    messages: list[ChatMessage]
    #: None 表示这个分支已经决定了回合结局。
    retry_kind: str | None = None
    final_outcome: str | None = None
    error: str | None = None


def _handle_assistant_step(
    *,
    step_result: AgentStep,
    current: list[ChatMessage],
    hooks: TurnHooks,
    saw_tool_result: bool,
    empty_response_retries: int,
    thinking_retries: int,
    tool_error_count: int,
) -> _AssistantOutcome:
    """处理「模型返回了文本」的全部情形。

    这是一个纯分类函数：它把模型的响应归入 5 种情形之一，且明确区分
    「模型真的完成了」与「模型话说一半停了」——后者是这个循环健壮性的关键。
    """
    content = step_result.content
    is_empty = not content.strip()

    # 情形 1：模型显式声明还在干活（<progress>），或在有工具结果后给出状态文本。
    if not is_empty and _treat_as_progress(step_result, saw_tool_result):
        hooks.progress(content)
        messages = _append_thinking(current, step_result)
        messages = messages + [
            {"role": ROLE_ASSISTANT_PROGRESS, "content": content}
        ]
        nudge = (
            CONTINUE_AFTER_TOOL_RESULT
            if saw_tool_result and step_result.kind != "progress"
            else CONTINUE_AFTER_PROGRESS
        )
        return _AssistantOutcome(_with_continuation(messages, nudge), retry_kind="progress")

    # 情形 2：空响应，但原因是 thinking 阶段被截断 → 续跑（不是模型的错）。
    if _is_recoverable_thinking_stop(step_result, is_empty) and (
        thinking_retries < MAX_THINKING_RETRIES
    ):
        stop_reason = step_result.diagnostics.stop_reason if step_result.diagnostics else None
        notice = (
            "模型在 thinking 阶段触发 max_tokens，正在继续请求后续步骤..."
            if stop_reason == "max_tokens"
            else "模型返回 pause_turn，正在继续请求后续步骤..."
        )
        hooks.progress(notice)
        messages = _append_thinking(current, step_result)
        messages = messages + [{"role": ROLE_ASSISTANT_PROGRESS, "content": notice}]
        nudge = (
            RESUME_AFTER_PAUSE if stop_reason == "pause_turn" else RESUME_AFTER_MAX_TOKENS
        )
        return _AssistantOutcome(_with_continuation(messages, nudge), retry_kind="thinking")

    # 情形 3：普通空响应 → 重试有限次。
    if is_empty and empty_response_retries < MAX_EMPTY_RESPONSE_RETRIES:
        return _AssistantOutcome(
            _with_continuation(
                current,
                CONTINUE_AFTER_EMPTY_WITH_TOOLS
                if saw_tool_result
                else CONTINUE_AFTER_EMPTY,
            ),
            retry_kind="empty",
        )

    # 情形 4：重试也用尽，仍为空 → 带着诊断信息兜底收尾。
    if is_empty:
        diagnostics = _format_diagnostics(step_result)
        if saw_tool_result:
            if tool_error_count > 0:
                fallback = (
                    f"工具执行后模型返回空响应，已停止当前回合。最近有 {tool_error_count} "
                    f"个工具报错；请重试、调整命令，或让模型改用其他方案。{diagnostics}"
                )
            else:
                fallback = (
                    "工具执行后模型返回空响应，已停止当前回合。请重试，或要求模型继续完成"
                    f"剩余步骤。{diagnostics}"
                )
        else:
            fallback = f"模型返回空响应，已停止当前回合。请重试，或要求模型继续。{diagnostics}"
        hooks.assistant(fallback, True)
        messages = _append_thinking(current, step_result)
        messages = messages + [{"role": ROLE_ASSISTANT, "content": fallback}]
        return _AssistantOutcome(messages, final_outcome="failed")

    # 情形 5：正常的完成响应。
    hooks.assistant(content, True)
    messages = _append_thinking(current, step_result)
    messages = messages + [
        _with_provider_usage({"role": ROLE_ASSISTANT, "content": content}, step_result)
    ]
    return _AssistantOutcome(messages, final_outcome="final")


def _treat_as_progress(step_result: AgentStep, saw_tool_result: bool) -> bool:
    """判断一段文本是「进度」还是「最终答复」。

    规则来自系统提示的响应协议：只有模型**显式**用 `<progress>` 标记时才算进度。
    没有标记的纯文本一律按最终答复处理——否则模型将永远无法结束回合。
    """
    return step_result.kind == "progress"


def _is_recoverable_thinking_stop(step_result: AgentStep, is_empty: bool) -> bool:
    """只有「空响应 + pause_turn/max_tokens + 且确实产生过 thinking 块」才算可恢复。"""
    if not is_empty:
        return False
    diagnostics = step_result.diagnostics
    if diagnostics is None:
        return False
    if diagnostics.stop_reason not in ("pause_turn", "max_tokens"):
        return False
    return "thinking" in diagnostics.block_types or "thinking" in diagnostics.ignored_block_types


def _format_diagnostics(step_result: AgentStep) -> str:
    diagnostics = step_result.diagnostics
    if diagnostics is None:
        return ""
    parts: list[str] = []
    if diagnostics.stop_reason:
        parts.append(f"stop_reason={diagnostics.stop_reason}")
    if diagnostics.block_types:
        parts.append(f"blocks={','.join(diagnostics.block_types)}")
    if diagnostics.ignored_block_types:
        parts.append(f"ignored={','.join(diagnostics.ignored_block_types)}")
    return f" 诊断信息: {'; '.join(parts)}。" if parts else ""


# ---------------------------------------------------------------------------
# 消息拼装辅助
# ---------------------------------------------------------------------------


def _with_provider_usage(message: ChatMessage, step_result: AgentStep) -> ChatMessage:
    """把服务端上报的用量挂到 assistant 侧消息上，作为计量真值。"""
    if step_result.usage is None:
        return message
    copied = dict(message)
    copied["providerUsage"] = {
        "inputTokens": step_result.usage.input_tokens,
        "outputTokens": step_result.usage.output_tokens,
        "totalTokens": step_result.usage.total_tokens,
        "source": step_result.usage.source,
    }
    return copied


def _append_thinking(
    messages: list[ChatMessage], step_result: AgentStep
) -> list[ChatMessage]:
    """保留 thinking 块。

    这些块在下一次请求时必须原样回传，否则模型会丢失推理上下文，
    并可能因为签名校验失败而报错。
    """
    if not step_result.thinking_blocks:
        return messages
    return messages + [
        {"role": "assistant_thinking", "blocks": list(step_result.thinking_blocks)}
    ]


def _with_continuation(messages: list[ChatMessage], content: str) -> list[ChatMessage]:
    return messages + [
        {"role": ROLE_USER, "content": content, "internal": "continuation"}
    ]


@dataclass(slots=True)
class _ToolCallIntent:
    messages: list[ChatMessage]
    #: 模型在调用工具前给出的最终性文本，应作为 assistant 消息展示。
    emitted_text: str | None = None
    #: 模型在调用工具前给出的进度文本，应走 progress 通道展示。
    progress_text: str | None = None


def _append_tool_call_intent(
    step_result: AgentStep, current: list[ChatMessage]
) -> _ToolCallIntent:
    """把「模型决定调用工具」这件事写进消息序列。

    模型可能同时给出文本和工具调用（先说明再动手），两种都要保留：
    文本作为 assistant 消息，调用作为 assistant_tool_call 消息。
    """
    messages = _append_thinking(current, step_result)
    emitted: str | None = None
    progress: str | None = None

    if step_result.content:
        if step_result.content_kind == "progress":
            # 模型在动手前先说了一句进度话：走 progress 通道，不当作最终答复。
            messages = messages + [
                {"role": ROLE_ASSISTANT_PROGRESS, "content": step_result.content}
            ]
            messages = _with_continuation(messages, CONTINUE_AFTER_PROGRESS)
            progress = step_result.content
        else:
            messages = messages + [
                _with_provider_usage(
                    {"role": ROLE_ASSISTANT, "content": step_result.content},
                    step_result,
                )
            ]
            emitted = step_result.content

    for index, call in enumerate(step_result.calls):
        message: ChatMessage = {
            "role": ROLE_ASSISTANT_TOOL_CALL,
            "toolUseId": call.id,
            "toolName": call.toolName,
            "input": call.input,
        }
        # provider usage 只挂在最后一个调用上，避免重复计量。
        if index == len(step_result.calls) - 1:
            message = _with_provider_usage(message, step_result)
        messages = messages + [message]

    return _ToolCallIntent(messages, emitted, progress)


def _replace_tool_result(
    messages: list[ChatMessage], tool_use_id: str, content: str, is_error: bool
) -> list[ChatMessage]:
    return [
        {**message, "content": content, "isError": is_error}
        if message.get("role") == ROLE_TOOL_RESULT
        and message.get("toolUseId") == tool_use_id
        else message
        for message in messages
    ]


def _find_awaiting_question(messages: list[ChatMessage]) -> str:
    """取出 ask_user 提出的问题文本（最后一个 tool_result 的内容）。"""
    for message in reversed(messages):
        if message.get("role") == ROLE_TOOL_RESULT:
            return str(message.get("content", "")).strip()
    return ""
