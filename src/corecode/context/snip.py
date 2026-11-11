"""snip compact —— 确定性删除中段历史。

在上下文利用率达到 70% 时，**物理删除**一段中间消息，并在原位插入一条
`snip_boundary` 标记。全程不调用模型，因此是确定性的、可预测的。

这是四级里唯一「真的把历史从转录里拿掉」的操作，所以护栏最重。核心思路是
先把消息切成**不可分割的组**，再标出所有受保护的组，最后只在「连续未受保护」
的空隙里删东西。受保护的理由有四类：

  1. **未闭合的工具调用 / 孤儿工具结果** —— 删掉会让 tool_use 与 tool_result
     失去配对，下一次 API 请求直接被拒。
  2. **文件编辑附近** —— 删掉「我改过什么」会让模型重复改动同一个文件。
  3. **错误附近** —— 删掉报错会让模型重复踩同一个坑。
  4. **边界消息**（system / 已有摘要）—— 删掉会破坏对话结构。

此外还保护每个这类组**前后各一组**，因为上下文往往是跨组的。

最后有一道**净收益校验**：算上插入边界消息的开销后，如果没真正省下 2000 token，
就整体放弃、原样返回。这避免了「删了半天反而没省」的情况。

"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..types import (
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_SNIP_BOUNDARY,
    ROLE_TOOL_RESULT,
    ChatMessage,
)
from .constants import (
    BOUNDARY_ROLES,
    ERROR_MARKERS,
    PROTECTED_TOOL_NAMES,
    SNIP_COMPACT_THRESHOLD,
    SNIP_KEEP_RECENT_MESSAGES,
    SNIP_MIN_MESSAGES_TO_REMOVE,
    SNIP_MIN_TOKENS_TO_FREE,
    SNIP_TARGET_USAGE,
)
from .tokens import (
    ContextStats,
    estimate_message_tokens,
    estimate_messages_tokens,
    js_round,
    mark_provider_usage_stale,
    token_count_with_estimation,
)

STALE_REASON = "conversation was snip-compacted after this provider usage was recorded"

#: 会被「重要错误」文案保护的角色（不含 tool_result —— 那条单独判定）。
_TEXT_ERROR_ROLES = frozenset(
    {
        "user",
        "assistant",
        "assistant_progress",
        "context_summary",
        "snip_boundary",
    }
)

@dataclass(slots=True)
class SnipResult:
    messages: list[ChatMessage]
    did_snip: bool
    tokens_before: int
    tokens_after: int
    tokens_freed: int
    removed_message_ids: list[str] = field(default_factory=list)
    boundary_message: ChatMessage | None = None
    reason: str | None = None

@dataclass(slots=True)
class MessageGroup:
    start: int
    end: int
    messages: list[ChatMessage]
    tokens: int
    protected: bool = False
    reasons: list[str] = field(default_factory=list)

@dataclass(slots=True)
class SafeRun:
    groups: list[MessageGroup]
    start: int
    end: int
    messages_count: int
    tokens: int

@dataclass(slots=True)
class _Deletion:
    start: int
    end: int
    tokens: int
    messages_count: int

# ---------------------------------------------------------------------------
# 谓词
# ---------------------------------------------------------------------------

def is_boundary_message(message: ChatMessage) -> bool:
    return message.get("role") in BOUNDARY_ROLES

def is_protected_tool_name(tool_name: str) -> bool:
    """文件编辑类工具名判定。

    """
    normalized = (tool_name or "").strip().lower()
    return (
        normalized in PROTECTED_TOOL_NAMES
        or "patch" in normalized
        or "write" in normalized
        or "edit" in normalized
        or "modify" in normalized
    )

def _tool_result_looks_like_error(message: ChatMessage) -> bool:
    if message.get("isError") is True:
        return True
    content = str(message.get("content", "")).lower()
    return any(marker in content for marker in ERROR_MARKERS)

def _text_looks_like_error(message: ChatMessage) -> bool:
    if message.get("role") not in _TEXT_ERROR_ROLES:
        return False
    content = str(message.get("content", "")).lower()
    return any(marker in content for marker in ERROR_MARKERS)

def _group_has_protected_tool(group: MessageGroup) -> bool:
    for message in group.messages:
        if message.get("role") in (ROLE_ASSISTANT_TOOL_CALL, ROLE_TOOL_RESULT):
            if is_protected_tool_name(str(message.get("toolName", ""))):
                return True
    return False

def _group_has_important_error(group: MessageGroup) -> bool:
    for message in group.messages:
        if _text_looks_like_error(message):
            return True
        if message.get("role") == ROLE_TOOL_RESULT and _tool_result_looks_like_error(
            message
        ):
            return True
    return False

# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

def build_message_groups(messages: list[ChatMessage]) -> list[MessageGroup]:
    """把消息切成不可分割的组。

    `assistant_tool_call` + 紧邻配对的 `tool_result` 合成一组；未配对的和
    孤儿 `tool_result` 各自成组并标为受保护。
    """
    groups: list[MessageGroup] = []
    index = 0
    total = len(messages)

    while index < total:
        message = messages[index]

        if message.get("role") == ROLE_ASSISTANT_TOOL_CALL:
            following = messages[index + 1] if index + 1 < total else None
            paired = (
                following is not None
                and following.get("role") == ROLE_TOOL_RESULT
                and following.get("toolUseId") == message.get("toolUseId")
            )
            if paired:
                members = [message, following]
                groups.append(
                    MessageGroup(
                        start=index,
                        end=index + 2,
                        messages=members,
                        tokens=sum(estimate_message_tokens(m) for m in members),
                    )
                )
                index += 2
            else:
                groups.append(
                    MessageGroup(
                        start=index,
                        end=index + 1,
                        messages=[message],
                        tokens=estimate_message_tokens(message),
                        protected=True,
                        reasons=["unclosed_tool_call"],
                    )
                )
                index += 1
            continue

        if message.get("role") == ROLE_TOOL_RESULT:
            # 走到这里说明它没有被前一条 call 消费掉。
            groups.append(
                MessageGroup(
                    start=index,
                    end=index + 1,
                    messages=[message],
                    tokens=estimate_message_tokens(message),
                    protected=True,
                    reasons=["orphan_tool_result"],
                )
            )
            index += 1
            continue

        groups.append(
            MessageGroup(
                start=index,
                end=index + 1,
                messages=[message],
                tokens=estimate_message_tokens(message),
            )
        )
        index += 1

    return groups

# ---------------------------------------------------------------------------
# 候选区间
# ---------------------------------------------------------------------------

def find_candidate_range(messages: list[ChatMessage]) -> tuple[int, int, str | None]:
    """返回 `(start, end, reason)`。`reason` 非 None 表示没有可用区间。"""
    total = len(messages)
    if total <= SNIP_KEEP_RECENT_MESSAGES + SNIP_MIN_MESSAGES_TO_REMOVE:
        return 0, 0, "too_few_messages"

    keep_recent_start = max(0, total - SNIP_KEEP_RECENT_MESSAGES)

    last_user_index = -1
    for index in range(total - 1, -1, -1):
        if messages[index].get("role") == "user":
            last_user_index = index
            break

    end = min(keep_recent_start, last_user_index if last_user_index >= 0 else total)
    if end <= 0:
        return 0, 0, "no_middle_range"

    # 起点推到最后一个边界消息之后，保证 system 与既有摘要不被删。
    start = 0
    for index in range(end):
        if is_boundary_message(messages[index]):
            start = index + 1

    if end - start < SNIP_MIN_MESSAGES_TO_REMOVE:
        return 0, 0, "candidate_range_too_small"

    return start, end, None

# ---------------------------------------------------------------------------
# 保护标记
# ---------------------------------------------------------------------------

def mark_protected_groups(
    groups: list[MessageGroup], candidate_start: int, candidate_end: int
) -> None:
    # 第一阶段：区间外的组与含边界消息的组
    for group in groups:
        if group.start < candidate_start or group.end > candidate_end:
            _add_reason(group, "outside_candidate_range")
            continue
        if any(is_boundary_message(m) for m in group.messages):
            _add_reason(group, "boundary_message")

    # 第二阶段：编辑与错误，连带前后各一组
    for index, group in enumerate(groups):
        if _group_has_protected_tool(group):
            _protect_nearby(groups, index, "near_file_edit")
        if _group_has_important_error(group):
            _protect_nearby(groups, index, "near_important_error")

def _add_reason(group: MessageGroup, reason: str) -> None:
    group.protected = True
    if reason not in group.reasons:
        group.reasons.append(reason)

def _protect_nearby(groups: list[MessageGroup], index: int, reason: str) -> None:
    for offset in (-1, 0, 1):
        target = index + offset
        if 0 <= target < len(groups):
            _add_reason(groups[target], reason)

# ---------------------------------------------------------------------------
# 安全段
# ---------------------------------------------------------------------------

def find_safe_runs(groups: list[MessageGroup]) -> list[SafeRun]:
    """把连续未受保护的组合并成安全段。"""
    runs: list[SafeRun] = []
    pending: list[MessageGroup] = []

    def flush() -> None:
        if not pending:
            return
        first, last = pending[0], pending[-1]
        runs.append(
            SafeRun(
                groups=list(pending),
                start=first.start,
                end=last.end,
                messages_count=last.end - first.start,
                tokens=sum(group.tokens for group in pending),
            )
        )
        pending.clear()

    for group in groups:
        if group.protected:
            flush()
            continue
        pending.append(group)
    flush()

    return runs

def _compare_runs(run: SafeRun) -> tuple[int, int, int]:
    """排序键：token 降序 → 消息数降序 → start 升序。"""
    return (-run.tokens, -run.messages_count, run.start)

def select_deletion_from_run(run: SafeRun, desired_tokens_to_free: int) -> _Deletion:
    """从安全段起点向后累积，直到释放够 token 且条数达标。"""
    tokens = 0
    messages_count = 0
    end_group_index = -1

    for index, group in enumerate(run.groups):
        tokens += group.tokens
        messages_count = group.end - run.start
        end_group_index = index
        if tokens >= desired_tokens_to_free and messages_count >= SNIP_MIN_MESSAGES_TO_REMOVE:
            break

    end = run.groups[max(0, end_group_index)].end
    return _Deletion(
        start=run.start, end=end, tokens=tokens, messages_count=messages_count
    )

# ---------------------------------------------------------------------------
# 边界消息
# ---------------------------------------------------------------------------

def build_snip_boundary_content(removed_count: int, tokens_freed: int) -> str:
    return "\n".join(
        [
            "[Snipped earlier conversation segment]",
            "",
            "A middle portion of the earlier conversation was removed to preserve "
            "context space.",
            "",
            "Removed range:",
            f"- messages: {removed_count}",
            f"- approximate tokens freed: {max(0, js_round(tokens_freed))}",
            "",
            "The recent conversation and active task context are preserved.",
        ]
    )

def _build_boundary_message(
    removed_messages: list[ChatMessage], removed_ids: list[str], tokens_freed: int
) -> ChatMessage:
    timestamp = int(time.time() * 1000)
    first_id = removed_ids[0] if removed_ids else "none"
    return {
        "id": f"snip-{timestamp}-{first_id}",
        "role": ROLE_SNIP_BOUNDARY,
        "content": build_snip_boundary_content(len(removed_messages), tokens_freed),
        "removedMessageIds": list(removed_ids),
        "removedCount": len(removed_messages),
        "tokensFreed": tokens_freed,
        "timestamp": timestamp,
    }

def _no_snip(
    messages: list[ChatMessage], tokens_before: int, reason: str
) -> SnipResult:
    """失败结果：**绝不改动转录**，返回原列表引用。"""
    return SnipResult(
        messages=messages,
        did_snip=False,
        tokens_before=tokens_before,
        tokens_after=tokens_before,
        tokens_freed=0,
        removed_message_ids=[],
        reason=reason,
    )

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def snip_compact_conversation(
    messages: list[ChatMessage],
    context_stats: ContextStats,
    model_context_window: int | None = None,
) -> SnipResult:
    """尝试剪掉一段中段历史。任何失败都返回原列表，不抛异常。"""
    trigger_tokens = context_stats.total_tokens
    tokens_before = estimate_messages_tokens(messages)

    effective_input = (
        context_stats.effective_input
        if context_stats.effective_input > 0
        else (model_context_window or 0)
    )
    # 注意：这里的利用率**不截顶**（与 ContextStats.utilization 不同）。
    utilization = (
        trigger_tokens / effective_input
        if effective_input > 0
        else context_stats.utilization
    )
    if utilization < SNIP_COMPACT_THRESHOLD:
        return _no_snip(messages, tokens_before, "below_threshold")

    start, end, range_reason = find_candidate_range(messages)
    if range_reason is not None:
        return _no_snip(messages, tokens_before, range_reason)

    groups = build_message_groups(messages)
    mark_protected_groups(groups, start, end)

    safe_runs = [
        run
        for run in find_safe_runs(groups)
        if run.messages_count >= SNIP_MIN_MESSAGES_TO_REMOVE
        and run.tokens >= SNIP_MIN_TOKENS_TO_FREE
    ]
    if not safe_runs:
        return _no_snip(messages, tokens_before, "no_safe_interval")

    safe_runs.sort(key=_compare_runs)
    best_run = safe_runs[0]

    desired_tokens_to_free = max(
        SNIP_MIN_TOKENS_TO_FREE,
        trigger_tokens - int(effective_input * SNIP_TARGET_USAGE),
    )
    deletion = select_deletion_from_run(best_run, desired_tokens_to_free)

    if deletion.messages_count < SNIP_MIN_MESSAGES_TO_REMOVE:
        return _no_snip(messages, tokens_before, "below_min_messages")

    removed_messages = messages[deletion.start : deletion.end]
    removed_ids = [
        str(m.get("id") or f"message-{deletion.start + offset}")
        for offset, m in enumerate(removed_messages)
    ]

    draft_boundary = _build_boundary_message(
        removed_messages, removed_ids, deletion.tokens
    )
    boundary_tokens = estimate_messages_tokens([draft_boundary])

    estimated_tokens_freed = max(0, deletion.tokens - boundary_tokens)
    if estimated_tokens_freed < SNIP_MIN_TOKENS_TO_FREE:
        return _no_snip(messages, tokens_before, "below_min_tokens")

    final_boundary: ChatMessage = {
        **draft_boundary,
        "content": build_snip_boundary_content(
            len(removed_messages), estimated_tokens_freed
        ),
        "tokensFreed": estimated_tokens_freed,
    }

    spliced = (
        messages[: deletion.start]
        + [final_boundary]
        + messages[deletion.end :]
    )
    # 压缩改动了历史，被保留消息上的旧 usage 不再可信。
    after = [mark_provider_usage_stale(m, STALE_REASON) for m in spliced]

    # 此刻所有 usage 都已 stale，因此这一步必然走 estimate_only 分支。
    tokens_after = token_count_with_estimation(after).total_tokens
    if tokens_after >= tokens_before:
        return _no_snip(messages, tokens_before, "no_token_reduction")

    return SnipResult(
        messages=after,
        did_snip=True,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_freed=max(0, tokens_before - tokens_after),
        removed_message_ids=removed_ids,
        boundary_message=after[deletion.start],
        reason="snipped_safe_middle_interval",
    )
