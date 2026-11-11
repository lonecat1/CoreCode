"""上下文管线 —— agent loop 与四级压缩之间的唯一接缝。

agent loop 只需要知道一件事：

    current, model_messages = pipeline.prepare(current, step=..., model_name=...)

它不需要知道有几种压缩、各自阈值是多少、谁改了真实转录。这个二元返回
`(真实消息, 模型可见投影)` 正是「collapse 只改投影」能干净落地的地方。

## 一个 step 内的执行顺序（不可调换）

```
① snip compact       ≥70%  每回合最多成功一次    改真实转录
② microcompact       ≥50%  每步                  改真实转录
③ context collapse   ≥75%  每步                  只改投影
④ auto-compact       ≥85%  仅 step 0             改真实转录
⑤ plan / runtime 注入                            只改投影
→ model.next(投影)
```

顺序有理由：

  - **snip 在 microcompact 之前**：snip 会整段删除消息，先删掉大块比先清空
    正文更划算；反之如果先 microcompact，snip 就看不到那些正文了，
    它按 token 排序的决策会基于被清空后的数据。
  - **collapse 在 microcompact 之后**：collapse 要挑「值得摘要的历史区间」，
    在旧工具输出已被清空后再挑，摘要会更精炼、开销更低。
  - **auto-compact 最后且只在 step 0**：它是最重的手段（要调模型），
    只有前面三级都压不住时才上，而且每回合只考虑一次，避免反复触发。

`prepare` 每次被调用都可能改变 `current`，调用方**必须**用返回值回写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..types import ChatMessage, ModelAdapter
from .collapse import (
    ContextCollapseResult,
    ContextCollapseState,
    apply_context_collapse_if_needed,
)
from .compact import CompressionResult, auto_compact
from .microcompact import microcompact
from .snip import SnipResult, snip_compact_conversation
from .tokens import compute_context_stats

@dataclass
class ContextPipeline:
    """四级压缩的编排者。

    `model_adapter` 只有 collapse 与 auto-compact 需要（它们要调模型生成摘要）；
    为 None 时这两级自动跳过，前两级（纯本地）照常工作。
    """

    model_adapter: ModelAdapter | None = None
    collapse_state: ContextCollapseState = field(default_factory=ContextCollapseState)
    #: 本回合是否已经成功 snip 过。每回合最多成功一次，未成功前每步都会重试。
    snipped_this_turn: bool = False
    #: 宿主的观察回调。全部可选。
    on_stats: Callable[[Any], None] | None = None
    on_snip: Callable[[SnipResult], None] | None = None
    on_collapse: Callable[[ContextCollapseResult], None] | None = None
    on_auto_compact: Callable[[CompressionResult], None] | None = None

    # -- 回合边界 -----------------------------------------------------------

    def on_turn_start(self) -> None:
        self.snipped_this_turn = False

    def on_turn_end(self) -> None:
        self.snipped_this_turn = False

    # -- 主接缝 -------------------------------------------------------------

    def prepare(
        self,
        messages: list[ChatMessage],
        *,
        step: int,
        model_name: str,
    ) -> tuple[list[ChatMessage], list[ChatMessage]]:
        """返回 `(真实消息, 模型可见投影)`。"""
        current = messages

        if not model_name:
            # 没有模型名就算不出窗口，四级压缩全部跳过。
            return current, current

        stats = compute_context_stats(current, model_name)
        self._notify_stats(stats)

        # ① snip：每回合最多成功一次。
        if not self.snipped_this_turn:
            snip_result = snip_compact_conversation(
                current, stats, stats.effective_input
            )
            if snip_result.did_snip:
                current = snip_result.messages
                self.snipped_this_turn = True
                if self.on_snip:
                    self.on_snip(snip_result)
                stats = compute_context_stats(current, model_name)
                self._notify_stats(stats)

        # ② microcompact
        before_micro = current
        current = microcompact(current, model_name)
        if current is not before_micro:
            stats = compute_context_stats(current, model_name)
            self._notify_stats(stats)

        # ③ context collapse（只改投影）
        collapse_result = apply_context_collapse_if_needed(
            current,
            model_name,
            self.model_adapter,
            self.collapse_state,
        )
        self._sync_collapse_state(collapse_result.state)
        model_messages = collapse_result.messages
        if collapse_result.collapsed and self.on_collapse:
            self.on_collapse(collapse_result)

        # ④ auto-compact：最重的手段，仅 step 0。
        if step == 0:
            auto_result = self._maybe_auto_compact(current, model_messages, model_name)
            if auto_result is not None:
                current = auto_result.messages
                model_messages = current
                # 真实转录被大幅重写，旧 span 的位置已经失效。
                #
                # **必须原地清空，不能替换引用。** 调用方（host）持有同一个
                # state 对象，并把它持久化进会话文件；替换引用会让调用方继续
                # 在原地同步调用方的对象（`args.contextCollapseState.spans =
                # [...nextState.spans]`），这里保持一致。
                #
                # 症状本来不明显（投影时会过滤掉失效 span），但会话文件里会
                # 一直累积指向已不存在消息的 span，而代码本处的不变量写着
                # 「原地更新」——不变量与实际行为不一致，比数据脏更麻烦。
                self._sync_collapse_state(ContextCollapseState())
                if self.on_auto_compact:
                    self.on_auto_compact(auto_result)

        return current, model_messages

    # -- 内部 ---------------------------------------------------------------

    def _maybe_auto_compact(
        self,
        current: list[ChatMessage],
        model_messages: list[ChatMessage],
        model_name: str,
    ) -> CompressionResult | None:
        if self.model_adapter is None:
            return None

        stats = compute_context_stats(model_messages, model_name)
        self._notify_stats(stats)
        if stats.warning_level not in ("critical", "blocked"):
            return None

        return auto_compact(model_messages, model_name, self.model_adapter)

    def _notify_stats(self, stats: Any) -> None:
        if self.on_stats:
            self.on_stats(stats)

    def _sync_collapse_state(self, next_state: ContextCollapseState) -> None:
        """把管线持有的状态与调用方共享的对象对齐。

        `agent_loop` 持有同一个 state 对象（会持久化到会话文件），因此这里
        原地更新它的字段，而不是替换引用。
        """
        self.collapse_state.spans = list(next_state.spans)
        self.collapse_state.enabled = next_state.enabled
        self.collapse_state.consecutive_failures = next_state.consecutive_failures

# 这里曾有一个 `NullContextPipeline`，`prepare()` 直接返回 `(messages, messages)`。
# 它**从未被任何地方使用**——`agent_loop.py` 里的 `IdentityContextPipeline` 做
# 完全相同的事，而实际装配用的是那一个。
#
# 两份功能相同的空实现放着，下一个读者会不知道该用哪个、也不知道哪一个才是
# 「官方」的空管线。已删除。`tools/check_orphans.py` 就是为此写的：它扫出了这条。
