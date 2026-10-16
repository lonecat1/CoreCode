"""auto-compact —— 最重的一级：调用模型生成全量摘要。

前三级压缩都是「不花模型调用」的省 token 手段。当利用率仍爬到 85% 时，
说明结构性的裁剪已经不够了，只能把最老的一段历史**真的换成一段摘要**。

这是唯一需要额外模型调用、也最容易出错的压缩：
  - 摘要请求本身可能失败（网络、超限）→ 有连续失败熔断；
  - 摘要必须能被解析出来，否则整次压缩作废；
  - 保留区必须**对齐到 API round 边界**，否则会把
    `tool_use` 与配对的 `tool_result` 切开，下一次请求直接被 API 拒绝。

最后一条是这里最关键的护栏。所谓 API round 是「一轮完整的模型往返」：
`assistant_thinking? → assistant_tool_call* → tool_result*`。切在轮次中间，
就会产生一个只有 `tool_result` 却没有对应 `tool_use` 的残缺请求。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..types import (
    ROLE_ASSISTANT,
    ROLE_ASSISTANT_PROGRESS,
    ROLE_ASSISTANT_THINKING,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_CONTEXT_SUMMARY,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
    ChatMessage,
    ModelAdapter,
)
from .constants import (
    AUTOCOMPACT_UTILIZATION,
    MAX_AUTOCOMPACT_FAILURES,
    MAX_KEEP_TOKENS,
    MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT,
    MIN_KEEP_MESSAGES,
)
from .tokens import (
    compute_context_stats,
    estimate_message_tokens,
    estimate_messages_tokens,
    mark_provider_usage_stale,
)

STALE_REASON = "conversation was compacted after this provider usage was recorded"

#: 生成摘要时，单条 tool_result 送入提示词的字符上限。
#: 摘要不需要看到完整输出，截断能显著降低这次额外调用的成本。
SUMMARY_TOOL_RESULT_LIMIT = 500

# 这里曾有一个 `SUMMARY_MAX_OUTPUT_TOKENS = 4_096`。
#
# 它**从未被使用**——摘要请求的 max_tokens 由 `resolve_max_output_tokens`
# 统一决定（见 config.py），并不读这个常量。原版 `compact/constants.ts:29`
# 也定义了同名常量且同样零引用，本实现把它连着一起继承了。
#
# 核对常量表时删除。零引用常量的代价不是几行代码，而是**后来者会以为它生效**：
# 改这里的数字不会改变任何行为，而改动者会以为改了。


# ---------------------------------------------------------------------------
# 进程内熔断状态
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactState:
    consecutive_failures: int = 0
    disabled: bool = False


_state = AutoCompactState()


def get_auto_compact_state() -> AutoCompactState:
    return _state


def reset_auto_compact_state() -> None:
    """重新武装。手动压缩成功后调用。"""
    _state.consecutive_failures = 0
    _state.disabled = False


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompressionResult:
    messages: list[ChatMessage]
    summary: ChatMessage
    removed_count: int
    tokens_before: int
    tokens_after: int


# ---------------------------------------------------------------------------
# 摘要提示词
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """请把下面这段编码助手与用户的对话历史压缩成一份结构化摘要。这份摘要将取代原始对话，因此必须保留继续工作所需的一切信息。

严格按以下格式输出，不要添加任何额外说明：

<summary>
## Primary Request
（用户最初要做什么）

## Key Decisions
（已经做出的技术决定与理由）

## Files Modified
（改动过的文件及其变更要点）

## Errors Encountered
（遇到过的错误与解决方式）

## Current State
（当前进展到哪一步）

## Pending Tasks
（还没做的事）
</summary>

对话历史：
"""


def build_compact_summary_prompt(conversation_text: str) -> str:
    return SUMMARY_PROMPT + conversation_text


def parse_summary_from_response(response: str) -> str | None:
    """从模型响应里抽出 `<summary>` 内容；抽不到返回 None。"""
    start = response.find("<summary>")
    end = response.find("</summary>")
    if start == -1 or end == -1 or end <= start:
        return None
    body = response[start + len("<summary>") : end].strip()
    return body or None


# ---------------------------------------------------------------------------
# 保留区边界
# ---------------------------------------------------------------------------


def group_messages_by_api_round(messages: list[ChatMessage]) -> list[list[int]]:
    """把消息下标按「一轮完整的模型往返」分组。

    一轮 = 可选的 thinking → 若干 tool_call → 若干 tool_result，
    或单条普通消息。切分边界永远落在这里，才不会切断配对。
    """
    rounds: list[list[int]] = []
    index = 0
    total = len(messages)

    while index < total:
        current: list[int] = []
        role = messages[index].get("role")

        if role == ROLE_ASSISTANT_THINKING:
            current.append(index)
            index += 1
            # thinking 之后紧跟的 tool_call 属于同一轮
            while index < total and messages[index].get("role") == ROLE_ASSISTANT_TOOL_CALL:
                current.append(index)
                index += 1
            while index < total and messages[index].get("role") == ROLE_TOOL_RESULT:
                current.append(index)
                index += 1
            rounds.append(current)
            continue

        if role == ROLE_ASSISTANT_TOOL_CALL:
            while index < total and messages[index].get("role") == ROLE_ASSISTANT_TOOL_CALL:
                current.append(index)
                index += 1
            while index < total and messages[index].get("role") == ROLE_TOOL_RESULT:
                current.append(index)
                index += 1
            rounds.append(current)
            continue

        current.append(index)
        index += 1
        rounds.append(current)

    return rounds


def find_retention_boundary(messages: list[ChatMessage]) -> int:
    """从尾部向前累加 token，算出保留区的起点（已对齐 API round 边界）。

    注意「至少留一轮给压缩」这条约束：如果整个对话都在 `MAX_KEEP_TOKENS` 以内，
    尾部累加会一路走到下标 0，返回 0 —— 那样就永远没有可压缩的内容，
    auto-compact 会静默失效。所以这里强制边界不能早于第 1 轮，
    保证始终存在一段前缀可供压缩。
    """
    rounds = group_messages_by_api_round(messages)
    if not rounds:
        return 0

    tokens = 0
    keep_from_round = len(rounds)
    for position in range(len(rounds) - 1, -1, -1):
        round_tokens = sum(estimate_message_tokens(messages[i]) for i in rounds[position])
        # 到达上限后停止
        if tokens + round_tokens > MAX_KEEP_TOKENS and tokens > 0:
            break
        tokens += round_tokens
        keep_from_round = position

    # 至少保留一轮可压缩前缀。
    keep_from_round = max(1, keep_from_round)

    boundary = rounds[keep_from_round][0] if keep_from_round < len(rounds) else len(messages)

    # 条数下限：不足 MIN_KEEP_MESSAGES 条就往前扩。
    non_system_count = sum(
        1 for m in messages[boundary:] if m.get("role") != ROLE_SYSTEM
    )
    while non_system_count < MIN_KEEP_MESSAGES and keep_from_round > 1:
        keep_from_round -= 1
        boundary = rounds[keep_from_round][0]
        non_system_count = sum(
            1 for m in messages[boundary:] if m.get("role") != ROLE_SYSTEM
        )

    return boundary


# ---------------------------------------------------------------------------
# 摘要输入渲染
# ---------------------------------------------------------------------------


def _messages_to_text(messages: list[ChatMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == ROLE_SYSTEM:
            continue
        content = str(message.get("content", ""))
        if role == ROLE_TOOL_RESULT and len(content) > SUMMARY_TOOL_RESULT_LIMIT:
            content = content[:SUMMARY_TOOL_RESULT_LIMIT] + "\n…（已截断）"
        if role == ROLE_ASSISTANT_TOOL_CALL:
            lines.append(f"[tool_call] {message.get('toolName')}")
            continue
        if content.strip():
            lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def _summary_request_messages(conversation_text: str) -> list[ChatMessage]:
    return [
        {
            "role": ROLE_SYSTEM,
            "content": "你是一个对话摘要器。只输出 <summary>…</summary> 之间的内容。",
        },
        {"role": "user", "content": build_compact_summary_prompt(conversation_text)},
    ]


# ---------------------------------------------------------------------------
# 核心
# ---------------------------------------------------------------------------


def compact_conversation(
    messages: list[ChatMessage], model_adapter: ModelAdapter
) -> CompressionResult | None:
    """把 `messages[1:boundary)` 换成一条 `context_summary`。

    注意这里**不是** `messages[1:boundary]` 那样盲切下标 0：原实现假定下标 0
    是 system 消息，若序列不以 system 开头，第一条非 system 消息会被静默吞掉。
    本实现改为「压缩区间跳过所有 system 消息」，语义更清晰且不丢内容。
    """
    if len(messages) <= 2:
        return None

    system_messages = [m for m in messages if m.get("role") == ROLE_SYSTEM]
    non_system = [m for m in messages if m.get("role") != ROLE_SYSTEM]
    if len(non_system) <= MIN_KEEP_MESSAGES:
        return None

    boundary = find_retention_boundary(messages)

    # 压缩区间 = 首个非 system 消息 → 保留区起点，中间的 system 消息保持原位不动。
    to_compress: list[ChatMessage] = []
    for index, message in enumerate(messages):
        if index >= boundary:
            break
        if message.get("role") == ROLE_SYSTEM:
            continue
        to_compress.append(message)

    if not to_compress:
        return None

    tokens_before = estimate_messages_tokens(messages)
    response = model_adapter.next(
        _summary_request_messages(_messages_to_text(to_compress))
    )
    summary_text = parse_summary_from_response(getattr(response, "content", "") or "")
    if summary_text is None:
        return None

    summary: ChatMessage = {
        "role": ROLE_CONTEXT_SUMMARY,
        "content": summary_text,
        "compressedCount": len(to_compress),
        "timestamp": _now_ms(),
    }

    kept = [
        mark_provider_usage_stale(m, STALE_REASON) for m in messages[boundary:]
    ]
    rebuilt = [*system_messages, summary, *kept]

    return CompressionResult(
        messages=rebuilt,
        summary=summary,
        removed_count=len(to_compress),
        tokens_before=tokens_before,
        tokens_after=estimate_messages_tokens(rebuilt),
    )


def should_auto_compact(messages: list[ChatMessage], model: str) -> bool:
    stats = compute_context_stats(messages, model)
    return stats.utilization >= AUTOCOMPACT_UTILIZATION


def auto_compact(
    messages: list[ChatMessage],
    model: str,
    model_adapter: ModelAdapter,
) -> CompressionResult | None:
    """带三重门控与熔断的自动压缩。

    门控：① 未熔断；② 利用率 ≥ 85%；③ 有效输入 ≥ 20000（小窗口下摘要开销不划算）。
    """
    if _state.disabled:
        return None

    stats = compute_context_stats(messages, model)
    if stats.utilization < AUTOCOMPACT_UTILIZATION:
        return None
    if stats.effective_input < MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT:
        return None

    try:
        result = compact_conversation(messages, model_adapter)
    except Exception:  # noqa: BLE001 - 摘要调用失败不应拖垮会话
        result = None

    if result is None:
        _state.consecutive_failures += 1
        if _state.consecutive_failures >= MAX_AUTOCOMPACT_FAILURES:
            _state.disabled = True
        return None

    _state.consecutive_failures = 0
    return result


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
