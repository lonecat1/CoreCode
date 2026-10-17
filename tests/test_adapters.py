"""配置层与 Anthropic wire 格式转换的测试。

这两块都是「翻译层」：翻译错了不会立刻报错，只会在某次请求被服务端拒绝时
才暴露，所以需要专门的测试钉住。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.adapters.anthropic import (
    parse_assistant_text,
    parse_retry_after,
    retry_delay_seconds,
    should_retry_status,
    to_wire_messages,
)
from minicode.config import (
    ConfigError,
    load_runtime_config,
    merge_settings,
    resolve_max_output_tokens,
)

_TMP_ROOT = os.path.join(_HERE, "_tmp")


class WireConversionTests(unittest.TestCase):
    """内部消息 -> Messages API 格式。"""

    def test_system_is_extracted_not_left_in_messages(self) -> None:
        system, messages = to_wire_messages(
            [
                {"role": "system", "content": "A"},
                {"role": "system", "content": "B"},
                {"role": "user", "content": "hi"},
            ]
        )
        self.assertEqual(system, "A\n\nB")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")

    def test_consecutive_same_role_messages_are_merged(self) -> None:
        """Messages API 要求 user/assistant 严格交替，合并是硬要求。"""
        _, messages = to_wire_messages(
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant_tool_call",
                    "toolUseId": "t1",
                    "toolName": "read_file",
                    "input": {"path": "a"},
                },
                {
                    "role": "tool_result",
                    "toolUseId": "t1",
                    "toolName": "read_file",
                    "content": "data",
                    "isError": False,
                },
            ]
        )
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "user"])
        # tool_use 块落在 assistant 消息里
        self.assertEqual(messages[1]["content"][0]["type"], "tool_use")
        # tool_result 块落在紧随其后的 user 消息里
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "t1")

    def test_thinking_blocks_are_replayed_verbatim(self) -> None:
        """thinking 块必须原样回传（含签名），否则推理上下文与签名校验都会失败。"""
        block = {"type": "thinking", "thinking": "推理内容", "signature": "sig-abc"}
        _, messages = to_wire_messages(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant_thinking", "blocks": [block]},
                {
                    "role": "assistant_tool_call",
                    "toolUseId": "t1",
                    "toolName": "x",
                    "input": {},
                },
            ]
        )
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIn(block, messages[1]["content"])
        self.assertEqual(messages[1]["content"][0]["signature"], "sig-abc")

    def test_redacted_thinking_is_preserved(self) -> None:
        block = {"type": "redacted_thinking", "data": "opaque"}
        _, messages = to_wire_messages([{"role": "assistant_thinking", "blocks": [block]}])
        self.assertIn(block, messages[0]["content"])

    def test_assistant_progress_is_tagged_for_the_model(self) -> None:
        """进度消息要带 <progress> 标签，否则模型会误以为那是最终答复。"""
        _, messages = to_wire_messages(
            [{"role": "assistant_progress", "content": "还在做"}]
        )
        text = messages[0]["content"][0]["text"]
        self.assertIn("<progress>", text)
        self.assertIn("还在做", text)

    def test_context_summary_is_delivered_as_user_text(self) -> None:
        _, messages = to_wire_messages(
            [{"role": "context_summary", "content": "摘要内容", "compressedCount": 3}]
        )
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("[Context Summary", messages[0]["content"][0]["text"])

    def test_snip_boundary_becomes_a_user_marker(self) -> None:
        _, messages = to_wire_messages(
            [
                {
                    "role": "snip_boundary",
                    "content": "x",
                    "removedMessageIds": [],
                    "removedCount": 0,
                    "tokensFreed": 0,
                }
            ]
        )
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("Snipped", messages[0]["content"][0]["text"])

    def test_snip_boundary_text_matches_the_original_word_for_word(self) -> None:
        """**逐字对齐原版 `buildAnthropicSnipBoundaryText()`。**

        这是模型在被裁剪处看到的**唯一**提示。早先这里是三段自拟文案，语义
        还不一样：

          - 原版在安抚模型：中段被删，但「最近的对话与当前任务上下文都保留
            着」，鼓励它直接用现有上下文继续；
          - 自拟版在警告模型：别对删掉的内容做任何假设。

        两者对同一次裁剪给出不同的行为暗示，所以必须逐字相同才能谈行为对等。

        期望值抄自 `compact/snipCompact.ts:365`，**不要顺手润色**。
        """
        from minicode.adapters.anthropic import _SNIP_BOUNDARY_TEXT

        self.assertEqual(
            _SNIP_BOUNDARY_TEXT,
            "[Snipped earlier conversation segment]\n"
            "\n"
            "A middle portion of the earlier conversation was removed to preserve "
            "context space.\n"
            "The recent conversation and active task context are preserved.",
        )
        # 结构也要对上：标题行、空行、两句正文
        self.assertEqual(len(_SNIP_BOUNDARY_TEXT.split("\n")), 4)
        self.assertEqual(_SNIP_BOUNDARY_TEXT.split("\n")[1], "")

    def test_snip_boundary_ignores_the_stored_content(self) -> None:
        """消息自带的 `content`（含统计信息）不上线，模型只看到固定文案。

        原版同样只用 `buildAnthropicSnipBoundaryText()` 的返回值——统计数字是
        给 UI 看的，写进 wire 只是噪音。
        """
        _, messages = to_wire_messages(
            [
                {
                    "role": "snip_boundary",
                    "content": "removed 12 messages, freed 3456 tokens",
                    "removedMessageIds": [],
                    "removedCount": 12,
                    "tokensFreed": 3456,
                }
            ]
        )
        text = messages[0]["content"][0]["text"]
        self.assertNotIn("3456", text)
        self.assertNotIn("removed 12", text)

    def test_tool_result_error_flag_survives(self) -> None:
        _, messages = to_wire_messages(
            [
                {
                    "role": "tool_result",
                    "toolUseId": "t1",
                    "toolName": "x",
                    "content": "boom",
                    "isError": True,
                }
            ]
        )
        self.assertTrue(messages[0]["content"][0]["is_error"])


class ResponseParsingTests(unittest.TestCase):
    """模型文本 -> (正文, kind)。"""

    def test_final_marker_is_stripped(self) -> None:
        self.assertEqual(parse_assistant_text("<final>\n答案\n</final>"), ("答案", "final"))

    def test_progress_marker_is_detected(self) -> None:
        self.assertEqual(
            parse_assistant_text("<progress>正在读文件"), ("正在读文件", "progress")
        )

    def test_bracket_marker_variants_are_accepted(self) -> None:
        self.assertEqual(parse_assistant_text("[FINAL] done"), ("done", "final"))
        self.assertEqual(parse_assistant_text("[PROGRESS] wait"), ("wait", "progress"))

    def test_unmarked_text_is_untagged(self) -> None:
        self.assertEqual(parse_assistant_text("普通回答"), ("普通回答", None))

    def test_empty_text_stays_empty(self) -> None:
        self.assertEqual(parse_assistant_text("   "), ("", None))


class RetryPolicyTests(unittest.TestCase):
    def test_retryable_statuses(self) -> None:
        self.assertTrue(should_retry_status(429))
        self.assertTrue(should_retry_status(500))
        self.assertTrue(should_retry_status(503))
        self.assertFalse(should_retry_status(400))
        self.assertFalse(should_retry_status(401))
        self.assertFalse(should_retry_status(404))

    def test_retry_after_seconds_and_date(self) -> None:
        self.assertEqual(parse_retry_after("2.5"), 2.5)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("垃圾"))

    def test_retry_after_takes_priority_over_backoff(self) -> None:
        self.assertEqual(retry_delay_seconds(3, 7.0), 7.0)

    def test_backoff_is_bounded(self) -> None:
        for attempt in range(1, 6):
            delay = retry_delay_seconds(attempt, None)
            self.assertLessEqual(delay, 8.0 * 1.25)
            self.assertGreater(delay, 0)


class MaxOutputTokensTests(unittest.TestCase):
    """逐条对应原版 `utils/context.ts` 的 `MODEL_MAX_OUTPUT_TOKEN_RULES`。

    **这组值此前是本实现自己拟的粗粒度规则**（`claude-sonnet-4` → 16000、
    `deepseek` → 8192、`qwen` → 32768），与原版的 18 条精确模式不一致。
    差异不只是数字不同——粗粒度子串会**静默截断用户配置**：用户设
    `maxOutputTokens: 16000`、模型 `deepseek-v4-pro` 时，原版得到 16000，
    粗粒度 `deepseek` 规则会砍成 8192 且不报错。
    """

    def test_known_models_use_their_defaults(self) -> None:
        # 原版 `claude-sonnet-4` 模式 → default 64_000（不是 16_000）
        self.assertEqual(resolve_max_output_tokens("claude-sonnet-4-20250514"), 64_000)
        self.assertEqual(resolve_max_output_tokens("claude-3-5-haiku"), 8_192)

    def test_unknown_model_falls_back_to_the_original_default(self) -> None:
        """原版 `UNKNOWN_MODEL_MAX_OUTPUT_TOKENS` 是 (32000, 64000)。"""
        self.assertEqual(resolve_max_output_tokens("some-new-model"), 32_000)

    def test_configured_value_is_clamped_to_upper_bound(self) -> None:
        self.assertEqual(
            resolve_max_output_tokens("claude-3-5-sonnet", 999_999), 8_192
        )

    def test_configured_value_below_bound_is_kept(self) -> None:
        self.assertEqual(resolve_max_output_tokens("claude-sonnet-4", 1_000), 1_000)

    def test_claude_opus_4_6_default(self) -> None:
        self.assertEqual(resolve_max_output_tokens("claude-opus-4-6"), 128_000)

    def test_more_specific_pattern_wins(self) -> None:
        """**顺序敏感**：`claude-opus-4-6` 必须排在 `opus-4` 之前。

        顺序错了会让新模型的预算被旧模型的规则吃掉——数字看起来合理，
        但比实际能力小得多。
        """
        self.assertEqual(resolve_max_output_tokens("claude-opus-4-6"), 128_000)
        self.assertEqual(resolve_max_output_tokens("claude-opus-4-1"), 32_000)
        self.assertEqual(resolve_max_output_tokens("opus-4"), 32_000)

    def test_deepseek_v4_uses_the_unknown_fallback(self) -> None:
        """**这条钉住一个真实踩到的坑。**

        原版只有 `deepseek-reasoner` 与 `deepseek-chat` 两条规则，
        `deepseek-v4-pro` 不匹配任何规则 → 走兜底 (32000, 64000)。

        于是用户配的 `maxOutputTokens: 16000` 得到 **16000**（保留）。
        本实现此前有一条粗粒度 `deepseek` 规则 (8192, 8192)，会把 16000
        夹成 **8192**——用户的预算被静默砍掉一半，症状是「长回答莫名截断」。
        """
        self.assertEqual(resolve_max_output_tokens("deepseek-v4-pro", 16_000), 16_000)
        self.assertEqual(resolve_max_output_tokens("deepseek-v4-pro"), 32_000)

    def test_deepseek_known_models_keep_their_own_limits(self) -> None:
        """兜底变宽了，但原版明确列出的两个 deepseek 模型仍用各自的上限。"""
        self.assertEqual(resolve_max_output_tokens("deepseek-chat", 16_000), 8_000)
        self.assertEqual(resolve_max_output_tokens("deepseek-chat"), 4_000)
        self.assertEqual(resolve_max_output_tokens("deepseek-reasoner"), 32_000)

    def test_model_name_matching_is_case_insensitive(self) -> None:
        self.assertEqual(resolve_max_output_tokens("Claude-Opus-4-6"), 128_000)

    def test_gpt_and_gemini_defaults(self) -> None:
        self.assertEqual(resolve_max_output_tokens("gpt-4o"), 16_384)
        self.assertEqual(resolve_max_output_tokens("gpt-5"), 128_000)
        self.assertEqual(resolve_max_output_tokens("gemini-2.5-pro"), 65_536)


class SettingsTests(unittest.TestCase):
    #: 这些环境变量会覆盖 settings.json，测试期间必须清空以免被真实环境污染。
    AMBIENT_KEYS = (
        "MINICODE_MODEL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "MINICODE_MAX_OUTPUT_TOKENS",
    )

    def setUp(self) -> None:
        os.makedirs(_TMP_ROOT, exist_ok=True)
        self.settings_path = Path(_TMP_ROOT) / "settings.json"
        self._saved_env = {
            key: os.environ.pop(key, None) for key in self.AMBIENT_KEYS
        }

    def tearDown(self) -> None:
        self.settings_path.unlink(missing_ok=True)
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value

    def _write(self, data: dict) -> None:
        self.settings_path.write_text(json.dumps(data), encoding="utf-8")

    def test_env_is_merged_not_replaced(self) -> None:
        merged = merge_settings(
            {"env": {"A": "1", "B": "2"}}, {"env": {"B": "3"}}
        )
        self.assertEqual(merged["env"], {"A": "1", "B": "3"})

    def test_mcp_server_env_is_merged_deeply(self) -> None:
        merged = merge_settings(
            {"mcpServers": {"s": {"command": "x", "env": {"K": "1", "L": "2"}}}},
            {"mcpServers": {"s": {"env": {"L": "9"}}}},
        )
        server = merged["mcpServers"]["s"]
        self.assertEqual(server["command"], "x")
        self.assertEqual(server["env"], {"K": "1", "L": "9"})

    def test_missing_model_raises(self) -> None:
        self._write({"env": {"ANTHROPIC_AUTH_TOKEN": "t"}})
        with self.assertRaises(ConfigError):
            load_runtime_config(self.settings_path)

    def test_missing_credentials_raises(self) -> None:
        self._write({"model": "claude-sonnet-4"})
        with self.assertRaises(ConfigError):
            load_runtime_config(self.settings_path)

    def test_full_config_resolves_and_builds_headers(self) -> None:
        self._write(
            {
                "model": "claude-sonnet-4",
                "env": {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "https://x.test/"},
            }
        )
        config = load_runtime_config(self.settings_path)
        self.assertEqual(config.model, "claude-sonnet-4")
        self.assertEqual(config.messages_endpoint(), "https://x.test/v1/messages")
        self.assertEqual(config.headers()["Authorization"], "Bearer tok")

    def test_process_env_overrides_settings_env(self) -> None:
        """环境变量必须赢过 settings.json 里的 env，否则 CI 里没法覆盖配置。"""
        self._write(
            {
                "model": "from-settings",
                "env": {"ANTHROPIC_AUTH_TOKEN": "t"},
            }
        )
        os.environ["MINICODE_MODEL"] = "from-process-env"
        config = load_runtime_config(self.settings_path)
        self.assertEqual(config.model, "from-process-env")

    def test_api_key_used_when_no_auth_token(self) -> None:
        self._write({"model": "claude-sonnet-4", "env": {"ANTHROPIC_API_KEY": "key-1"}})
        config = load_runtime_config(self.settings_path)
        self.assertEqual(config.headers()["x-api-key"], "key-1")
        self.assertNotIn("Authorization", config.headers())


if __name__ == "__main__":
    unittest.main(verbosity=2)
