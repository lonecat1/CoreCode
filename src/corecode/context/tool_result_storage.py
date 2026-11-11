"""大工具结果的落盘与预算。

单条工具输出可能极大（读一个巨型文件、跑一条输出几万行的命令）。把它们
整段留在上下文里会立刻把预算耗尽，所以这里做两件事：

1. **单条超限落盘**：超过 50,000 字符就把全文写进
   `~/.corecode/tool-results/<session>/<toolUseId>.txt`，上下文里只留一段
   预览加文件路径。模型仍然知道全文在哪，需要时可以再读。
2. **单批预算**：同一批工具结果的可见总量超过 200,000 字符时，按**体积从大到小**
   继续落盘，直到降到预算内。先处理最大的，能在最少的落盘次数里腾出最多空间。

`ContentReplacementState` 是这套机制的记忆：它保证同一个 `toolUseId` 永远得到
**字节相同**的替换文本（幂等重放，不会重复写盘），并记住哪些结果已经计入过
可见体积，避免跨批次重复扣费。它是进程内状态，不落盘。

两道闸门是**串联**的：单条阈值先过，然后整批再过一次预算。
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import tool_results_dir
from .tokens import js_round

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

#: 单条结果落盘阈值（字符）。恰好等于该值**不**触发。
DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
#: 单个工具批次的可见总量预算（字符）。
MAX_TOOL_RESULTS_PER_BATCH_CHARS = 200_000
#: 预览保留的字符数。
PREVIEW_SIZE_CHARS = 2_000

#: 预览在换行处切分的位置门槛：只有最后一个换行**严格大于**该值时才在换行处切。
_PREVIEW_NEWLINE_THRESHOLD = PREVIEW_SIZE_CHARS * 0.5

_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9._-]")

_SESSION_DIR = _SAFE_SEGMENT.sub("_", str(uuid.uuid4())) or str(uuid.uuid4())

def sanitize_path_segment(value: str) -> str:
    """把任意标识符变成安全的路径片段。"""
    cleaned = _SAFE_SEGMENT.sub("_", value or "")
    return cleaned or str(uuid.uuid4())

def normalize_tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)

def generate_preview(content: str) -> tuple[str, bool]:
    """返回 `(预览, 是否还有更多)`。

    换行边界特例：只有最后一个换行的位置**严格大于** 1000 时才在换行处切，
    否则硬切 2000。这样避免为了一个很靠前的换行把预览砍得太短。
    """
    if len(content) <= PREVIEW_SIZE_CHARS:
        return content, False

    head = content[:PREVIEW_SIZE_CHARS]
    last_newline = head.rfind("\n")
    cut_point = last_newline if last_newline > _PREVIEW_NEWLINE_THRESHOLD else PREVIEW_SIZE_CHARS
    return content[:cut_point], True

def format_chars(chars: int) -> str:
    if chars >= 1_000_000:
        return f"{chars / 1_000_000:.1f}M chars"
    if chars >= 1_000:
        return f"{js_round(chars / 1_000)}K chars"
    return f"{chars} chars"

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

@dataclass
class ContentReplacementState:
    """进程内的替换记忆。不落盘。"""

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)

def create_content_replacement_state() -> ContentReplacementState:
    return ContentReplacementState()

@dataclass(slots=True)
class PendingToolResult:
    """一条待回填的 tool_result（与 ChatMessage 的 tool_result 形状一致）。"""

    role: str
    toolUseId: str  # noqa: N815 - 与消息字段名保持一致
    toolName: str  # noqa: N815
    content: str
    isError: bool = False  # noqa: N815

    def to_message(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "toolUseId": self.toolUseId,
            "toolName": self.toolName,
            "content": self.content,
            "isError": self.isError,
        }

@dataclass(slots=True)
class ToolResultReplacement:
    kind: str
    tool_use_id: str
    replacement: str

# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def tool_results_directory(base_dir: str | None = None) -> str:
    """本进程的落盘目录。

    `base_dir` 允许调用方（测试、或想把产物放在别处的宿主）覆盖根目录。
    默认落在 `~/.corecode/tool-results/<进程随机目录>/`。
    """
    root = base_dir if base_dir is not None else str(tool_results_dir())
    return os.path.join(root, _SESSION_DIR)

def persist_tool_result(
    content: str, tool_use_id: str, base_dir: str | None = None
) -> str | None:
    """把完整内容写盘，返回文件路径；失败返回 None（调用方放弃替换）。"""
    directory = tool_results_directory(base_dir)
    filename = f"{sanitize_path_segment(tool_use_id)}.txt"
    filepath = os.path.join(directory, filename)

    # 双保险：即使 sanitize 之后仍越界，就改用随机文件名。
    relative = os.path.relpath(filepath, directory)
    if relative.startswith("..") or os.path.isabs(relative):
        filepath = os.path.join(directory, f"{uuid.uuid4()}.txt")

    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        # 'x' 模式不覆盖已存在文件；已存在视为成功（幂等）。
        with open(filepath, "x", encoding="utf-8") as handle:
            handle.write(content)
        return filepath
    except FileExistsError:
        return filepath
    except OSError:
        return None

def _build_replacement(content: str, original_size: int, filepath: str) -> str:
    """构造替换文本：预览 + 完整输出路径。

    模型看到它就知道全文在哪，需要时可以再读一次。
    """
    preview, has_more = generate_preview(content)
    parts = [
        PERSISTED_OUTPUT_TAG,
        f"Output too large ({format_chars(original_size)}). "
        f"Full output saved to: {filepath}",
        "",
        f"Preview (first {format_chars(PREVIEW_SIZE_CHARS)}):",
        preview,
    ]
    if has_more:
        parts.append("...")
    parts.append(PERSISTED_OUTPUT_CLOSING_TAG)
    return "\n".join(parts)

def empty_output_text(tool_name: str) -> str:
    return f"({tool_name} completed with no output)"

def is_already_persisted(content: str) -> bool:
    return content.startswith(PERSISTED_OUTPUT_TAG)

# ---------------------------------------------------------------------------
# 单条路径
# ---------------------------------------------------------------------------

def replace_large_tool_result(
    result: PendingToolResult,
    state: ContentReplacementState | None = None,
    threshold: int = DEFAULT_MAX_RESULT_SIZE_CHARS,
    base_dir: str | None = None,
) -> PendingToolResult:
    """单条结果超过阈值时落盘，返回替换后的结果。

    顺序很重要：**先查历史替换**（幂等重放），再判空，再判是否已落盘，
    最后才看长度。这个顺序保证同一 id 反复处理不会写第二个文件。
    """
    content = normalize_tool_result_content(result.content)

    if state is not None:
        remembered = state.replacements.get(result.toolUseId)
        if remembered is not None:
            return PendingToolResult(
                role=result.role,
                toolUseId=result.toolUseId,
                toolName=result.toolName,
                content=remembered,
                isError=result.isError,
            )

    if content.strip() == "":
        if state is not None:
            state.seen_ids.add(result.toolUseId)
        return PendingToolResult(
            role=result.role,
            toolUseId=result.toolUseId,
            toolName=result.toolName,
            content=empty_output_text(result.toolName),
            isError=result.isError,
        )

    if is_already_persisted(content):
        if state is not None:
            state.seen_ids.add(result.toolUseId)
            state.replacements[result.toolUseId] = content
        return PendingToolResult(
            role=result.role,
            toolUseId=result.toolUseId,
            toolName=result.toolName,
            content=content,
            isError=result.isError,
        )

    if len(content) <= threshold:
        return PendingToolResult(
            role=result.role,
            toolUseId=result.toolUseId,
            toolName=result.toolName,
            content=content,
            isError=result.isError,
        )

    filepath = persist_tool_result(content, result.toolUseId, base_dir)
    if filepath is None:
        return PendingToolResult(
            role=result.role,
            toolUseId=result.toolUseId,
            toolName=result.toolName,
            content=content,
            isError=result.isError,
        )

    replacement = _build_replacement(content, len(content), filepath)
    if state is not None:
        state.seen_ids.add(result.toolUseId)
        state.replacements[result.toolUseId] = replacement

    return PendingToolResult(
        role=result.role,
        toolUseId=result.toolUseId,
        toolName=result.toolName,
        content=replacement,
        isError=result.isError,
    )

# ---------------------------------------------------------------------------
# 批路径
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BudgetOutcome:
    results: list[PendingToolResult]
    newly_replaced: list[ToolResultReplacement]

def apply_tool_result_budget(
    results: list[PendingToolResult],
    state: ContentReplacementState | None = None,
    limit: int = MAX_TOOL_RESULTS_PER_BATCH_CHARS,
    base_dir: str | None = None,
) -> BudgetOutcome:
    """把整批可见总量压到 `limit` 以内。"""
    if not results:
        return BudgetOutcome(results=results, newly_replaced=[])

    seen = state.seen_ids if state is not None else set()
    replacements = state.replacements if state is not None else {}

    visible_size = 0
    candidates: list[tuple[str, str, int]] = []
    replacement_map: dict[str, str] = {}

    for result in results:
        content = normalize_tool_result_content(result.content)

        if result.toolUseId in replacements:
            # 已有替换文本：按替换后的长度计费，不再当候选。
            visible_size += len(replacements[result.toolUseId])
            continue

        if result.toolUseId in seen:
            visible_size += len(content)
            continue

        if content.strip() == "":
            # 空输出不计入可见体积。
            seen.add(result.toolUseId)
            continue

        if is_already_persisted(content):
            visible_size += len(content)
            seen.add(result.toolUseId)
            replacements[result.toolUseId] = content
            replacement_map[result.toolUseId] = content
            continue

        visible_size += len(content)
        candidates.append((result.toolUseId, content, len(content)))

    # 先处理最大的：能在最少的落盘次数里腾出最多空间。
    candidates.sort(key=lambda item: (-item[2], item[0]))

    newly_replaced: list[ToolResultReplacement] = []
    for tool_use_id, content, size in candidates:
        if visible_size <= limit:
            break

        filepath = persist_tool_result(content, tool_use_id, base_dir)
        # 注意：无论成功与否都先标记已见，避免下一批重复统计。
        seen.add(tool_use_id)
        if filepath is None:
            continue

        replacement = _build_replacement(content, size, filepath)
        replacement_map[tool_use_id] = replacement
        replacements[tool_use_id] = replacement
        visible_size = visible_size - size + len(replacement)
        newly_replaced.append(
            ToolResultReplacement(
                kind="tool-result",
                tool_use_id=tool_use_id,
                replacement=replacement,
            )
        )

    # 循环结束后，所有候选（含未被替换的）都算「已见」。
    for tool_use_id, _content, _size in candidates:
        seen.add(tool_use_id)

    if not replacement_map:
        return BudgetOutcome(results=results, newly_replaced=[])

    rebuilt = [
        PendingToolResult(
            role=result.role,
            toolUseId=result.toolUseId,
            toolName=result.toolName,
            content=replacement_map.get(result.toolUseId, result.content),
            isError=result.isError,
        )
        for result in results
    ]
    return BudgetOutcome(results=rebuilt, newly_replaced=newly_replaced)
