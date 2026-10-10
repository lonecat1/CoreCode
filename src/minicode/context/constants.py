"""上下文工程的阈值常量。

全部阈值集中在这里，是**唯一事实来源**。原 TypeScript 实现把这些值散落在
`constants.ts` 与 `token-estimator.ts` 两处硬编码重复，本实现收敛为一处。

有一处刻意的差异：原实现定义了 `BLOCKED_UTILIZATION` / `MIN_KEEP_TOKENS` /
`PTL_MAX_RETRIES` / `SUMMARY_MAX_OUTPUT_TOKENS` 四个常量但**从未引用**。
本实现不复刻它们——保留零引用的常量只会误导后来者。

调整这些值会直接影响长会话的行为，改动前请先看 docs/spec-context.md 的护栏说明。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 触发阈值（对 effectiveInput 的利用率）
# ---------------------------------------------------------------------------

#: microcompact：清空较旧的只读工具输出正文。
MICROCOMPACT_UTILIZATION = 0.50

#: snip compact：确定性删除中段历史。
SNIP_COMPACT_THRESHOLD = 0.70
#: snip 的目标利用率，用于反推「需要释放多少 token」。
SNIP_TARGET_USAGE = 0.60
#: snip 单次最少删除的消息条数。
SNIP_MIN_MESSAGES_TO_REMOVE = 6
#: snip 的尾部保留窗口（最近 N 条永不删除）。
SNIP_KEEP_RECENT_MESSAGES = 12
#: snip 的最小净释放 token；达不到就整体放弃。
SNIP_MIN_TOKENS_TO_FREE = 2_000

#: context collapse：把历史区间投影为摘要（不改真实转录）。
CONTEXT_COLLAPSE_UTILIZATION = 0.75
#: collapse 的目标利用率。
CONTEXT_COLLAPSE_TARGET_USAGE = 0.65
#: collapse 的尾部保留窗口。
CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES = 12
#: collapse 单个 span 的最小净节省；达不到记一次失败。
CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE = 2_000
#: 单次折叠最多处理几个 span（防止一次请求做太多摘要调用）。
CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS = 2
#: collapse 连续失败多少次后自我禁用。
CONTEXT_COLLAPSE_MAX_FAILURES = 3

#: auto-compact：调用模型生成全量摘要替换前缀。
AUTOCOMPACT_UTILIZATION = 0.85
#: auto-compact 连续失败多少次后自我禁用（进程内）。
MAX_AUTOCOMPACT_FAILURES = 3
#: 窗口小于此值时跳过 auto-compact：摘要开销不划算。
MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT = 20_000

# ---------------------------------------------------------------------------
# warningLevel 阈值
#
# 原实现在 token-estimator.ts 里把这四个数字硬编码了一遍，与上面的常量重复
# 但不共享。这里让它们指向同一组常量。
# ---------------------------------------------------------------------------

#: >= 0.95：上下文已经阻塞，必须压缩。
BLOCKED_UTILIZATION = 0.95
#: >= 0.85：危急，触发 auto-compact。
CRITICAL_UTILIZATION = AUTOCOMPACT_UTILIZATION
#: >= 0.50：警告。
WARNING_UTILIZATION = MICROCOMPACT_UTILIZATION

# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------

#: microcompact 保留最近几条可压缩工具结果。
KEEP_RECENT_TOOL_RESULTS = 3
#: auto-compact 至少保留几条非 system 消息。
MIN_KEEP_MESSAGES = 6
#: auto-compact 尾部保留区的 token 下限。
MIN_KEEP_TOKENS = 10_000
#: auto-compact 尾部保留区的 token 上限。
MAX_KEEP_TOKENS = 40_000

#: microcompact 的替换文本。精确匹配，不要改动（模型见过这个标记）。
CLEAR_MARKER = "[Output cleared for context space]"

#: microcompact 的白名单：只有只读且输出易膨胀的工具才值得清空。
#: 编辑类工具刻意不在其中——它们的输出是「改了什么」的证据，必须保留。
COMPACTABLE_TOOLS = frozenset(
    {"read_file", "run_command", "search_files", "list_files", "web_fetch"}
)

#: snip 的文件编辑工具保护集。名字含 patch/write/edit/modify 的也会被保护
#: （见 snip.py 的 is_protected_tool_name，子串匹配是有意为之）。
PROTECTED_TOOL_NAMES = frozenset(
    {"edit_file", "modify_file", "patch_file", "write_file", "apply_patch"}
)

#: 出现这些子串（小写）的消息被视为「重要错误」，其附近区段不可删除。
ERROR_MARKERS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "permission denied",
)

#: 边界消息角色：snip 的候选区间起点由最后一个边界消息决定。
BOUNDARY_ROLES = frozenset({"system", "context_summary", "snip_boundary"})
