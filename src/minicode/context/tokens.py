"""Token 计量。

核心设计：**provider 上报的 usage 是计量真值**，本地字符估算只用来补
「最后一次 provider usage 之后的尾巴」。原因很实际——本地估算误差能到 30%，
而 provider 给的数字是准的；但模型还没回过话的那部分（比如刚插入的
tool_result）没有任何 provider 数字可用，只能估。

由此得到边界语义：**找到了 usage 边界消息，边界及其之前完全信任 provider 的
totalTokens（不重估、不加成），只有其后的尾部才本地估算。**

一旦压缩改动了历史（snip / auto-compact / collapse），被保留消息上的旧 usage
就不再可信，必须用 `mark_provider_usage_stale()` 标记失效——否则计量会
基于「压缩前的世界」给出错误数字。

两个 JS/Python 语义差异被显式处理，否则边界处行为会漂移：
  - `Math.round(50.5) == 51` 而 Python `round(50.5) == 50`（银行家舍入）
  - JS `String.length` 是 UTF-16 码元数，Python `len(str)` 是码点数
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..types import (
    ROLE_ASSISTANT,
    ROLE_ASSISTANT_PROGRESS,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_ASSISTANT_THINKING,
    ROLE_CONTEXT_SUMMARY,
    ROLE_SNIP_BOUNDARY,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ChatMessage,
)
from .constants import (
    BLOCKED_UTILIZATION,
    CRITICAL_UTILIZATION,
    WARNING_UTILIZATION,
)
from .model_context import get_model_context_window

# ---------------------------------------------------------------------------
# 数值语义对齐
# ---------------------------------------------------------------------------


def js_round(value: float) -> int:
    """对齐 JavaScript 的 `Math.round`：.5 一律向上（向 +∞），而非银行家舍入。

    Python 的 `round(0.5) == 0`、`round(2.5) == 2`，与 JS 相反。
    """
    return math.floor(value + 0.5)


def utf16_length(text: str) -> int:
    """字符串的 UTF-16 码元数，对齐 JavaScript 的 `String.length`。

    非 BMP 字符（emoji 等）在 JS 里算 2，Python `len` 只算 1。
    这个差异会影响 token 估算，所以必须显式对齐。
    """
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# 本地估算
# ---------------------------------------------------------------------------

#: 各角色的字符/token 比率。
#: 数值越高表示该角色的内容「越紧凑」（代码与结构化文本比自然语言密集）。
CHARS_PER_TOKEN: dict[str, float] = {
    ROLE_SYSTEM: 3.5,
    ROLE_USER: 3.0,
    ROLE_ASSISTANT_THINKING: 3.0,
    ROLE_ASSISTANT: 3.5,
    ROLE_ASSISTANT_PROGRESS: 3.5,
    ROLE_ASSISTANT_TOOL_CALL: 2.5,
    ROLE_TOOL_RESULT: 2.0,
    ROLE_CONTEXT_SUMMARY: 3.5,
    ROLE_SNIP_BOUNDARY: 3.5,
}

#: 未列出角色的兜底比率。
DEFAULT_CHARS_PER_TOKEN = 3.0

#: 会承载 provider usage 的三个 assistant 侧角色。
USAGE_BEARING_ROLES = frozenset(
    {ROLE_ASSISTANT, ROLE_ASSISTANT_PROGRESS, ROLE_ASSISTANT_TOOL_CALL}
)


def message_content_length(message: ChatMessage) -> int:
    """按角色决定「用来估算的字符数」。"""
    role = message.get("role")

    if role == ROLE_ASSISTANT_THINKING:
        try:
            return utf16_length(json.dumps(message.get("blocks") or [], ensure_ascii=False))
        except (TypeError, ValueError):
            return 0

    if role == ROLE_ASSISTANT_TOOL_CALL:
        try:
            return utf16_length(
                json.dumps(message.get("input"), ensure_ascii=False, default=str)
            )
        except (TypeError, ValueError):
            return 0

    if role in (
        ROLE_SYSTEM,
        ROLE_USER,
        ROLE_ASSISTANT,
        ROLE_ASSISTANT_PROGRESS,
        ROLE_TOOL_RESULT,
        ROLE_CONTEXT_SUMMARY,
        ROLE_SNIP_BOUNDARY,
    ):
        return utf16_length(str(message.get("content", "")))

    return 0


def estimate_message_tokens(message: ChatMessage) -> int:
    ratio = CHARS_PER_TOKEN.get(str(message.get("role")), DEFAULT_CHARS_PER_TOKEN)
    return math.ceil(message_content_length(message) / ratio)


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


# ---------------------------------------------------------------------------
# provider usage
# ---------------------------------------------------------------------------


def message_provider_usage(message: ChatMessage) -> dict[str, Any] | None:
    """取出**可用**的 provider usage；被标 stale 的不算可用。"""
    if message.get("role") not in USAGE_BEARING_ROLES:
        return None
    if message.get("usageStale"):
        return None
    usage = message.get("providerUsage")
    return usage if isinstance(usage, dict) else None


def mark_provider_usage_stale(message: ChatMessage, reason: str) -> ChatMessage:
    """把一条消息的 provider usage 标为失效。

    只对「带 providerUsage 的 assistant 侧消息」生效；其余消息**原样返回同一对象**
    （避免无谓的对象复制，也让调用方能用 `is` 判断是否变过）。
    """
    if message.get("role") not in USAGE_BEARING_ROLES:
        return message
    if not isinstance(message.get("providerUsage"), dict):
        return message
    return {**message, "usageStale": True, "usageStaleReason": reason}


def _first_stale_reason(messages: list[ChatMessage]) -> str | None:
    """正序找第一条被标 stale 的 usage，返回其原因。"""
    for message in messages:
        if message.get("role") in USAGE_BEARING_ROLES and message.get("usageStale"):
            reason = message.get("usageStaleReason")
            return str(reason) if reason else "provider usage was marked stale"
    return None


# ---------------------------------------------------------------------------
# 合并结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenAccountingResult:
    total_tokens: int
    provider_usage_tokens: int
    estimated_tokens: int
    source: str  # provider_usage | provider_usage_plus_estimate | estimate_only
    is_exact: bool
    usage_boundary: dict[str, Any] | None = None
    stale: bool = False
    reason: str | None = None


def token_count_with_estimation(messages: list[ChatMessage]) -> TokenAccountingResult:
    """合并 provider usage 与本地估算。"""
    # 从尾部向前找最近的可用 usage 边界。
    for index in range(len(messages) - 1, -1, -1):
        usage = message_provider_usage(messages[index])
        if usage is None:
            continue

        tail = messages[index + 1 :]
        estimated = estimate_messages_tokens(tail)
        provider_total = int(usage.get("totalTokens") or 0)
        boundary_id = None
        if messages[index].get("role") == ROLE_ASSISTANT_TOOL_CALL:
            boundary_id = messages[index].get("toolUseId")

        return TokenAccountingResult(
            total_tokens=provider_total + estimated,
            provider_usage_tokens=provider_total,
            estimated_tokens=estimated,
            source="provider_usage_plus_estimate" if estimated > 0 else "provider_usage",
            is_exact=estimated == 0,
            usage_boundary={"messageIndex": index, "messageId": boundary_id},
        )

    # 没有可用 usage：全量本地估算，并说明为什么没有。
    estimated = estimate_messages_tokens(messages)
    stale_reason = _first_stale_reason(messages)
    return TokenAccountingResult(
        total_tokens=estimated,
        provider_usage_tokens=0,
        estimated_tokens=estimated,
        source="estimate_only",
        is_exact=False,
        stale=bool(stale_reason),
        reason=stale_reason or "no provider usage available",
    )


# ---------------------------------------------------------------------------
# 上下文统计
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextStats:
    estimated_tokens: int
    total_tokens: int
    provider_usage_tokens: int
    context_window: int
    effective_input: int
    utilization: float
    warning_level: str  # normal | warning | critical | blocked
    accounting: TokenAccountingResult = field(repr=False)


def compute_warning_level(utilization: float) -> str:
    if utilization >= BLOCKED_UTILIZATION:
        return "blocked"
    if utilization >= CRITICAL_UTILIZATION:
        return "critical"
    if utilization >= WARNING_UTILIZATION:
        return "warning"
    return "normal"


def compute_context_stats(messages: list[ChatMessage], model: str) -> ContextStats:
    window = get_model_context_window(model)
    accounting = token_count_with_estimation(messages)

    if window.effective_input > 0:
        # 利用率截顶到 1：超过窗口就报 100%，不再继续增长。
        utilization = min(1.0, accounting.total_tokens / window.effective_input)
    else:
        utilization = 0.0

    return ContextStats(
        estimated_tokens=accounting.estimated_tokens,
        total_tokens=accounting.total_tokens,
        provider_usage_tokens=accounting.provider_usage_tokens,
        context_window=window.context_window,
        effective_input=window.effective_input,
        utilization=utilization,
        warning_level=compute_warning_level(utilization),
        accounting=accounting,
    )
