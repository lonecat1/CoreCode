"""四级压缩的**阈值与判定**——用硬编码的期望值钉住。

## 这些数字从哪来

本实现，逐字段比对（`estimate` / `total` / `effective` / `utilization` /
`level` / `source` / `shouldAutoCompact` / `snip` / `micro`），**17/17 完全
一致**——包括 `snip` 的「剪了没剪」与 `microcompact` 的「清了没清」。

那个工具需要 TS 仓库在场，不能进单元测试。所以这里把它的结论**固化成本地
断言**：日后任何阈值漂移都会在这里变红，不需要跑 TS。

## 三条结构条件（不知道会写出「什么都没触发」的假测试）

1. **snip** 需要 `条数 > KEEP_RECENT(12) + MIN_MESSAGES_TO_REMOVE(6)`
   ——至少 19 条；且利用率 ≥ 0.70、至少省下 2000 token。
2. **microcompact** 需要 `tool_result` **多于 `KEEP_RECENT_TOOL_RESULTS(3)`**
   ——它保留最近 3 条，只清更早的。
3. **auto-compact** 在 0.85，`warningLevel` 的 `blocked` 在 0.95。

第一版用例全是 2 条消息，于是 `snip` 永远回 `too_few_messages`、
`microcompact` 永远不动——13 个用例全绿，但什么都没验证。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from corecode.context.compact import should_auto_compact  # noqa: E402
from corecode.context.microcompact import microcompact  # noqa: E402
from corecode.context.model_context import get_model_context_window  # noqa: E402
from corecode.context.snip import snip_compact_conversation  # noqa: E402
from corecode.context.tokens import (  # noqa: E402
    compute_context_stats,
    estimate_messages_tokens,
)

MODEL = "claude-sonnet-4"

def build_messages(chars: int, count: int, tool_ratio: float) -> list[dict]:
    """与 `tools/compaction_compare.py` 完全相同的构造（必须一致）。"""
    messages: list[dict] = []
    tool_every = 0 if tool_ratio <= 0 else max(2, round(1 / tool_ratio))
    for index in range(count):
        is_tool = tool_every > 0 and index % tool_every == tool_every - 1
        content = f"[{index}] " + ("x" * chars)
        if is_tool:
            messages.append(
                {
                    "role": "tool_result",
                    "content": content,
                    "id": f"m{index}",
                    "toolUseId": f"t{index}",
                    "toolName": "read_file",
                    "isError": False,
                }
            )
        else:
            messages.append(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": content,
                    "id": f"m{index}",
                }
            )
    return messages

def snap(messages: list[dict]) -> dict:
    """一次算出四个层次的判定——与对照工具的字段一致。"""
    stats = compute_context_stats(messages, MODEL)
    window = get_model_context_window(MODEL)
    snip = snip_compact_conversation(messages, stats, window.effective_input)
    after_micro = microcompact(messages, MODEL)
    return {
        "total": stats.accounting.total_tokens,
        "effective": stats.effective_input,
        "utilization": round(stats.utilization, 4),
        "level": stats.warning_level,
        "auto": should_auto_compact(messages, MODEL),
        "snip_did": snip.did_snip,
        "snip_reason": snip.reason,
        "micro_changed": after_micro is not messages,
    }

#: `(名字, 字符数, 条数, tool 占比) -> 期望快照`。
#:
#: 期望值抄自 `tools/compaction_compare.py` 的实测输出（TS 与 Python 一致）。
EXPECTED: dict[tuple[str, int, int, float], dict] = {
    # --- 阈值边界 ---
    ("tiny", 100, 2, 0.0): dict(
        level="normal", auto=False, snip_did=False, snip_reason="below_threshold",
        micro_changed=False),
    ("quarter", 80_000, 2, 0.0): dict(
        utilization=0.2692, level="normal", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=False),
    ("half_edge", 138_000, 2, 0.0): dict(
        utilization=0.4643, level="normal", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=False),
    ("half_over", 145_000, 2, 0.0): dict(
        utilization=0.4879, level="normal", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=False),
    ("snip_edge", 193_000, 2, 0.0): dict(
        utilization=0.6493, level="warning", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=False),
    ("snip_over", 210_000, 2, 0.0): dict(
        level="warning", auto=False, snip_did=False,
        snip_reason="too_few_messages", micro_changed=False),
    ("autocompact_edge", 235_000, 2, 0.0): dict(
        utilization=0.7906, level="warning", auto=False, snip_did=False,
        snip_reason="too_few_messages", micro_changed=False),
    ("autocompact_over", 260_000, 2, 0.0): dict(
        utilization=0.8748, level="critical", auto=True, snip_did=False,
        snip_reason="too_few_messages", micro_changed=False),
    ("blocked", 560_000, 2, 0.0): dict(
        utilization=1.0, level="blocked", auto=True, snip_did=False,
        snip_reason="too_few_messages", micro_changed=False),
    # --- 条数够多 → snip 的候选区间成立 ---
    ("many_19_small", 12_000, 19, 0.0): dict(
        utilization=0.3853, level="normal", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=False),
    ("many_19_big", 40_000, 19, 0.0): dict(
        level="blocked", auto=True, snip_did=True,
        snip_reason="snipped_safe_middle_interval", micro_changed=False),
    ("many_30_big", 40_000, 30, 0.0): dict(
        level="blocked", auto=True, snip_did=True,
        snip_reason="snipped_safe_middle_interval", micro_changed=False),
    ("many_30_huge", 90_000, 30, 0.0): dict(
        level="blocked", auto=True, snip_did=True,
        snip_reason="snipped_safe_middle_interval", micro_changed=False),
    # --- 工具结果够多 → microcompact 才会清 ---
    ("micro_4t", 20_000, 12, 0.5): dict(
        utilization=0.5436, level="warning", auto=False, snip_did=False,
        snip_reason="below_threshold", micro_changed=True),
    ("micro_8t", 25_000, 20, 0.5): dict(
        level="blocked", auto=True, snip_did=False,
        snip_reason="no_safe_interval", micro_changed=True),
    ("micro_8t_big", 60_000, 20, 0.5): dict(
        level="blocked", auto=True, snip_did=False,
        snip_reason="no_safe_interval", micro_changed=True),
    ("mixed_30", 45_000, 30, 0.4): dict(
        level="blocked", auto=True, snip_did=False,
        snip_reason="no_safe_interval", micro_changed=True),
}

class CompactionThresholdTests(unittest.TestCase):
    def test_every_case_matches_the_verified_snapshot(self) -> None:
        for (name, chars, count, ratio), expected in EXPECTED.items():
            with self.subTest(case=name):
                actual = snap(build_messages(chars, count, ratio))
                for key, want in expected.items():
                    self.assertEqual(
                        actual[key], want, f"{name}.{key}：期望 {want!r}"
                    )

    def test_the_suite_actually_exercises_every_level(self) -> None:
        """**防「假通过」的元测试。**

        第一版用例全是 2 条消息，`snip` 永远 `too_few_messages`、
        `microcompact` 永远不动——全绿但什么都没验证。

        这里断言：这批用例里**至少各有一个**真正触发 / 明确拒绝的例子。
        """
        snaps = {
            name: snap(build_messages(chars, count, ratio))
            for (name, chars, count, ratio) in EXPECTED
        }
        self.assertTrue(
            any(s["snip_did"] for s in snaps.values()),
            "没有任何用例真的剪过——snip 的候选逻辑完全没被验证",
        )
        self.assertTrue(
            any(s["micro_changed"] for s in snaps.values()),
            "没有任何用例真的清过工具结果——microcompact 没被验证",
        )
        self.assertTrue(
            any(s["auto"] for s in snaps.values()),
            "没有任何用例触发 auto-compact",
        )
        self.assertTrue(
            any(not s["auto"] for s in snaps.values()),
            "所有用例都触发 auto-compact——门槛下限没被验证",
        )
        # 三种 snip 拒绝理由都要出现过，否则「拒绝路径」也没被覆盖
        reasons = {s["snip_reason"] for s in snaps.values()}
        for reason in ("below_threshold", "too_few_messages", "no_safe_interval"):
            with self.subTest(reason=reason):
                self.assertIn(reason, reasons)

    def test_warning_levels_cover_all_four_bands(self) -> None:
        """`normal` / `warning` / `critical` / `blocked` 四档都出现过。"""
        levels = {
            snap(build_messages(chars, count, ratio))["level"]
            for (_, chars, count, ratio) in EXPECTED
        }
        self.assertEqual(levels, {"normal", "warning", "critical", "blocked"})

class BoundaryTests(unittest.TestCase):
    """把 0.50 / 0.70 / 0.85 / 0.95 四个门槛的两侧各试一次。"""

    def _utilization_of(self, chars: int) -> float:
        stats = compute_context_stats(
            [{"role": "user", "content": "x" * chars}], MODEL
        )
        return stats.utilization

    def test_utilization_is_capped_at_one(self) -> None:
        """超过窗口时利用率封顶 1.0（不报 >1，否则下游比较会失真）。"""
        self.assertEqual(self._utilization_of(10_000_000), 1.0)

    def test_microcompact_threshold_is_five_percent_below_snip(self) -> None:
        """`microcompact(0.50)` 先于 `snip(0.70)`——顺序不是随意的。"""
        from corecode.context import constants

        self.assertLess(
            constants.MICROCOMPACT_UTILIZATION, constants.SNIP_COMPACT_THRESHOLD
        )
        self.assertEqual(constants.MICROCOMPACT_UTILIZATION, 0.50)
        self.assertEqual(constants.SNIP_COMPACT_THRESHOLD, 0.70)
        self.assertEqual(constants.AUTOCOMPACT_UTILIZATION, 0.85)
        self.assertEqual(constants.BLOCKED_UTILIZATION, 0.95)

    def test_retention_limits_match_the_original(self) -> None:
        """microcompact 只清「最近 3 条之外」的工具结果。"""
        from corecode.context import constants

        self.assertEqual(constants.KEEP_RECENT_TOOL_RESULTS, 3)
        self.assertEqual(constants.SNIP_KEEP_RECENT_MESSAGES, 12)
        self.assertEqual(constants.SNIP_MIN_MESSAGES_TO_REMOVE, 6)
        self.assertEqual(constants.SNIP_MIN_TOKENS_TO_FREE, 2_000)

    def test_cleared_content_is_the_original_marker(self) -> None:
        """

        `CLEAR_MARKER` 是**模型可见的文本**：模型看到它才知道这里的内容是被
        主动清掉的，而不是本来就空。换成空串（或换个说法）都会改变模型的判断
        依据，而「清了几条」这种布尔断言发现不了。

        （这条是补的：`CLEAR_MARKER = ""` 的变异原本存活——没有任何测试检查
        清空后的**内容**，只检查了「变了没变」。）
        """
        from corecode.context.constants import CLEAR_MARKER

        self.assertEqual(CLEAR_MARKER, "[Output cleared for context space]")

        # 真的清一次，看落地的内容
        messages = build_messages(20_000, 12, 0.5)
        after = microcompact(messages, MODEL)
        cleared = [
            m for m in after
            if m.get("role") == "tool_result" and m.get("content") == CLEAR_MARKER
        ]
        self.assertTrue(cleared, "这个用例应当真的清掉了工具结果")
        # 没被清的那几条正文必须原样保留
        kept = [
            m for m in after
            if m.get("role") == "tool_result" and m.get("content") != CLEAR_MARKER
        ]
        self.assertEqual(len(kept), 3, "应当保留最近 3 条工具结果的原文")
        for message in kept:
            with self.subTest(message=message["id"]):
                self.assertTrue(message["content"].startswith("["))

if __name__ == "__main__":
    unittest.main(verbosity=2)
