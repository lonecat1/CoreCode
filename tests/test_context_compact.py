"""microcompact 与 snip compact 的行为测试。

这两个模块都会**改写真实转录**，所以测试的重点是护栏：
不该删的一个都不能删，不该清的一个都不能清，失败时必须完全不动转录。

测试数据用 `push_over()` 按**目标利用率**反算所需字符数，而不是硬编码长度——
硬编码会让测试在阈值或窗口表变动时静默失去意义（变成「没触发所以通过」）。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from corecode.context.constants import CLEAR_MARKER
from corecode.context.microcompact import microcompact
from corecode.context.snip import (
    build_message_groups,
    build_snip_boundary_content,
    find_candidate_range,
    find_safe_runs,
    is_protected_tool_name,
    snip_compact_conversation,
)
from corecode.context.tokens import compute_context_stats

MODEL = "claude-sonnet-4"                       # 窗口 200000，有效输入 184000
WINDOW = 184_000
MICRO_THRESHOLD = 0.50
SNIP_THRESHOLD = 0.70
#: user 角色的字符/token 比率。用来反算「需要多少字符才能越过阈值」。
USER_CHARS_PER_TOKEN = 3.0

#: 断言里可能出现巨大的消息内容，关掉 diff 以免淹没输出。
unittest.TestCase.maxDiff = None


def filler(chars: int) -> str:
    return "x" * chars


def chars_needed(ratio: float) -> int:
    """算出总字符数，使 user 内容恰好越过 `ratio` 利用率。"""
    target_tokens = ratio * WINDOW * 1.15      # 留 15% 余量，避免贴边
    return int(target_tokens * USER_CHARS_PER_TOKEN)


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


def tool_result(tool_name: str, content: str, tool_use_id: str = "t", is_error: bool = False) -> dict:
    return {
        "role": "tool_result",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "content": content,
        "isError": is_error,
    }


def tool_call(tool_name: str, tool_use_id: str = "t") -> dict:
    return {
        "role": "assistant_tool_call",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "input": {},
    }


def pressured_microcompact(results: list[dict]) -> list[dict]:
    """构造越过 microcompact 阈值的序列：一条大 user + 若干工具结果。"""
    return [
        {"role": "system", "content": "sys"},
        user(filler(chars_needed(MICRO_THRESHOLD)), "u0"),
        *results,
    ]


def pressured_snip(tail_extra: list[dict] | None = None) -> list[dict]:
    """构造越过 snip 阈值的**可剪裁**序列。

    关键点：利用率必须由**中段的大量中等消息**提供，而不是一两条巨型消息。
    否则候选区间里没有足够多的可删消息，snip 会因为区间太小而放弃。
    """
    total_chars = chars_needed(SNIP_THRESHOLD)
    middle_count = 40
    per_message = total_chars // middle_count

    messages: list[dict] = [
        {"role": "system", "content": "you are corecode"},
        assistant("开始工作", "a-start"),
    ]
    # 中段：可被剪裁的普通问答
    for index in range(middle_count):
        messages.append(assistant(filler(per_message), f"a{index}"))
        messages.append(user(filler(per_message), f"u{index}"))
    # 尾部保留需的「最后一条 user」
    messages.extend(tail_extra or [])
    messages.append(user("请继续", "last-user"))
    return messages


def assert_over(test: unittest.TestCase, messages: list[dict], threshold: float) -> None:
    stats = compute_context_stats(messages, MODEL)
    test.assertGreaterEqual(
        stats.utilization,
        threshold,
        f"测试数据未越过阈值 {threshold}（实测 {stats.utilization:.3f}），"
        "后续断言将失去意义",
    )


class MicrocompactTests(unittest.TestCase):
    def test_below_threshold_returns_same_object(self) -> None:
        messages = [user("hi"), tool_result("read_file", "data")]
        self.assertIs(microcompact(messages, MODEL), messages)

    def test_clears_all_but_the_last_three_compactable_results(self) -> None:
        results = [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(5)
        ]
        messages = pressured_microcompact(results)
        assert_over(self, messages, MICRO_THRESHOLD)

        result = microcompact(messages, MODEL)

        self.assertIsNot(result, messages, "越过阈值后应当返回新列表")
        # 前两条（最早的）被清空
        self.assertEqual(result[2]["content"], CLEAR_MARKER)
        self.assertEqual(result[3]["content"], CLEAR_MARKER)
        # 后三条保留
        for index in (4, 5, 6):
            self.assertNotEqual(result[index]["content"], CLEAR_MARKER)

    def test_exactly_three_candidates_is_left_alone(self) -> None:
        results = [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(3)
        ]
        messages = pressured_microcompact(results)
        assert_over(self, messages, MICRO_THRESHOLD)

        self.assertIs(
            microcompact(messages, MODEL), messages, "恰好 3 条候选时不应处理"
        )

    def test_edit_tools_are_never_cleared(self) -> None:
        """编辑类工具的输出是「改过什么」的证据，清掉会导致重复改动。"""
        results = [
            tool_result("write_file", filler(2000), "w1"),
            tool_result("edit_file", filler(2000), "e1"),
            tool_result("patch_file", filler(2000), "p1"),
            tool_result("read_file", filler(2000), "r1"),
            tool_result("read_file", filler(2000), "r2"),
            tool_result("read_file", filler(2000), "r3"),
            tool_result("read_file", filler(2000), "r4"),
        ]
        messages = pressured_microcompact(results)
        assert_over(self, messages, MICRO_THRESHOLD)

        result = microcompact(messages, MODEL)
        # 白名单里只有 4 条 read_file，清掉最早那条；编辑类三条不动。
        for index in (2, 3, 4):
            self.assertNotEqual(
                result[index]["content"], CLEAR_MARKER, "编辑类工具输出不应被清空"
            )
        self.assertEqual(result[5]["content"], CLEAR_MARKER, "最早的 read_file 应被清空")

    def test_preserves_all_other_fields(self) -> None:
        results = [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(4)
        ]
        messages = pressured_microcompact(results)
        result = microcompact(messages, MODEL)
        cleared = result[2]
        self.assertEqual(cleared["toolUseId"], "t0")
        self.assertEqual(cleared["toolName"], "read_file")
        self.assertIn("isError", cleared)
        self.assertEqual(result[2]["content"], CLEAR_MARKER)

    def test_error_results_are_also_cleared(self) -> None:
        results = [
            tool_result("run_command", filler(2000), "e1", is_error=True),
            *[tool_result("read_file", filler(2000), f"t{i}") for i in range(3)],
        ]
        messages = pressured_microcompact(results)
        assert_over(self, messages, MICRO_THRESHOLD)

        result = microcompact(messages, MODEL)
        self.assertEqual(result[2]["content"], CLEAR_MARKER)
        self.assertTrue(result[2]["isError"])

    def test_message_count_is_unchanged(self) -> None:
        results = [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(5)
        ]
        messages = pressured_microcompact(results)
        result = microcompact(messages, MODEL)
        self.assertEqual(len(result), len(messages))

    def test_does_not_mutate_the_input_list(self) -> None:
        results = [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(5)
        ]
        messages = pressured_microcompact(results)
        original = messages[2]["content"]
        microcompact(messages, MODEL)
        self.assertEqual(messages[2]["content"], original, "不得就地修改传入列表")

    def test_already_cleared_is_idempotent(self) -> None:
        results = [tool_result("read_file", CLEAR_MARKER, "t0")] + [
            tool_result("read_file", filler(2000), f"t{i}") for i in range(1, 4)
        ]
        messages = pressured_microcompact(results)
        # 只有 3 条候选（其中 1 条已清空）→ 不超过保留数，原样返回
        self.assertIs(microcompact(messages, MODEL), messages)


class ProtectedToolNameTests(unittest.TestCase):
    def test_exact_names(self) -> None:
        for name in ("write_file", "edit_file", "patch_file", "modify_file", "apply_patch"):
            self.assertTrue(is_protected_tool_name(name), name)

    def test_substring_matching_is_intentional(self) -> None:
        """宁可多保护不可误删；子串命中的副作用是有意保留的。"""
        self.assertTrue(is_protected_tool_name("overwrite_file"))
        self.assertTrue(is_protected_tool_name("patch_anything"))
        self.assertTrue(is_protected_tool_name("credit_report"))  # 含 'edit'
        self.assertTrue(is_protected_tool_name("MODIFY_THING"))

    def test_ordinary_tools_are_not_protected(self) -> None:
        for name in ("read_file", "list_files", "grep_files", "run_command", "web_fetch"):
            self.assertFalse(is_protected_tool_name(name), name)


class GroupingTests(unittest.TestCase):
    def test_paired_call_and_result_form_one_group(self) -> None:
        messages = [tool_call("read_file", "x1"), tool_result("read_file", "d", "x1")]
        groups = build_message_groups(messages)
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].start, groups[0].end), (0, 2))
        self.assertFalse(groups[0].protected)

    def test_unpaired_call_is_protected(self) -> None:
        messages = [tool_call("read_file", "x1"), user("next")]
        groups = build_message_groups(messages)
        self.assertTrue(groups[0].protected)
        self.assertIn("unclosed_tool_call", groups[0].reasons)

    def test_orphan_result_is_protected(self) -> None:
        messages = [tool_result("read_file", "d", "x1")]
        groups = build_message_groups(messages)
        self.assertTrue(groups[0].protected)
        self.assertIn("orphan_tool_result", groups[0].reasons)

    def test_mismatched_ids_do_not_pair(self) -> None:
        messages = [tool_call("read_file", "x1"), tool_result("read_file", "d", "x2")]
        groups = build_message_groups(messages)
        self.assertTrue(groups[0].protected)
        self.assertIn("unclosed_tool_call", groups[0].reasons)
        self.assertTrue(groups[1].protected)
        self.assertIn("orphan_tool_result", groups[1].reasons)

    def test_parallel_batch_is_fully_protected(self) -> None:
        """并行批次（先连续 call 再连续 result）在原语义下整批受保护。"""
        messages = [
            tool_call("read_file", "a"),
            tool_call("read_file", "b"),
            tool_result("read_file", "1", "a"),
            tool_result("read_file", "2", "b"),
        ]
        groups = build_message_groups(messages)
        self.assertTrue(all(group.protected for group in groups))


class CandidateRangeTests(unittest.TestCase):
    def test_too_few_messages(self) -> None:
        messages = [user("a") for _ in range(10)]
        _, _, reason = find_candidate_range(messages)
        self.assertEqual(reason, "too_few_messages")

    def test_start_moves_past_boundary_messages(self) -> None:
        messages = [{"role": "system", "content": "s"}] + [
            user(f"m{i}") for i in range(25)
        ]
        start, end, reason = find_candidate_range(messages)
        self.assertIsNone(reason)
        self.assertGreaterEqual(start, 1, "起点必须越过 system 消息")
        self.assertGreater(end - start, 0)

    def test_tail_including_last_user_is_never_cut(self) -> None:
        messages = [user(f"m{i}") for i in range(30)]
        start, end, reason = find_candidate_range(messages)
        self.assertIsNone(reason)
        self.assertLessEqual(end, 29, "end 不得越过最后一条 user")

    def test_existing_summary_is_a_boundary(self) -> None:
        messages = [user(f"m{i}") for i in range(25)]
        messages.insert(3, {"role": "context_summary", "content": "旧摘要"})
        start, _, reason = find_candidate_range(messages)
        self.assertIsNone(reason)
        self.assertGreaterEqual(start, 4, "起点必须越过既有摘要")


class SafeRunTests(unittest.TestCase):
    def _groups(self, protected_flags: list[bool]):
        messages = [user(f"m{i}") for i in range(len(protected_flags))]
        groups = build_message_groups(messages)
        for group, flag in zip(groups, protected_flags):
            group.protected = flag
        return groups

    def test_consecutive_unprotected_merge_into_one_run(self) -> None:
        groups = self._groups([False, False, True, False, False, False])
        runs = find_safe_runs(groups)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].messages_count, 2)
        self.assertEqual(runs[1].messages_count, 3)

    def test_all_protected_yields_no_runs(self) -> None:
        self.assertEqual(find_safe_runs(self._groups([True, True])), [])


class SnipTests(unittest.TestCase):
    def test_below_threshold_returns_same_object(self) -> None:
        messages = [user("hi"), assistant("x"), user("y")]
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        self.assertFalse(result.did_snip)
        self.assertIs(result.messages, messages)
        self.assertEqual(result.reason, "below_threshold")

    def test_successful_snip_removes_middle_and_inserts_boundary(self) -> None:
        messages = pressured_snip()
        assert_over(self, messages, SNIP_THRESHOLD)

        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)

        self.assertTrue(result.did_snip, f"未能剪裁，原因：{result.reason}")
        self.assertLess(len(result.messages), len(messages))
        self.assertIsNot(result.messages, messages, "成功时必须返回新列表")

        boundary = result.boundary_message
        self.assertIsNotNone(boundary)
        self.assertEqual(boundary["role"], "snip_boundary")
        self.assertIn("[Snipped earlier conversation segment]", boundary["content"])
        self.assertEqual(boundary["removedCount"], len(result.removed_message_ids))
        self.assertGreater(result.tokens_freed, 0)

    def test_system_message_survives(self) -> None:
        messages = pressured_snip()
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        self.assertEqual(result.messages[0]["role"], "system")

    def test_last_user_message_survives(self) -> None:
        """最后一条 user 及其之后的内容永远不可删。"""
        messages = pressured_snip()
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        self.assertEqual(result.messages[-1]["content"], "请继续")

    def test_file_edit_output_is_protected(self) -> None:
        """编辑组的输出不得被删除。"""
        marker = "UNIQUE_EDIT_OUTPUT_MARKER"
        messages = pressured_snip(
            tail_extra=[tool_call("write_file", "w1"), tool_result("write_file", marker, "w1")]
        )
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        if result.did_snip:
            self.assertTrue(
                any(m.get("content") == marker for m in result.messages),
                "文件编辑组的输出必须保留",
            )

    def test_error_output_is_protected(self) -> None:
        error_text = "Traceback: permission denied while writing"
        messages = pressured_snip(
            tail_extra=[
                tool_call("read_file", "e1"),
                tool_result("read_file", error_text, "e1", is_error=True),
            ]
        )
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        if result.did_snip:
            self.assertTrue(
                any(m.get("content") == error_text for m in result.messages),
                "报错工具结果必须保留",
            )

    def test_snip_marks_retained_usage_stale(self) -> None:
        """压缩改动了历史，被保留消息上的旧 usage 必须失效。"""
        usage = {
            "inputTokens": 100,
            "outputTokens": 10,
            "totalTokens": 110,
            "source": "test",
        }
        messages = pressured_snip(
            tail_extra=[assistant("带用量的消息", "with-usage", usage=usage)]
        )
        stats = compute_context_stats(messages, MODEL)
        result = snip_compact_conversation(messages, stats, WINDOW)
        if result.did_snip:
            carried = [m for m in result.messages if m.get("id") == "with-usage"]
            self.assertEqual(len(carried), 1)
            self.assertTrue(carried[0].get("usageStale"), "被保留的 usage 必须标记失效")

    def test_boundary_content_is_deterministic(self) -> None:
        text = build_snip_boundary_content(7, 1234)
        self.assertIn("- messages: 7", text)
        self.assertIn("- approximate tokens freed: 1234", text)

    def test_boundary_content_rounds_half_up(self) -> None:
        """50.5 必须渲染成 51（JS 语义），而不是 Python 的 50。"""
        self.assertIn("51", build_snip_boundary_content(1, 50.5))

    def test_no_snip_never_modifies_the_transcript(self) -> None:
        messages = [user("a"), assistant("b"), user("c")]
        stats = compute_context_stats(messages, MODEL)
        before = [dict(m) for m in messages]
        result = snip_compact_conversation(messages, stats, WINDOW)
        self.assertIs(result.messages, messages)
        self.assertEqual(messages, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
