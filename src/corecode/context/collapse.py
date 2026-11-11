"""context collapse —— 投影式折叠。

这是四级压缩里**唯一不改动真实转录**的机制。它把一段旧历史在
「模型可见视图」里换成一条摘要，而真实转录、会话日志、UI 全都保持完整。

为什么这个区分很重要？因为另外三级压缩都是**不可逆**的：snip 物理删除、
microcompact 清空正文、auto-compact 用摘要替换前缀。一旦删错，信息就没了。
collapse 则可以随时撤销——它只是一层投影。

实现上就是一个纯函数：`project_collapsed_view(messages, state) -> 模型可见消息`。
agent-loop 把它返回的数组发给模型，真实数组一行不动。

本实现只标记**真正受影响的**部分：

  - 被 span 覆盖的消息：已从投影中移除，不参与计量；
  - span **之前**的消息：完全未被触碰，保留其 usage；
  - span **之后**的消息：其 usage 是在「消息更多」的状态下测得的，现在少了
    整个 span，这个数字已不代表当前投影 → **标记失效**。

这样既修正了过度失效，又保住了正确性：只有真正失效的数字才被丢弃。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

from ..types import (
    ROLE_ASSISTANT_PROGRESS,
    ROLE_ASSISTANT_THINKING,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_CONTEXT_SUMMARY,
    ROLE_SNIP_BOUNDARY,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ChatMessage,
    ModelAdapter,
)
from .constants import (
    BOUNDARY_ROLES,
    CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES,
    CONTEXT_COLLAPSE_MAX_FAILURES,
    CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS,
    CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE,
    CONTEXT_COLLAPSE_TARGET_USAGE,
    CONTEXT_COLLAPSE_UTILIZATION,
)
from .compact import parse_summary_from_response
from .tokens import (
    compute_context_stats,
    estimate_message_tokens,
    estimate_messages_tokens,
    mark_provider_usage_stale,
)

STALE_REASON = (
    "conversation was context-collapsed in the model-visible projection "
    "after this provider usage was recorded"
)

#: 摘要开销估算：约为被折叠区段的 15%，但不低于 128 token。
SUMMARY_OVERHEAD_RATIO = 0.15
SUMMARY_OVERHEAD_FLOOR = 128

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class CollapseSpan:
    """一个已折叠的区间。"""

    id: str
    start_message_id: str
    end_message_id: str
    message_ids: list[str]
    summary: str
    tokens_before: int
    tokens_after: int = 0
    status: str = "staged"  # staged | committed
    created_at: int = 0
    reason: str = "context_pressure"

@dataclass
class ContextCollapseState:
    spans: list[CollapseSpan] = field(default_factory=list)
    enabled: bool = True
    consecutive_failures: int = 0

def create_context_collapse_state() -> ContextCollapseState:
    return ContextCollapseState()

@dataclass(slots=True)
class CollapseCandidate:
    start_index: int
    end_index: int
    start_message_id: str
    end_message_id: str
    message_ids: list[str]
    messages: list[ChatMessage]
    tokens_before: int
    estimated_tokens_after: int
    estimated_tokens_to_save: int

@dataclass(slots=True)
class ContextCollapseResult:
    messages: list[ChatMessage]
    state: ContextCollapseState
    collapsed: bool
    spans: list[CollapseSpan] = field(default_factory=list)

# ---------------------------------------------------------------------------
# 消息身份
# ---------------------------------------------------------------------------

def message_id(message: ChatMessage, index: int) -> str:
    """消息的身份：优先用 `id`，否则用位置合成的伪 id。"""
    explicit = message.get("id")
    return str(explicit) if explicit else f"message-{index}"

def _collapsed_message_ids(state: ContextCollapseState) -> set[str]:
    """所有已覆盖消息的 id（含 staged，与投影排除口径一致）。"""
    ids: set[str] = set()
    for span in state.spans:
        if span.status in ("committed", "staged"):
            ids.update(span.message_ids)
    return ids

def is_collapse_boundary(message: ChatMessage) -> bool:
    return message.get("role") in BOUNDARY_ROLES

# ---------------------------------------------------------------------------
# 分组（与 snip 不同：按 API 轮次聚合）
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CollapseGroup:
    start: int
    end: int
    messages: list[ChatMessage]
    tokens: int
    protected: bool = False

def tool_group_is_closed(messages: list[ChatMessage]) -> bool:
    """双向集合相等判定。

    """
    calls = {
        str(m.get("toolUseId"))
        for m in messages
        if m.get("role") == ROLE_ASSISTANT_TOOL_CALL
    }
    results = {
        str(m.get("toolUseId")) for m in messages if m.get("role") == ROLE_TOOL_RESULT
    }

    if not calls and not results:
        return True
    if not calls or not results:
        return False
    return calls <= results and results <= calls

def build_collapse_groups(messages: list[ChatMessage]) -> list[CollapseGroup]:
    """按「一轮完整往返」聚合：thinking? → tool_call* → tool_result*。"""
    groups: list[CollapseGroup] = []
    index = 0
    total = len(messages)

    while index < total:
        role = messages[index].get("role")
        members: list[ChatMessage] = []

        if role in (ROLE_ASSISTANT_THINKING, ROLE_ASSISTANT_TOOL_CALL):
            members.append(messages[index])
            index += 1
            while index < total and messages[index].get("role") == ROLE_ASSISTANT_TOOL_CALL:
                members.append(messages[index])
                index += 1
            while index < total and messages[index].get("role") == ROLE_TOOL_RESULT:
                members.append(messages[index])
                index += 1

            has_call = any(m.get("role") == ROLE_ASSISTANT_TOOL_CALL for m in members)
            protected = has_call and not tool_group_is_closed(members)
            groups.append(
                CollapseGroup(
                    start=index - len(members),
                    end=index,
                    messages=members,
                    tokens=sum(estimate_message_tokens(m) for m in members),
                    protected=protected,
                )
            )
            continue

        if role == ROLE_TOOL_RESULT:
            # 孤儿结果：不可折叠。
            groups.append(
                CollapseGroup(
                    start=index,
                    end=index + 1,
                    messages=[messages[index]],
                    tokens=estimate_message_tokens(messages[index]),
                    protected=True,
                )
            )
            index += 1
            continue

        groups.append(
            CollapseGroup(
                start=index,
                end=index + 1,
                messages=[messages[index]],
                tokens=estimate_message_tokens(messages[index]),
            )
        )
        index += 1

    return groups

# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------

def build_collapsed_summary_content(span: CollapseSpan) -> str:
    return "\n".join(
        [
            "[Collapsed context summary]",
            f"This summary replaces messages {span.start_message_id} through "
            f"{span.end_message_id} in the model-visible context only.",
            "The original transcript is preserved in the session/UI.",
            "",
            span.summary,
        ]
    )

def build_collapsed_summary_message(span: CollapseSpan) -> ChatMessage:
    return {
        "id": f"collapse-summary-{span.id}",
        "role": ROLE_CONTEXT_SUMMARY,
        "content": build_collapsed_summary_content(span),
        "compressedCount": len(span.message_ids),
        "timestamp": span.created_at,
    }

@dataclass(slots=True)
class _Projection:
    start: int
    end: int
    message: ChatMessage

def project_span(
    messages: list[ChatMessage], span: CollapseSpan
) -> _Projection | None:
    """校验并定位一个 span。任一条件不满足都返回 None（该 span 静默失效）。"""
    if span.status != "committed" or not span.message_ids:
        return None

    index_by_id = {message_id(m, i): i for i, m in enumerate(messages)}

    indices: list[int] = []
    for span_id in span.message_ids:
        position = index_by_id.get(span_id)
        if position is None:
            return None
        indices.append(position)

    # 必须严格连续。
    for previous, current in zip(indices, indices[1:]):
        if current != previous + 1:
            return None

    start = indices[0]
    end = indices[-1] + 1
    if message_id(messages[start], start) != span.start_message_id:
        return None
    if message_id(messages[end - 1], end - 1) != span.end_message_id:
        return None

    return _Projection(start=start, end=end, message=build_collapsed_summary_message(span))

def project_collapsed_view(
    messages: list[ChatMessage], state: ContextCollapseState
) -> list[ChatMessage]:
    """产出模型可见视图。无可用 span 时返回**原列表引用**。"""
    if not state.enabled or not state.spans:
        return messages

    projections = [
        projection
        for span in state.spans
        if (projection := project_span(messages, span)) is not None
    ]
    if not projections:
        return messages

    projections.sort(key=lambda p: p.start)

    result: list[ChatMessage] = []
    occupied: set[int] = set()
    cursor = 0
    last_summary_end = 0

    for projection in projections:
        if any(i in occupied for i in range(projection.start, projection.end)):
            continue

        # span 之前的消息未被触碰：保留其 provider usage。
        while cursor < projection.start:
            result.append(messages[cursor])
            cursor += 1

        result.append(projection.message)
        occupied.update(range(projection.start, projection.end))
        cursor = projection.end
        last_summary_end = projection.end

    # span 之后的消息：其 usage 是在「消息更多」时测得的，现在已不代表当前投影。
    while cursor < len(messages):
        message = messages[cursor]
        if cursor >= last_summary_end and last_summary_end > 0:
            message = mark_provider_usage_stale(message, STALE_REASON)
        result.append(message)
        cursor += 1

    return result

# ---------------------------------------------------------------------------
# 候选选择
# ---------------------------------------------------------------------------

def estimate_collapse_summary_tokens(tokens_before: int) -> int:
    return max(SUMMARY_OVERHEAD_FLOOR, math.ceil(tokens_before * SUMMARY_OVERHEAD_RATIO))

def _desired_tokens_to_save(
    min_tokens_to_save: int, current_tokens: int | None, effective_input: int | None
) -> int:
    if current_tokens is None or effective_input is None or effective_input <= 0:
        return min_tokens_to_save
    deficit = current_tokens - effective_input * CONTEXT_COLLAPSE_TARGET_USAGE
    return max(min_tokens_to_save, math.ceil(deficit))

def find_collapse_candidate(
    messages: list[ChatMessage],
    state: ContextCollapseState,
    *,
    keep_recent_messages: int = CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES,
    min_tokens_to_save: int = CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE,
    current_tokens: int | None = None,
    effective_input: int | None = None,
) -> CollapseCandidate | None:
    """返回**第一个**满足条件的候选区间（按自然顺序，不按大小排序）。"""
    if not messages:
        return None

    last_user_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == ROLE_USER:
            last_user_index = index
            break

    keep_recent_start = max(0, len(messages) - keep_recent_messages)
    protected_start = min(
        keep_recent_start, last_user_index if last_user_index >= 0 else len(messages)
    )
    if protected_start <= 0:
        return None

    collapsed_ids = _collapsed_message_ids(state)
    groups = build_collapse_groups(messages)
    desired = _desired_tokens_to_save(min_tokens_to_save, current_tokens, effective_input)

    run: list[CollapseGroup] = []

    def evaluate(candidate_run: list[CollapseGroup]) -> CollapseCandidate | None:
        if not candidate_run:
            return None
        return _build_candidate_from_groups(
            messages, candidate_run, min_tokens_to_save, desired
        )

    for group in groups:
        blocked = (
            group.protected
            or group.end > protected_start
            or any(is_collapse_boundary(m) for m in group.messages)
            or any(
                message_id(m, group.start + offset) in collapsed_ids
                for offset, m in enumerate(group.messages)
            )
        )
        if blocked:
            found = evaluate(run)
            if found is not None:
                return found
            run = []
            continue
        run.append(group)

    return evaluate(run)

def _build_candidate_from_groups(
    messages: list[ChatMessage],
    run: list[CollapseGroup],
    min_tokens_to_save: int,
    desired: int,
) -> CollapseCandidate | None:
    tokens = 0
    estimated_after = 0
    estimated_save = 0
    end_group_index = -1

    for index, group in enumerate(run):
        tokens += group.tokens
        estimated_after = estimate_collapse_summary_tokens(tokens)
        estimated_save = max(0, tokens - estimated_after)
        end_group_index = index
        if estimated_save >= desired:
            break

    if end_group_index < 0:
        return None

    selected = run[: end_group_index + 1]
    first, last = selected[0], selected[-1]
    selected_messages = messages[first.start : last.end]
    ids = [
        message_id(m, first.start + offset)
        for offset, m in enumerate(selected_messages)
    ]

    estimated_after = estimate_collapse_summary_tokens(tokens)
    estimated_save = max(0, tokens - estimated_after)
    if estimated_save < min_tokens_to_save:
        return None

    return CollapseCandidate(
        start_index=first.start,
        end_index=last.end,
        start_message_id=ids[0],
        end_message_id=ids[-1],
        message_ids=ids,
        messages=selected_messages,
        tokens_before=tokens,
        estimated_tokens_after=estimated_after,
        estimated_tokens_to_save=estimated_save,
    )

# ---------------------------------------------------------------------------
# 摘要请求
# ---------------------------------------------------------------------------

COLLAPSE_SUMMARY_PROMPT = """You are creating a local context-collapse summary for an AI coding session.
The summary will replace only this older message span in the model-visible context.
The original transcript remains preserved outside the model-visible projection.

Produce the final summary in <summary> tags.

Preserve:
- User intent and active goals
- Completed tasks and current state
- Important decisions and constraints
- Tool calls and tool results that still matter
- File reads/writes and code changes, with paths, function names, config names, and commands
- Errors, failures, warnings, and exact messages when relevant
- TODOs, uncertainty, follow-up constraints, and anything still relevant later

Rules:
- Do not invent facts or outcomes
- Do not omit critical paths, function names, configuration keys, file paths, or error text
- Keep it concise, but prefer specificity over vague compression
- This is not a full conversation compact; summarize only the provided span

Messages to summarize:

"""

def build_context_collapse_summary_prompt(conversation_text: str) -> str:
    return COLLAPSE_SUMMARY_PROMPT + conversation_text

def messages_to_collapse_text(messages: list[ChatMessage]) -> str:
    """把待折叠区段渲染成摘要请求的正文。"""
    import json

    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == ROLE_USER:
            parts.append(f"[User]: {message.get('content', '')}")
        elif role in ("assistant", ROLE_ASSISTANT_PROGRESS):
            parts.append(f"[Assistant]: {message.get('content', '')}")
        elif role == ROLE_ASSISTANT_THINKING:
            parts.append("[Assistant Thinking]: preserved provider reasoning block")
        elif role == ROLE_ASSISTANT_TOOL_CALL:
            payload = json.dumps(message.get("input"), ensure_ascii=False, default=str)
            parts.append(
                f"[Tool Call: {message.get('toolName')} {message.get('toolUseId')}]: {payload}"
            )
        elif role == ROLE_TOOL_RESULT:
            suffix = " ERROR" if message.get("isError") else ""
            parts.append(
                f"[Tool Result: {message.get('toolName')} {message.get('toolUseId')}{suffix}]: "
                f"{message.get('content', '')}"
            )
        elif role == ROLE_CONTEXT_SUMMARY:
            parts.append(f"[Context Summary]: {message.get('content', '')}")
        elif role == ROLE_SNIP_BOUNDARY:
            parts.append(f"[Snip Boundary]: {message.get('content', '')}")
        elif role == ROLE_SYSTEM:
            parts.append("[System]: protected system message")
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# 收尾构造器
# ---------------------------------------------------------------------------

def _unchanged(
    messages: list[ChatMessage], state: ContextCollapseState
) -> ContextCollapseResult:
    return ContextCollapseResult(
        messages=messages, state=state, collapsed=False, spans=[]
    )

def _failed(
    messages: list[ChatMessage],
    state: ContextCollapseState,
    max_failures: int,
) -> ContextCollapseResult:
    failures = state.consecutive_failures + 1
    return ContextCollapseResult(
        messages=messages,
        state=ContextCollapseState(
            spans=list(state.spans),
            enabled=False if failures >= max_failures else state.enabled,
            consecutive_failures=failures,
        ),
        collapsed=False,
        spans=[],
    )

def _committed(
    messages: list[ChatMessage],
    state: ContextCollapseState,
    planned: list[CollapseSpan],
) -> ContextCollapseResult:
    committed = [replace(span, status="committed") for span in planned]
    next_state = ContextCollapseState(
        spans=[*state.spans, *committed],
        enabled=state.enabled,
        consecutive_failures=0,
    )
    return ContextCollapseResult(
        messages=project_collapsed_view(messages, next_state),
        state=next_state,
        collapsed=True,
        spans=committed,
    )

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def apply_context_collapse_if_needed(
    messages: list[ChatMessage],
    model: str,
    model_adapter: ModelAdapter | None = None,
    state: ContextCollapseState | None = None,
    *,
    utilization_threshold: float = CONTEXT_COLLAPSE_UTILIZATION,
    max_spans_per_pass: int = CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS,
    max_failures: int = CONTEXT_COLLAPSE_MAX_FAILURES,
) -> ContextCollapseResult:
    """尝试折叠旧历史。返回值里的 `messages` 是**模型可见投影**。

    调用方必须只把 `result.messages` 发给模型，不要用它替换真实转录。
    """
    current_state = state or create_context_collapse_state()

    if not current_state.enabled:
        return _unchanged(messages, current_state)

    current_projected = project_collapsed_view(messages, current_state)
    stats = compute_context_stats(current_projected, model)
    if stats.utilization < utilization_threshold:
        return _unchanged(current_projected, current_state)

    if model_adapter is None:
        # 没有模型适配器就无法生成摘要：不改动，也不计入失败。
        return _unchanged(current_projected, current_state)

    planned: list[CollapseSpan] = []
    span_budget = max(1, int(max_spans_per_pass))

    for pass_index in range(span_budget):
        selection_state = ContextCollapseState(
            spans=[*current_state.spans, *planned],
            enabled=True,
            consecutive_failures=current_state.consecutive_failures,
        )
        projected = project_collapsed_view(messages, selection_state)
        pass_stats = compute_context_stats(projected, model)

        if planned and pass_stats.utilization <= CONTEXT_COLLAPSE_TARGET_USAGE:
            break

        candidate = find_collapse_candidate(
            messages,
            selection_state,
            current_tokens=pass_stats.total_tokens,
            effective_input=pass_stats.effective_input,
        )
        if candidate is None:
            break

        summary_prompt = build_context_collapse_summary_prompt(
            messages_to_collapse_text(candidate.messages)
        )
        try:
            response = model_adapter.next(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a precise assistant that summarizes older "
                            "coding-session context without inventing details."
                        ),
                    },
                    {"role": "user", "content": summary_prompt},
                ]
            )
        except Exception:  # noqa: BLE001 - 摘要失败不该拖垮会话
            return _failed(current_projected, current_state, max_failures)

        content = getattr(response, "content", "") or ""
        if not content.strip():
            return _failed(current_projected, current_state, max_failures)

        summary = parse_summary_from_response(content)
        if not summary:
            return _failed(current_projected, current_state, max_failures)

        now = int(time.time() * 1000)
        draft = CollapseSpan(
            id=f"collapse-{now}-{pass_index}-{candidate.start_message_id}",
            start_message_id=candidate.start_message_id,
            end_message_id=candidate.end_message_id,
            message_ids=list(candidate.message_ids),
            summary=summary,
            tokens_before=candidate.tokens_before,
            tokens_after=0,
            status="staged",
            created_at=now,
            reason="context_pressure",
        )

        summary_tokens = estimate_messages_tokens([build_collapsed_summary_message(draft)])
        tokens_to_save = max(0, candidate.tokens_before - summary_tokens)

        if tokens_to_save < CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE:
            if planned:
                break
            return _failed(current_projected, current_state, max_failures)

        draft.tokens_after = summary_tokens
        planned.append(draft)

    if not planned:
        return _unchanged(current_projected, current_state)

    return _committed(messages, current_state, planned)
