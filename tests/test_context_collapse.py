"""collapse / auto-compact / tool-result-storage / pipeline 的测试。

重点：
  - collapse 是**投影**：真实转录绝不能被改动；
  - collapse 的 usage 失效范围（本实现对原实现做了修正，见 docs/divergences.md）；
  - auto-compact 的三重门控与连续失败熔断；
  - 大结果落盘的单条与批次两道闸门；
  - pipeline 的四级顺序与「snip 每回合最多一次」。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.context.collapse import (
    CollapseSpan,
    ContextCollapseState,
    apply_context_collapse_if_needed,
    build_collapsed_summary_content,
    create_context_collapse_state,
    find_collapse_candidate,
    project_collapsed_view,
    tool_group_is_closed,
)
from minicode.context.compact import (
    compact_conversation,
    find_retention_boundary,
    group_messages_by_api_round,
    parse_summary_from_response,
    reset_auto_compact_state,
)
from minicode.context.pipeline import ContextPipeline
from minicode.context.tool_result_storage import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    MAX_TOOL_RESULTS_PER_BATCH_CHARS,
    PREVIEW_SIZE_CHARS,
    ContentReplacementState,
    PendingToolResult,
    apply_tool_result_budget,
    create_content_replacement_state,
    empty_output_text,
    format_chars,
    generate_preview,
    replace_large_tool_result,
)
from minicode.context.tokens import (
    compute_context_stats,
    mark_provider_usage_stale,
    message_provider_usage,
)
from minicode.types import AgentStep

MODEL = "claude-sonnet-4"
WINDOW = 184_000
USER_CHARS_PER_TOKEN = 3.0

#: 落盘类测试的输出目录。刻意放在仓库内而不是 ~/.mini-code：
#: 一来沙箱允许写这里，二来测试不会污染真实用户数据。
_OUTPUT_DIR = os.path.join(_HERE, "_tmp", "tool-results")

unittest.TestCase.maxDiff = None


def filler(chars: int) -> str:
    return "x" * chars


def chars_needed(ratio: float) -> int:
    return int(ratio * WINDOW * 1.15 * USER_CHARS_PER_TOKEN)


def user(content: str, ident: str | None = None) -> dict:
    message = {"role": "user", "content": content}
    if ident:
        message["id"] = ident
    return message


def assistant(content: str, ident: str | None = None, usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if ident:
        message["id"] = ident
    if usage:
        message["providerUsage"] = usage
    return message


def usage(total: int) -> dict:
    return {"inputTokens": total, "outputTokens": 0, "totalTokens": total, "source": "t"}


def tool_result(content: str, tool_use_id: str = "t", tool_name: str = "read_file") -> dict:
    return {
        "role": "tool_result",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "content": content,
        "isError": False,
    }


def tool_call(tool_use_id: str = "t", tool_name: str = "read_file") -> dict:
    return {
        "role": "assistant_tool_call",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "input": {},
    }


class ScriptedAdapter:
    """返回固定摘要的假适配器，并记录最后一次收到的提示词。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.last_prompt = ""

    def next(self, messages, *, tools=None, signal=None):  # noqa: ANN001
        self.calls += 1
        # 记下送进模型的正文，便于断言「某些内容确实参与了摘要」。
        self.last_prompt = "\n".join(
            str(m.get("content", "")) for m in messages
        )
        if not self._responses:
            return AgentStep(type="assistant", content="<summary>空</summary>")
        return AgentStep(type="assistant", content=self._responses.pop(0))


# ---------------------------------------------------------------------------
# collapse
# ---------------------------------------------------------------------------


class ToolGroupClosureTests(unittest.TestCase):
    def test_parallel_batch_is_closed_here(self) -> None:
        """与 snip 相反：这里用双向集合相等，并行批次判定为闭合。"""
        messages = [
            tool_call("a"),
            tool_call("b"),
            tool_result("1", "a"),
            tool_result("2", "b"),
        ]
        self.assertTrue(tool_group_is_closed(messages))

    def test_missing_result_is_not_closed(self) -> None:
        self.assertFalse(tool_group_is_closed([tool_call("a")]))

    def test_empty_is_closed(self) -> None:
        self.assertTrue(tool_group_is_closed([]))

    def test_extra_result_is_not_closed(self) -> None:
        self.assertFalse(
            tool_group_is_closed([tool_call("a"), tool_result("1", "a"), tool_result("2", "b")])
        )


class ProjectionTests(unittest.TestCase):
    def _committed_state(self, message_ids: list[str]) -> ContextCollapseState:
        return ContextCollapseState(
            spans=[
                CollapseSpan(
                    id="span-1",
                    start_message_id=message_ids[0],
                    end_message_id=message_ids[-1],
                    message_ids=message_ids,
                    summary="被折叠的摘要",
                    tokens_before=1000,
                    tokens_after=100,
                    status="committed",
                    created_at=1,
                )
            ]
        )

    def test_projection_replaces_span_with_summary(self) -> None:
        messages = [
            assistant("before", "a0"),
            user("one", "u1"),
            assistant("two", "a1"),
            user("after", "u2"),
        ]
        state = self._committed_state(["u1", "a1"])
        projected = project_collapsed_view(messages, state)

        self.assertEqual(len(projected), 3)
        self.assertEqual(projected[0]["content"], "before")
        self.assertEqual(projected[1]["role"], "context_summary")
        self.assertIn("[Collapsed context summary]", projected[1]["content"])
        self.assertIn("被折叠的摘要", projected[1]["content"])
        self.assertEqual(projected[2]["content"], "after")

    def test_real_transcript_is_not_modified(self) -> None:
        """这是 collapse 存在的全部意义：真实转录一行不动。"""
        messages = [user("one", "u1"), assistant("two", "a1"), user("after", "u2")]
        before = [dict(m) for m in messages]
        project_collapsed_view(messages, self._committed_state(["u1", "a1"]))
        self.assertEqual(messages, before)

    def test_uncommitted_span_is_ignored(self) -> None:
        messages = [user("one", "u1"), assistant("two", "a1")]
        state = ContextCollapseState(
            spans=[
                CollapseSpan(
                    id="s",
                    start_message_id="u1",
                    end_message_id="a1",
                    message_ids=["u1", "a1"],
                    summary="x",
                    tokens_before=1,
                    status="staged",
                )
            ]
        )
        self.assertIs(project_collapsed_view(messages, state), messages)

    def test_span_with_missing_id_is_silently_dropped(self) -> None:
        """span 引用的消息不在了（例如被后续压缩删掉）→ 该 span 静默失效。"""
        messages = [user("only", "u9")]
        state = self._committed_state(["u1", "a1"])
        self.assertIs(project_collapsed_view(messages, state), messages)

    def test_non_contiguous_span_is_rejected(self) -> None:
        messages = [user("1", "u1"), user("2", "u2"), user("3", "u3")]
        state = self._committed_state(["u1", "u3"])
        self.assertIs(project_collapsed_view(messages, state), messages)

    def test_disabled_state_returns_same_object(self) -> None:
        messages = [user("x", "u1")]
        state = ContextCollapseState(enabled=False)
        self.assertIs(project_collapsed_view(messages, state), messages)

    # -- 本实现对原实现的修正 ------------------------------------------------

    def test_usage_before_span_is_preserved(self) -> None:
        """span 之前的消息完全未被触碰，其 provider usage 必须仍然可用。"""
        messages = [
            assistant("early", "a0", usage=usage(500)),
            user("one", "u1"),
            assistant("two", "a1"),
            user("after", "u2"),
        ]
        projected = project_collapsed_view(messages, self._committed_state(["u1", "a1"]))
        self.assertIsNotNone(
            message_provider_usage(projected[0]),
            "span 之前消息的 usage 不应被标记失效",
        )

    def test_usage_after_span_is_marked_stale(self) -> None:
        """span 之后消息的 usage 是在「消息更多」时测得的，已不代表当前投影。"""
        messages = [
            user("one", "u1"),
            assistant("two", "a1"),
            assistant("after", "a3", usage=usage(900)),
        ]
        projected = project_collapsed_view(messages, self._committed_state(["u1", "a1"]))
        tail = projected[-1]
        self.assertTrue(tail.get("usageStale"))
        self.assertIsNone(message_provider_usage(tail))

    def test_stats_do_not_permanently_degrade(self) -> None:
        """修正后的关键收益：投影里仍存在可用的 provider usage。"""
        messages = [
            assistant("early", "a0", usage=usage(1000)),
            user("one", "u1"),
            assistant("two", "a1"),
            user("after", "u2"),
        ]
        projected = project_collapsed_view(messages, self._committed_state(["u1", "a1"]))
        stats = compute_context_stats(projected, MODEL)
        self.assertNotEqual(
            stats.accounting.source,
            "estimate_only",
            "span 之前仍有可用 usage，计量不应退化为纯估算",
        )


class CollapseCandidateTests(unittest.TestCase):
    def test_no_candidate_when_too_short(self) -> None:
        messages = [user("a"), assistant("b"), user("c")]
        self.assertIsNone(find_collapse_candidate(messages, create_context_collapse_state()))

    def test_candidate_respects_recent_window(self) -> None:
        messages = [assistant(filler(6000), f"a{i}") for i in range(40)]
        messages.append(user("最后", "last"))
        candidate = find_collapse_candidate(
            messages, create_context_collapse_state(), current_tokens=10**9
        )
        self.assertIsNotNone(candidate)
        # 候选不得侵入尾部保留窗口
        self.assertLess(candidate.end_index, len(messages) - 12)

    def test_existing_span_excludes_its_messages(self) -> None:
        messages = [assistant(filler(6000), f"a{i}") for i in range(40)]
        messages.append(user("最后", "last"))
        first = find_collapse_candidate(
            messages, create_context_collapse_state(), current_tokens=10**9
        )
        self.assertIsNotNone(first)

        state = ContextCollapseState(
            spans=[
                CollapseSpan(
                    id="s1",
                    start_message_id=first.start_message_id,
                    end_message_id=first.end_message_id,
                    message_ids=list(first.message_ids),
                    summary="已折叠",
                    tokens_before=first.tokens_before,
                    status="committed",
                )
            ]
        )
        second = find_collapse_candidate(messages, state, current_tokens=10**9)
        if second is not None:
            self.assertFalse(
                set(second.message_ids) & set(first.message_ids),
                "已被 span 覆盖的消息不应再次入选",
            )


class ApplyCollapseTests(unittest.TestCase):
    def _long(self) -> list[dict]:
        """构造越过 collapse 阈值（75%）的对话。

        两个约束必须同时满足，否则测试会「因为没触发而通过」：
          1. 利用率 ≥ 0.75（collapse 的触发线）；
          2. 体积要由**大量中等消息**提供，而不是一两条巨型消息——
             候选区间受尾部保留窗口限制，巨型消息会把可用区间挤没。
        """
        return [assistant(filler(6000), f"a{i}") for i in range(96)] + [
            user("最后", "last")
        ]

    def test_fixture_actually_crosses_threshold(self) -> None:
        """守住夹具本身：数据没有越过阈值时，后续断言毫无意义。"""
        messages = self._long()
        stats = compute_context_stats(messages, MODEL)
        self.assertGreaterEqual(
            stats.utilization, 0.75, f"夹具未越过阈值（{stats.utilization:.3f}）"
        )

    def test_below_threshold_returns_projection_of_current_state(self) -> None:
        messages = [user("a"), assistant("b"), user("c")]
        result = apply_context_collapse_if_needed(
            messages, MODEL, ScriptedAdapter([]), create_context_collapse_state()
        )
        self.assertFalse(result.collapsed)

    def test_successful_collapse_commits_a_span(self) -> None:
        messages = self._long()
        original_length = len(messages)
        adapter = ScriptedAdapter(["<summary>折叠内容</summary>", "<summary>再来一段</summary>"])
        result = apply_context_collapse_if_needed(
            messages, MODEL, adapter, create_context_collapse_state()
        )
        self.assertTrue(result.collapsed, "应当折叠至少一段")
        self.assertGreaterEqual(len(result.spans), 1)
        self.assertEqual(result.spans[0].status, "committed")
        # 投影比原始短
        self.assertLess(len(result.messages), len(messages))
        # 真实转录未动
        self.assertEqual(len(messages), original_length)

    def test_unparseable_summary_counts_as_failure(self) -> None:
        messages = self._long()
        adapter = ScriptedAdapter(["没有标签的响应"])
        result = apply_context_collapse_if_needed(
            messages, MODEL, adapter, create_context_collapse_state()
        )
        self.assertFalse(result.collapsed)
        self.assertEqual(result.state.consecutive_failures, 1)

    def test_consecutive_failures_disable_collapse(self) -> None:
        messages = self._long()
        state = create_context_collapse_state()
        for _ in range(3):
            result = apply_context_collapse_if_needed(
                messages, MODEL, ScriptedAdapter(["坏响应"]), state
            )
            state = result.state
        self.assertFalse(state.enabled, "连续失败 3 次后必须自我禁用")

        # 禁用后立即返回未改动，且不再调用适配器
        adapter = ScriptedAdapter([])
        after = apply_context_collapse_if_needed(messages, MODEL, adapter, state)
        self.assertFalse(after.collapsed)
        self.assertEqual(adapter.calls, 0)

    def test_no_adapter_does_not_count_as_failure(self) -> None:
        messages = self._long()
        result = apply_context_collapse_if_needed(
            messages, MODEL, None, create_context_collapse_state()
        )
        self.assertFalse(result.collapsed)
        self.assertEqual(result.state.consecutive_failures, 0)


# ---------------------------------------------------------------------------
# auto-compact
# ---------------------------------------------------------------------------


class AutoCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        """复位 auto-compact 的**进程级熔断器**。

        那是一个模块级可变状态（`compact._state`），一旦某个用例把它跑到
        `disabled = True`，**同进程内后续所有用例的 auto-compact 都会静默失效**。

        不隔离的后果不是「测试报错」，而是**测试因为错误的原因通过**：
        本文件下面的 `PipelineTests` 会以为 auto-compact 没触发是因为阈值没到，
        实际是因为上一个用例把它关了。这条隔离是补测试时才发现的。
        """
        reset_auto_compact_state()

    def tearDown(self) -> None:
        reset_auto_compact_state()

    def test_api_round_grouping_keeps_tool_pairs_together(self) -> None:
        messages = [
            user("u1"),
            tool_call("a"),
            tool_call("b"),
            tool_result("1", "a"),
            tool_result("2", "b"),
            assistant("done"),
        ]
        rounds = group_messages_by_api_round(messages)
        # call 与 result 必须在同一轮里，否则会被切断配对
        flattened = [index for round_ in rounds for index in round_]
        self.assertEqual(flattened, list(range(len(messages))))
        for round_ in rounds:
            indices = set(round_)
            for index, message in enumerate(messages):
                if message.get("role") == "assistant_tool_call" and index in indices:
                    self.assertTrue(
                        any(
                            i in indices and messages[i].get("role") == "tool_result"
                            for i in range(len(messages))
                        ),
                        "同一轮里必须同时包含 tool_call 与其结果",
                    )

    def test_retention_boundary_never_splits_a_round(self) -> None:
        messages = [user(filler(2000), f"u{i}") for i in range(30)]
        messages.append(tool_call("x"))
        messages.append(tool_result("结果", "x"))
        messages.append(user("最后"))
        boundary = find_retention_boundary(messages)
        # 边界处不应是 tool_result（那意味着 tool_call 被切在了压缩侧）
        self.assertNotEqual(messages[boundary].get("role"), "tool_result")

    def test_compact_produces_context_summary(self) -> None:
        # 总量必须超过 MAX_KEEP_TOKENS（40000），否则没有可压缩前缀。
        messages = [{"role": "system", "content": "sys"}] + [
            user(filler(4000), f"u{i}") for i in range(60)
        ]
        adapter = ScriptedAdapter(["<summary>摘要正文</summary>"])
        result = compact_conversation(messages, adapter)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.summary["role"], "context_summary")
        self.assertEqual(result.summary["content"], "摘要正文")
        self.assertGreater(result.removed_count, 0)
        # system 消息必须保留在最前
        self.assertEqual(result.messages[0]["role"], "system")
        self.assertEqual(result.messages[1]["role"], "context_summary")

    def test_compact_does_not_drop_a_leading_non_system_message(self) -> None:
        """本实现对原实现的修正：原实现盲切下标 0 会吞掉首条非 system 消息。"""
        messages = [user("这是第一条，必须保住", "first")] + [
            user(filler(4000), f"u{i}") for i in range(60)
        ]
        adapter = ScriptedAdapter(["<summary>摘要</summary>"])
        result = compact_conversation(messages, adapter)
        self.assertIsNotNone(result)
        assert result is not None
        # 它必须进入摘要输入，而不是凭空消失
        self.assertIn("这是第一条", adapter.last_prompt)

    def test_compact_returns_none_when_too_short(self) -> None:
        self.assertIsNone(compact_conversation([user("a"), assistant("b")], ScriptedAdapter([])))

    def test_summary_parse_requires_tags(self) -> None:
        self.assertEqual(parse_summary_from_response("<summary>hi</summary>"), "hi")
        self.assertIsNone(parse_summary_from_response("no tags"))
        self.assertIsNone(parse_summary_from_response("<summary></summary>"))
        self.assertIsNone(parse_summary_from_response("</summary><summary>"))


# ---------------------------------------------------------------------------
# tool-result storage
# ---------------------------------------------------------------------------


class PreviewTests(unittest.TestCase):
    def test_short_content_is_returned_whole(self) -> None:
        preview, has_more = generate_preview("短内容")
        self.assertEqual(preview, "短内容")
        self.assertFalse(has_more)

    def test_exactly_2000_is_not_truncated(self) -> None:
        content = "a" * PREVIEW_SIZE_CHARS
        preview, has_more = generate_preview(content)
        self.assertEqual(len(preview), PREVIEW_SIZE_CHARS)
        self.assertFalse(has_more)

    def test_hard_cut_when_last_newline_is_early(self) -> None:
        """最后一个换行位置不严格大于 1000 时，硬切 2000。"""
        content = "a" * 100 + "\n" + "b" * 5000
        preview, has_more = generate_preview(content)
        self.assertTrue(has_more)
        self.assertEqual(len(preview), PREVIEW_SIZE_CHARS)

    def test_newline_cut_when_last_newline_is_late(self) -> None:
        content = "a" * 1500 + "\n" + "b" * 5000
        preview, has_more = generate_preview(content)
        self.assertTrue(has_more)
        self.assertEqual(preview, "a" * 1500, "应当在换行处切，且不含换行符")

    def test_format_chars_buckets(self) -> None:
        self.assertEqual(format_chars(300), "300 chars")
        self.assertEqual(format_chars(52_000), "52K chars")
        self.assertEqual(format_chars(1_200_000), "1.2M chars")


class ReplaceLargeResultTests(unittest.TestCase):
    def _result(self, content: str, ident: str = "t1") -> PendingToolResult:
        return PendingToolResult(
            role="tool_result", toolUseId=ident, toolName="read_file", content=content
        )

    def test_small_result_is_untouched(self) -> None:
        state = create_content_replacement_state()
        result = replace_large_tool_result(self._result("小内容"), state, base_dir=_OUTPUT_DIR)
        self.assertEqual(result.content, "小内容")

    def test_exactly_at_threshold_is_not_persisted(self) -> None:
        state = create_content_replacement_state()
        content = "a" * DEFAULT_MAX_RESULT_SIZE_CHARS
        result = replace_large_tool_result(self._result(content), state, base_dir=_OUTPUT_DIR)
        self.assertEqual(result.content, content, "恰好等于阈值不应触发落盘")

    def test_oversized_result_is_persisted_with_preview(self) -> None:
        state = create_content_replacement_state()
        content = "line\n" * 20_000
        result = replace_large_tool_result(self._result(content), state, base_dir=_OUTPUT_DIR)

        self.assertIn("<persisted-output>", result.content)
        self.assertIn("</persisted-output>", result.content)
        self.assertIn("Full output saved to:", result.content)
        self.assertIn("Preview (first 2K chars):", result.content)
        self.assertLess(len(result.content), len(content))

    def test_replacement_is_idempotent_per_tool_use_id(self) -> None:
        state = create_content_replacement_state()
        content = "line\n" * 20_000
        first = replace_large_tool_result(self._result(content), state, base_dir=_OUTPUT_DIR)
        second = replace_large_tool_result(self._result(content), state, base_dir=_OUTPUT_DIR)
        self.assertEqual(first.content, second.content, "同一 id 必须得到字节相同的替换")

    def test_empty_output_becomes_a_marker(self) -> None:
        state = create_content_replacement_state()
        result = replace_large_tool_result(self._result("   "), state, base_dir=_OUTPUT_DIR)
        self.assertEqual(result.content, empty_output_text("read_file"))

    def test_already_persisted_is_recognised(self) -> None:
        state = create_content_replacement_state()
        content = "<persisted-output>\nold\n</persisted-output>"
        result = replace_large_tool_result(self._result(content), state, base_dir=_OUTPUT_DIR)
        self.assertEqual(result.content, content)
        self.assertIn("t1", state.replacements)

    def test_persisted_file_actually_contains_full_output(self) -> None:
        """落盘的意义在于全文可追溯，所以文件内容必须与原文一致。"""
        state = create_content_replacement_state()
        content = "line\n" * 20_000
        result = replace_large_tool_result(
            self._result(content, "traceme"), state, base_dir=_OUTPUT_DIR
        )
        marker = "Full output saved to: "
        start = result.content.index(marker) + len(marker)
        path = result.content[start : result.content.index("\n", start)]
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), content)


class BatchBudgetTests(unittest.TestCase):
    def _result(self, ident: str, size: int) -> PendingToolResult:
        return PendingToolResult(
            role="tool_result", toolUseId=ident, toolName="read_file", content="a" * size
        )

    def test_empty_batch(self) -> None:
        outcome = apply_tool_result_budget([], create_content_replacement_state())
        self.assertEqual(outcome.results, [])

    def test_within_budget_returns_same_list_object(self) -> None:
        state = create_content_replacement_state()
        results = [self._result("t1", 1000), self._result("t2", 1000)]
        outcome = apply_tool_result_budget(results, state, base_dir=_OUTPUT_DIR)
        self.assertIs(outcome.results, results, "无替换时应返回原列表引用")

    def test_over_budget_persists_largest_first(self) -> None:
        state = create_content_replacement_state()
        results = [
            self._result("small", 10_000),
            self._result("huge", MAX_TOOL_RESULTS_PER_BATCH_CHARS),
            self._result("medium", 50_000),
        ]
        outcome = apply_tool_result_budget(results, state, base_dir=_OUTPUT_DIR)
        replaced = {item.tool_use_id for item in outcome.newly_replaced}
        self.assertIn("huge", replaced, "应先落盘最大的那个")
        by_id = {r.toolUseId: r for r in outcome.results}
        self.assertIn("<persisted-output>", by_id["huge"].content)

    def test_seen_ids_prevent_double_counting(self) -> None:
        state = create_content_replacement_state()
        size = MAX_TOOL_RESULTS_PER_BATCH_CHARS // 4
        first = [self._result(f"t{i}", size) for i in range(4)]
        apply_tool_result_budget(first, state, base_dir=_OUTPUT_DIR)
        # 第二批同样大小，但已被计过，不应再触发替换
        second = [self._result(f"t{i}", size) for i in range(4)]
        outcome = apply_tool_result_budget(second, state, base_dir=_OUTPUT_DIR)
        self.assertEqual(outcome.newly_replaced, [])


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        """复位进程级熔断器——理由见 `AutoCompactTests.setUp`。"""
        reset_auto_compact_state()

    def tearDown(self) -> None:
        reset_auto_compact_state()

    def test_below_all_thresholds_changes_nothing(self) -> None:
        pipeline = ContextPipeline(model_adapter=ScriptedAdapter([]))
        messages = [user("hi", "u1"), assistant("ok", "a1")]
        current, projected = pipeline.prepare(messages, step=0, model_name=MODEL)
        self.assertEqual(len(current), 2)
        self.assertIs(current, projected, "无压缩时两个视图应相同")

    def test_no_model_name_skips_everything(self) -> None:
        pipeline = ContextPipeline(model_adapter=ScriptedAdapter([]))
        messages = [user(filler(200_000), "u1")]
        current, projected = pipeline.prepare(messages, step=0, model_name="")
        self.assertIs(current, projected)
        self.assertEqual(len(current), 1)

    def test_microcompact_runs_and_returns_new_current(self) -> None:
        pipeline = ContextPipeline(model_adapter=None)
        messages = [
            {"role": "system", "content": "sys"},
            user(filler(chars_needed(0.50)), "u0"),
        ] + [tool_result(filler(2000), f"t{i}") for i in range(5)]

        current, _ = pipeline.prepare(messages, step=0, model_name=MODEL)
        self.assertIsNot(current, messages)
        self.assertEqual(current[2]["content"], "[Output cleared for context space]")

    def test_snip_happens_at_most_once_per_turn(self) -> None:
        pipeline = ContextPipeline(model_adapter=None)
        middle = 40
        per_message = chars_needed(0.70) // middle
        messages = [{"role": "system", "content": "sys"}, assistant("start", "a-start")]
        for index in range(middle):
            messages.append(assistant(filler(per_message), f"a{index}"))
            messages.append(user(filler(per_message), f"u{index}"))
        messages.append(user("继续", "last"))

        current, _ = pipeline.prepare(messages, step=0, model_name=MODEL)
        self.assertTrue(pipeline.snipped_this_turn, "首个 step 应当成功 snip")
        length_after_first = len(current)

        # 同一回合内后续 step 不应再删
        again, _ = pipeline.prepare(current, step=1, model_name=MODEL)
        self.assertEqual(len(again), length_after_first)

        # 新回合重置标记
        pipeline.on_turn_start()
        self.assertFalse(pipeline.snipped_this_turn)

    def test_stats_callback_fires(self) -> None:
        seen: list = []
        pipeline = ContextPipeline(model_adapter=None, on_stats=seen.append)
        pipeline.prepare([user("hi")], step=0, model_name=MODEL)
        self.assertTrue(seen)
        self.assertTrue(hasattr(seen[0], "utilization"))

    def test_collapse_state_is_shared_in_place(self) -> None:
        """管线必须原地更新共享的 state 对象，宿主才能持久化到会话。"""
        shared = create_context_collapse_state()
        pipeline = ContextPipeline(model_adapter=None, collapse_state=shared)
        self.assertIs(pipeline.collapse_state, shared)
        pipeline.prepare([user("hi")], step=0, model_name=MODEL)
        self.assertIs(pipeline.collapse_state, shared)

    def test_collapse_step_syncs_the_shared_state_in_place(self) -> None:
        """**collapse 真正跑过之后也要保持同一性。**

        上面那条用 `model_adapter=None`，而 collapse 需要模型生成摘要——
        它根本不会执行。于是「collapse 之后替换引用」这个变异能存活：
        用 None 适配器时 `apply_context_collapse_if_needed` 原样返回同一个
        state 对象，替换与原地更新没有区别。

        这条用一个真会折叠的适配器把那条路径走通。
        """
        shared = create_context_collapse_state()
        adapter = ScriptedAdapter(["<summary>折叠摘要</summary>"])
        pipeline = ContextPipeline(model_adapter=adapter, collapse_state=shared)
        # **先跳过 snip。**
        #
        # 管线的顺序是 snip（阈值 70%）→ microcompact → collapse（阈值 75%）。
        # 利用率 100% 时 snip 会先删掉一大段，把它降到 75% 以下，collapse
        # 于是不再触发——这是**正确的**管线行为，不是缺陷。
        # 要单独观察 collapse 就必须让它先「本回合已 snip 过」。
        pipeline.snipped_this_turn = True

        # 造一个够大、够多的历史：collapse 的阈值是 75%，
        # 且候选区间必须落在「尾部保留窗口」之前——
        # `CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES = 12`，所以消息数要多于 12，
        # 否则 `protectedStart` 算出来是 0，候选直接为空。
        messages = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": filler(40_000),
                "id": f"m{index}",
            }
            for index in range(20)
        ]
        pipeline.prepare(messages, step=0, model_name=MODEL)

        self.assertIs(pipeline.collapse_state, shared, "collapse 后必须仍是同一个对象")
        self.assertTrue(shared.spans, "这次折叠真的发生了（否则本条测不到东西）")

    def test_auto_compact_only_runs_on_the_first_step(self) -> None:
        """**auto-compact 只在 step 0 尝试。**

        它是最重的手段（要调模型），每步都试会让一个回合里反复触发摘要调用。
        原版写着 `if (step === 0 && modelName)`。

        这条用同一个超限历史分别跑 step=0 与 step=1，断言后者一次都不调模型。
        """
        def build():
            return [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": filler(80_000),
                    "id": f"m{index}",
                }
                for index in range(8)
            ]

        first = ScriptedAdapter(["<summary>摘要</summary>"] * 4)
        ContextPipeline(model_adapter=first).prepare(build(), step=0, model_name=MODEL)
        self.assertGreater(first.calls, 0, "step 0 应当尝试 auto-compact")

        later = ScriptedAdapter(["<summary>摘要</summary>"] * 4)
        ContextPipeline(model_adapter=later).prepare(build(), step=1, model_name=MODEL)
        self.assertEqual(later.calls, 0, "step > 0 不该尝试 auto-compact")

    def test_snip_runs_at_most_once_per_turn(self) -> None:
        """**snip 每回合最多成功一次。**

        它是纯本地删除，但会整段改写转录；每步都重试会让一个回合内的历史
        反复变动，模型看到的上下文前后不一致。
        """
        shared_state = create_context_collapse_state()
        adapter = ScriptedAdapter(["<summary>摘要</summary>"] * 4)
        pipeline = ContextPipeline(model_adapter=adapter, collapse_state=shared_state)

        snaps: list[bool] = []
        pipeline.on_snip = lambda result: snaps.append(result.did_snip)

        # 够大、够多，且满足 snip 的「至少删 6 条」。
        messages = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": filler(40_000),
                "id": f"m{index}",
            }
            for index in range(20)
        ]

        pipeline.prepare(messages, step=0, model_name=MODEL)
        first_round = list(snaps)
        # 同一个回合内再走几步
        pipeline.prepare(messages, step=1, model_name=MODEL)
        pipeline.prepare(messages, step=2, model_name=MODEL)

        self.assertLessEqual(
            sum(1 for _ in first_round), 1, "一个回合内 snip 至多成功一次"
        )
        self.assertLessEqual(sum(1 for _ in snaps), 1, "跨步也不该再次 snip")

    def test_no_model_name_skips_collapse_even_with_a_large_history(self) -> None:
        """**没有模型名时四级压缩全部跳过。**

        算不出窗口就没有阈值，任何压缩都是盲猜。原版与本实现都在入口处
        直接返回。

        上面那条 `test_no_model_name_skips_everything` 用的是单条 200k 字符的
        消息——即使不提前返回，各级也会因为「消息太少」而拒绝，所以它**测不出
        这个提前返回**。这条用「一定会触发各级阈值」的历史，唯一让它们不动的
        原因只能是提前返回。
        """
        shared = create_context_collapse_state()
        adapter = ScriptedAdapter(["<summary>摘要</summary>"] * 4)
        pipeline = ContextPipeline(model_adapter=adapter, collapse_state=shared)

        messages = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": filler(80_000),
                "id": f"m{index}",
            }
            for index in range(10)
        ]

        current, projected = pipeline.prepare(messages, step=0, model_name="")

        self.assertIs(current, messages, "真实转录不该被改动")
        self.assertIs(projected, messages, "投影不该被改动")
        self.assertEqual(adapter.calls, 0, "不该为了压缩去调模型")
        self.assertEqual(shared.spans, [], "不该产生折叠 span")

    def test_auto_compact_clears_the_shared_state_in_place(self) -> None:
        """**auto-compact 之后必须在原地清空，而不是替换引用。**

        上面那条测试用 `model_adapter=None`，因此 `_maybe_auto_compact`
        直接返回 None——**auto-compact 这条分支从未被走到**，替换引用的
        缺陷因此逃过。

        症状本来是隐蔽的：投影时会过滤掉失效 span，所以模型看不到错误内容。
        但 `host` 持有同一个 state 并把**它**写进会话文件，替换引用会让
        会话文件里一直累积指向已不存在消息的 span；而本文件的不变量写着
        「原地更新」——不变量与实际行为不一致，比数据脏更麻烦。

        原版 `replaceContextCollapseState()` 同样在原地同步调用方的对象。
        """
        shared = create_context_collapse_state()
        # 造一个已经折叠过一段的 state，并把它交给管线。
        shared.spans.append(
            CollapseSpan(
                id="span-1",
                start_message_id="m1",
                end_message_id="m2",
                message_ids=["m1", "m2"],
                summary="旧摘要",
                tokens_before=10_000,
                tokens_after=1_000,
                created_at=0,
            )
        )
        shared.consecutive_failures = 2

        pipeline = ContextPipeline(
            model_adapter=ScriptedAdapter(["<summary>压缩后的摘要</summary>"]),
            collapse_state=shared,
        )

        # 造一个足够大的历史，让 warningLevel 达到 blocked（>= 0.95）。
        #
        # 两个门槛都要过，否则这条测试会「因为错误的原因通过」：
        #   ① 利用率要够高（8 × 80000 字符 → util = 1.000）；
        #   ② `compact_conversation` 要求**非 system 消息多于 6 条**
        #      （`len(non_system) <= MIN_KEEP_MESSAGES` 时直接返回 None）。
        # 只满足其中一个时，auto-compact 会被拒，而断言失败看起来像
        # 「状态没清空」——实际是根本没走到那一步。
        messages = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": filler(80_000),
                "id": f"m{index}",
            }
            for index in range(8)
        ]

        pipeline.prepare(messages, step=0, model_name=MODEL)

        # ① 对象还是同一个（宿主能持久化到它）
        self.assertIs(pipeline.collapse_state, shared)
        # ② 数据已被清空（失效 span 不会写进会话文件）
        self.assertEqual(shared.spans, [])
        self.assertEqual(shared.consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
