"""token 计量与模型窗口表的测试。

重点在**边界语义**：什么时候信任 provider 数字、什么时候必须本地估算，
以及两个 JS/Python 数值差异（round 与 UTF-16 长度）必须对齐。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.context.model_context import (
    UNKNOWN_MODEL_CONTEXT,
    get_model_context_window,
)
from minicode.context.tokens import (
    compute_context_stats,
    estimate_message_tokens,
    estimate_messages_tokens,
    js_round,
    mark_provider_usage_stale,
    message_provider_usage,
    token_count_with_estimation,
    utf16_length,
)


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant(content: str, usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if usage:
        message["providerUsage"] = usage
    return message


def usage(total: int) -> dict:
    return {
        "inputTokens": total,
        "outputTokens": 0,
        "totalTokens": total,
        "source": "test",
    }


class NumericAlignmentTests(unittest.TestCase):
    """这些差异不处理的话，边界处行为会与原实现漂移。"""

    def test_js_round_rounds_half_up(self) -> None:
        # Python 内建 round 是银行家舍入：round(50.5) == 50
        self.assertEqual(js_round(50.5), 51)
        self.assertEqual(js_round(2.5), 3)
        self.assertEqual(js_round(-2.5), -2)
        self.assertEqual(js_round(2.4), 2)

    def test_utf16_length_counts_surrogate_pairs_as_two(self) -> None:
        self.assertEqual(utf16_length("abc"), 3)
        self.assertEqual(utf16_length("你好"), 2)
        # 非 BMP 字符（emoji）在 JS 里算 2 个码元
        self.assertEqual(utf16_length("😀"), 2)
        self.assertEqual(utf16_length("a😀b"), 4)


class EstimationTests(unittest.TestCase):
    def test_estimate_uses_role_specific_ratio(self) -> None:
        # user 比率 3.0，tool_result 比率 2.0
        self.assertEqual(estimate_message_tokens(user("abcdef")), 2)
        self.assertEqual(
            estimate_message_tokens(
                {"role": "tool_result", "content": "abcdef", "toolName": "read_file"}
            ),
            3,
        )

    def test_estimate_is_ceiling(self) -> None:
        # 7 / 3.0 = 2.33 -> 3
        self.assertEqual(estimate_message_tokens(user("abcdefg")), 3)

    def test_tool_call_estimates_from_json_input(self) -> None:
        tokens = estimate_message_tokens(
            {
                "role": "assistant_tool_call",
                "toolUseId": "t1",
                "toolName": "read_file",
                "input": {"path": "a.txt"},
            }
        )
        self.assertGreater(tokens, 0)

    def test_thinking_estimates_from_blocks(self) -> None:
        tokens = estimate_message_tokens(
            {"role": "assistant_thinking", "blocks": [{"type": "thinking", "thinking": "x" * 30}]}
        )
        self.assertGreater(tokens, 0)

    def test_unknown_role_contributes_zero(self) -> None:
        self.assertEqual(estimate_message_tokens({"role": "mystery", "content": "x" * 99}), 0)

    def test_messages_tokens_sums(self) -> None:
        messages = [user("abcdef"), user("abcdef")]
        self.assertEqual(
            estimate_messages_tokens(messages),
            sum(estimate_message_tokens(m) for m in messages),
        )


class UsageMergingTests(unittest.TestCase):
    def test_provider_usage_is_trusted_and_tail_estimated(self) -> None:
        messages = [
            user("a" * 300),
            assistant("done", usage=usage(1000)),
            user("b" * 300),
        ]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.provider_usage_tokens, 1000)
        self.assertEqual(result.total_tokens, 1000 + result.estimated_tokens)
        self.assertEqual(result.source, "provider_usage_plus_estimate")
        self.assertFalse(result.is_exact)
        self.assertEqual(result.usage_boundary["messageIndex"], 1)

    def test_exact_when_nothing_follows_the_boundary(self) -> None:
        messages = [user("a" * 300), assistant("done", usage=usage(1000))]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.source, "provider_usage")
        self.assertTrue(result.is_exact)
        self.assertEqual(result.total_tokens, 1000)

    def test_messages_before_boundary_are_not_re_estimated(self) -> None:
        """边界之前的本地估算不得与 provider 数字相加，否则严重高估。"""
        big_prefix = [user("x" * 30_000) for _ in range(5)]
        messages = big_prefix + [assistant("d", usage=usage(500))]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.total_tokens, 500)
        self.assertEqual(result.provider_usage_tokens, 500)

    def test_estimate_only_without_any_usage(self) -> None:
        messages = [user("a" * 300)]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.source, "estimate_only")
        self.assertEqual(result.provider_usage_tokens, 0)
        self.assertEqual(result.total_tokens, result.estimated_tokens)
        self.assertFalse(result.stale)

    def test_boundary_id_only_for_tool_call_role(self) -> None:
        messages = [
            {
                "role": "assistant_tool_call",
                "toolUseId": "tu-9",
                "toolName": "read_file",
                "input": {},
                "providerUsage": usage(200),
            }
        ]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.usage_boundary["messageId"], "tu-9")

    def test_last_usable_boundary_wins(self) -> None:
        messages = [
            assistant("a", usage=usage(100)),
            user("x"),
            assistant("b", usage=usage(900)),
        ]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.provider_usage_tokens, 900)
        self.assertEqual(result.usage_boundary["messageIndex"], 2)

    # -- stale 语义 ---------------------------------------------------------

    def test_mark_stale_only_touches_usage_bearing_messages(self) -> None:
        plain = user("hi")
        self.assertIs(mark_provider_usage_stale(plain, "why"), plain)

        no_usage = {"role": "assistant", "content": "x"}
        self.assertIs(mark_provider_usage_stale(no_usage, "why"), no_usage)

        with_usage = assistant("x", usage=usage(10))
        marked = mark_provider_usage_stale(with_usage, "why")
        self.assertIsNot(marked, with_usage)
        self.assertTrue(marked["usageStale"])
        self.assertEqual(marked["usageStaleReason"], "why")

    def test_stale_usage_is_not_usable(self) -> None:
        message = mark_provider_usage_stale(assistant("x", usage=usage(10)), "why")
        self.assertIsNone(message_provider_usage(message))

    def test_stale_usage_degrades_to_estimate_only_with_reason(self) -> None:
        messages = [mark_provider_usage_stale(assistant("x" * 300, usage=usage(9999)), "被压缩了")]
        result = token_count_with_estimation(messages)
        self.assertEqual(result.source, "estimate_only")
        self.assertTrue(result.stale)
        self.assertEqual(result.reason, "被压缩了")


class ContextStatsTests(unittest.TestCase):
    def test_utilization_is_capped_at_one(self) -> None:
        # 构造一个远超窗口的上下文
        messages = [user("x" * 4_000_000)]
        stats = compute_context_stats(messages, "claude-sonnet-4")
        self.assertEqual(stats.utilization, 1.0)
        self.assertEqual(stats.warning_level, "blocked")

    def test_warning_levels_follow_the_four_bands(self) -> None:
        from minicode.context.tokens import compute_warning_level

        self.assertEqual(compute_warning_level(0.0), "normal")
        self.assertEqual(compute_warning_level(0.49), "normal")
        self.assertEqual(compute_warning_level(0.50), "warning")
        self.assertEqual(compute_warning_level(0.84), "warning")
        self.assertEqual(compute_warning_level(0.85), "critical")
        self.assertEqual(compute_warning_level(0.94), "critical")
        self.assertEqual(compute_warning_level(0.95), "blocked")
        self.assertEqual(compute_warning_level(1.0), "blocked")

    def test_effective_input_is_window_minus_reserve(self) -> None:
        stats = compute_context_stats([user("hi")], "claude-sonnet-4")
        self.assertEqual(stats.context_window, 200_000)
        self.assertEqual(stats.effective_input, 184_000)


class ModelContextTests(unittest.TestCase):
    def test_specific_patterns_win_over_general(self) -> None:
        """顺序敏感：更具体的模式必须排在更宽泛的模式之前。"""
        self.assertEqual(get_model_context_window("claude-opus-4-6").output_reserve, 16_000)
        self.assertEqual(get_model_context_window("gpt-4.1").context_window, 1_047_576)
        self.assertEqual(get_model_context_window("gpt-4").context_window, 128_000)
        self.assertEqual(
            get_model_context_window("deepseek-reasoner").effective_input, 112_000
        )
        self.assertEqual(
            get_model_context_window("deepseek-chat").effective_input, 124_000
        )

    def test_matching_is_case_insensitive_substring(self) -> None:
        self.assertEqual(
            get_model_context_window("CLAUDE-SONNET-4-20250514").context_window, 200_000
        )
        # 带空格的人读形式也要能命中
        self.assertEqual(get_model_context_window("claude 3.5 sonnet").context_window, 200_000)

    def test_unknown_model_falls_back(self) -> None:
        window = get_model_context_window("totally-new-model")
        self.assertEqual(window, UNKNOWN_MODEL_CONTEXT)
        self.assertEqual(window.effective_input, 120_000)

    def test_empty_model_name_falls_back(self) -> None:
        self.assertEqual(get_model_context_window(""), UNKNOWN_MODEL_CONTEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
