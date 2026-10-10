"""microcompact —— 最轻的一级压缩。

把**较旧的只读工具输出正文**替换成一句标记，消息条数不变。
这是四级里唯一的「无损结构、只丢正文」操作：对话骨架完整保留，
但读文件/跑命令产生的大段输出被清掉。

为什么只清只读工具的输出？因为编辑类工具（`write_file` / `edit_file` /
`patch_file`）的输出是「我改了什么」的证据。清掉它，模型就会忘记自己
已经做过的修改，然后在下一轮重复改动同一个文件。

白名单见 `constants.COMPACTABLE_TOOLS`。注意 `toolName` 是**精确匹配**，
不做大小写折叠、不 trim —— 这保证了不会误伤名字相似的第三方工具。

触发线 50% 利用率：这是四级里最低的，代价也最小的，所以最早介入。
"""

from __future__ import annotations

from ..types import ChatMessage
from .constants import (
    CLEAR_MARKER,
    COMPACTABLE_TOOLS,
    KEEP_RECENT_TOOL_RESULTS,
    MICROCOMPACT_UTILIZATION,
)
from .tokens import compute_context_stats


def microcompact(messages: list[ChatMessage], model: str) -> list[ChatMessage]:
    """清空较旧的可压缩工具输出。

    返回值语义（调用方依赖这一点）：
      - 未达阈值 / 候选不足 / 无实际改动 → **返回传入的同一个列表对象**
      - 有改动 → 返回新列表

    因此调用方可以安全地用 `result is messages` 判断「有没有变过」，
    从而决定是否需要重算上下文统计。
    """
    stats = compute_context_stats(messages, model)
    if stats.utilization < MICROCOMPACT_UTILIZATION:
        return messages

    # 收集所有可压缩的工具结果下标，按消息数组顺序（不是时间顺序）。
    candidates = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool_result"
        and message.get("toolName") in COMPACTABLE_TOOLS
    ]

    # 候选不超过保留条数时无事可做。注意 `== 3` 也不处理。
    if len(candidates) <= KEEP_RECENT_TOOL_RESULTS:
        return messages

    keep_from = len(candidates) - KEEP_RECENT_TOOL_RESULTS
    indices_to_clear = set(candidates[:keep_from])

    cleared: list[ChatMessage] = []
    changed = False
    for index, message in enumerate(messages):
        if index in indices_to_clear and message.get("content") != CLEAR_MARKER:
            # 只换 content，保留 id / toolUseId / toolName / isError 等全部字段。
            cleared.append({**message, "content": CLEAR_MARKER})
            changed = True
        else:
            cleared.append(message)

    return cleared if changed else messages
