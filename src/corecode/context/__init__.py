"""上下文工程：四级压缩 + token 计量 + 大结果落盘。

按「从轻到重」排列，触发阈值依次抬高：

| 机制 | 触发 | 是否改写真实转录 | 调用模型 |
|---|---|---|---|
| `microcompact` | ≥50% | 是（只换正文） | 否 |
| `snip_compact_conversation` | ≥70% | 是（删中段 + 插边界） | 否 |
| `apply_context_collapse_if_needed` | ≥75% | **否**（只改模型可见投影） | 是 |
| `auto_compact` | ≥85% | 是（摘要替换前缀） | 是 |

另外 `tool_result_storage` 负责把超大工具输出落盘，上下文只留预览与路径。

`ContextPipeline` 是 agent loop 唯一需要知道的接缝：它返回
`(真实消息, 模型可见投影)` 两个数组——这个二元返回正是 collapse
「只改投影」能干净落地的地方。
"""

from .collapse import (
    CollapseSpan,
    ContextCollapseResult,
    ContextCollapseState,
    apply_context_collapse_if_needed,
    create_context_collapse_state,
    project_collapsed_view,
)
from .compact import (
    CompressionResult,
    auto_compact,
    build_compact_summary_prompt,
    compact_conversation,
    get_auto_compact_state,
    parse_summary_from_response,
    reset_auto_compact_state,
    should_auto_compact,
)
from .constants import (
    AUTOCOMPACT_UTILIZATION,
    CLEAR_MARKER,
    COMPACTABLE_TOOLS,
    CONTEXT_COLLAPSE_UTILIZATION,
    MICROCOMPACT_UTILIZATION,
    SNIP_COMPACT_THRESHOLD,
)
from .microcompact import microcompact
from .model_context import (
    UNKNOWN_MODEL_CONTEXT,
    ModelContextWindow,
    get_model_context_window,
)
from .pipeline import ContextPipeline
from .snip import SnipResult, snip_compact_conversation
from .tokens import (
    ContextStats,
    TokenAccountingResult,
    compute_context_stats,
    estimate_message_tokens,
    estimate_messages_tokens,
    js_round,
    mark_provider_usage_stale,
    message_provider_usage,
    token_count_with_estimation,
    utf16_length,
)
from .tool_result_storage import (
    ContentReplacementState,
    PendingToolResult,
    apply_tool_result_budget,
    create_content_replacement_state,
    replace_large_tool_result,
)

__all__ = [
    "AUTOCOMPACT_UTILIZATION",
    "CLEAR_MARKER",
    "COMPACTABLE_TOOLS",
    "CONTEXT_COLLAPSE_UTILIZATION",
    "CollapseSpan",
    "CompressionResult",
    "ContentReplacementState",
    "ContextCollapseResult",
    "ContextCollapseState",
    "ContextPipeline",
    "ContextStats",
    "MICROCOMPACT_UTILIZATION",
    "ModelContextWindow",
    "PendingToolResult",
    "SNIP_COMPACT_THRESHOLD",
    "SnipResult",
    "TokenAccountingResult",
    "UNKNOWN_MODEL_CONTEXT",
    "apply_context_collapse_if_needed",
    "apply_tool_result_budget",
    "auto_compact",
    "build_compact_summary_prompt",
    "compact_conversation",
    "compute_context_stats",
    "create_content_replacement_state",
    "create_context_collapse_state",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "get_auto_compact_state",
    "get_model_context_window",
    "js_round",
    "mark_provider_usage_stale",
    "message_provider_usage",
    "microcompact",
    "parse_summary_from_response",
    "project_collapsed_view",
    "replace_large_tool_result",
    "reset_auto_compact_state",
    "should_auto_compact",
    "snip_compact_conversation",
    "token_count_with_estimation",
    "utf16_length",
]
