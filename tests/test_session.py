"""会话持久化的行为测试。

重点在**恢复正确性**：存档、恢复、剪裁回放、压缩边界切分、fork 之后
能否还原出一致的消息序列。以及本实现相对原实现修掉的几处静默数据问题
（docs/divergences.md 的 D11–D15）。

测试用仓库内的临时目录作为 `MINI_CODE_HOME`，不污染真实用户数据。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

# 家目录隔离由 tests/__init__.py 在任何 minicode 导入之前完成。
# 这里只是取回已生效的路径，绝不自己设环境变量（那样在 config 已被导入后无效）。
# 用**动态访问器**而不是模块级常量：后者已删除（见 config.py 的说明）——
# 而且它本来就是这套隔离要防的东西。
from minicode.config import minicode_home  # noqa: E402

_TMP_HOME = str(minicode_home())

from minicode.session import (  # noqa: E402
    MAX_TITLE_LENGTH,
    append_compact_boundary,
    append_context_collapse_span,
    append_snip_boundary,
    cleanup_expired_sessions,
    clear_session,
    extract_title_from_events,
    fork_session,
    list_sessions,
    load_context_collapse_state,
    load_session,
    load_session_with_report,
    load_transcript,
    parse_events,
    project_dir_name,
    rename_session,
    save_session,
    session_file_path,
)

unittest.TestCase.maxDiff = None

CWD = "D:\\CoreCode\\demo"


def user(content: str, ident: str | None = None, internal: str | None = None) -> dict:
    message: dict = {"role": "user", "content": content}
    if ident:
        message["id"] = ident
    if internal:
        message["internal"] = internal
    return message


def assistant(content: str, ident: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if ident:
        message["id"] = ident
    return message


def tool_call(tool_use_id: str, tool_name: str = "read_file", ident: str | None = None) -> dict:
    return {
        "role": "assistant_tool_call",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "input": {"path": "a.txt"},
        "id": ident or f"call-{tool_use_id}",
    }


def tool_result(tool_use_id: str, content: str, is_error: bool = False, ident: str | None = None) -> dict:
    return {
        "role": "tool_result",
        "toolUseId": tool_use_id,
        "toolName": "read_file",
        "content": content,
        "isError": is_error,
        "id": ident or f"res-{tool_use_id}",
    }


class SessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        os.makedirs(_TMP_HOME, exist_ok=True)
        self.session_id = "abc12345"

    def tearDown(self) -> None:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    def save(self, messages: list[dict], cursor: int = 0, session_id: str | None = None) -> int:
        return save_session(CWD, session_id or self.session_id, messages, cursor)

    def load(self, session_id: str | None = None):
        return load_session(CWD, session_id or self.session_id)


# ---------------------------------------------------------------------------
# 路径变换
# ---------------------------------------------------------------------------


class ProjectDirNameTests(unittest.TestCase):
    def test_windows_and_posix_paths(self) -> None:
        cases = {
            "D:\\CoreCode\\MiniCode": "D-CoreCode-MiniCode",
            "C:\\Users\\bob\\proj": "C-Users-bob-proj",
            "D:/CoreCode": "D-CoreCode",
            "/home/bob/proj": "home-bob-proj",
        }
        for raw, expected in cases.items():
            with self.subTest(cwd=raw):
                self.assertEqual(project_dir_name(raw), expected)

    def test_trailing_separator_keeps_a_dash(self) -> None:
        self.assertEqual(project_dir_name("D:\\CoreCode\\"), "D-CoreCode-")

    def test_case_sensitive(self) -> None:
        """大小写敏感：两个大小写不同的路径是不同项目目录。"""
        self.assertNotEqual(project_dir_name("d:\\corecode"), project_dir_name("D:\\CoreCode"))

    def test_collision_is_documented_behaviour(self) -> None:
        """有损变换：`D:\\a-b` 与 `D:\\a\\b` 会串到同一目录（刻意保留）。"""
        self.assertEqual(project_dir_name("D:\\a-b"), project_dir_name("D:\\a\\b"))

    def test_unc_prefix_is_eaten(self) -> None:
        self.assertEqual(project_dir_name("\\\\server\\share\\proj"), "server-share-proj")


class SessionPathTests(SessionTestCase):
    def test_path_traversal_is_rejected(self) -> None:
        """原实现不净化 sessionId，会读写项目目录之外。这里加固为拒绝。"""
        with self.assertRaises(ValueError):
            session_file_path(CWD, "..\\..\\escape")

    def test_normal_id_is_fine(self) -> None:
        path = session_file_path(CWD, "abc12345")
        self.assertTrue(str(path).endswith("abc12345.jsonl"))


# ---------------------------------------------------------------------------
# 保存与去重
# ---------------------------------------------------------------------------


class SaveSessionTests(SessionTestCase):
    def test_first_save_writes_all_non_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            user("hello", "u1"),
            assistant("hi", "a1"),
        ]
        written = self.save(messages)
        self.assertEqual(written, 2)
        self.assertEqual(self.load(), [user("hello", "u1"), assistant("hi", "a1")])

    def test_system_message_is_never_written(self) -> None:
        self.save([{"role": "system", "content": "sys"}, user("x", "u1")])
        events = parse_events(session_file_path(CWD, self.session_id))
        self.assertTrue(all(e.get("type") != "system" for e in events))

    def test_leading_non_system_message_is_not_dropped(self) -> None:
        """本实现对原实现的修正（D14）：原实现盲切下标 0，会吞掉首条消息。"""
        messages = [user("第一条必须保住", "first"), assistant("ok", "a1")]
        self.save(messages)
        loaded = self.load()
        self.assertEqual(len(loaded or []), 2)
        self.assertEqual((loaded or [])[0]["content"], "第一条必须保住")

    def test_dedup_by_id(self) -> None:
        messages = [user("a", "u1"), assistant("b", "a1")]
        self.save(messages)
        written = self.save(messages)  # 再存一次
        self.assertEqual(written, 0, "同一批带 id 的消息不应重复写入")
        self.assertEqual(len(self.load() or []), 2)

    def test_new_messages_are_appended(self) -> None:
        self.save([user("a", "u1")])
        self.save([user("a", "u1"), assistant("b", "a1")])
        self.assertEqual(len(self.load() or []), 2)

    def test_content_change_does_not_rewrite(self) -> None:
        """去重主键是 id，与内容无关：改了内容再存仍被跳过（磁盘保留旧值）。"""
        self.save([user("原始内容", "u1")])
        self.save([user("改过的内容", "u1")])
        self.assertEqual((self.load() or [{}])[0]["content"], "原始内容")

    def test_messages_without_id_use_position_cursor(self) -> None:
        self.save([user("a"), assistant("b")], cursor=0)
        # 前两条已落盘（并被补上 id），游标推进后再存只写增量
        written = self.save([user("a"), assistant("b"), user("c")], cursor=2)
        self.assertEqual(written, 1)
        self.assertEqual(len(self.load() or []), 3)

    def test_cursor_skips_earlier_id_less_messages(self) -> None:
        messages = [user("a"), assistant("b")]
        written = self.save(messages, cursor=2)
        self.assertEqual(written, 0, "游标之前的无 id 消息应被跳过")

    def test_parent_chain_is_linear(self) -> None:
        self.save([user("a", "u1"), assistant("b", "a1")])
        events = parse_events(session_file_path(CWD, self.session_id))
        self.assertIsNone(events[0]["parentUuid"])
        self.assertEqual(events[1]["parentUuid"], events[0]["uuid"])

    def test_message_id_is_written_back_in_place(self) -> None:
        """落盘会原地给消息补 id，调用方之后能靠它去重。"""
        message = user("x")
        self.save([message])
        self.assertIsInstance(message.get("id"), str)
        self.assertTrue(message["id"])

    def test_corrupt_line_is_skipped_not_fatal(self) -> None:
        self.save([user("a", "u1")])
        path = session_file_path(CWD, self.session_id)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON\n")
        self.save([user("a", "u1"), assistant("b", "a1")])
        self.assertEqual(len(self.load() or []), 2)

    def test_missing_trailing_newline_is_repaired(self) -> None:
        """原实现会把新行拼到旧行尾部，制造无法解析的一行。"""
        path = session_file_path(CWD, self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"user","uuid":"x1","message":{"role":"user","content":"旧"}}', encoding="utf-8")
        self.save([user("新", "u2")])
        events = parse_events(path)
        self.assertEqual(len(events), 2, "缺少结尾换行时新行不应被拼接")
        self.assertEqual(len(self.load() or []), 2)


# ---------------------------------------------------------------------------
# 压缩边界
# ---------------------------------------------------------------------------


class CompactBoundaryTests(SessionTestCase):
    def test_boundary_writes_three_parts(self) -> None:
        self.save([user("a", "u1")])
        append_compact_boundary(
            CWD, self.session_id, "摘要正文", "manual", 1000, 100,
            retained_messages=[assistant("保留的", "a9")],
        )
        events = parse_events(session_file_path(CWD, self.session_id))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["user", "compact_boundary", "user", "assistant"])
        self.assertEqual(events[1]["compactMetadata"]["trigger"], "manual")
        self.assertEqual(events[2]["message"]["content"], "摘要正文")

    def test_load_starts_after_last_boundary(self) -> None:
        self.save([user("旧历史", "u1"), assistant("旧回复", "a1")])
        append_compact_boundary(CWD, self.session_id, "摘要", "manual", 10, 1)
        self.save([], 0)
        loaded = self.load()
        # 恢复出的第一条是摘要（伪造的 user 消息），旧历史被边界切掉
        self.assertIsNotNone(loaded)
        self.assertEqual((loaded or [])[0]["content"], "摘要")

    def test_retained_messages_are_not_written_twice_with_same_uuid(self) -> None:
        """本实现对原实现的修正（D11）：不再产生重复 uuid 行。"""
        self.save([user("a", "u1"), assistant("b", "a1")])
        append_compact_boundary(
            CWD, self.session_id, "摘要", "auto", 100, 10,
            retained_messages=[assistant("b", "a1")],
        )
        events = parse_events(session_file_path(CWD, self.session_id))
        uuids = [e["uuid"] for e in events]
        self.assertEqual(len(uuids), len(set(uuids)), "不应出现重复 uuid")
        # 但可追溯性保留：重写行记录了来源
        rewritten = [e for e in events if e.get("rewrittenFrom")]
        self.assertEqual(len(rewritten), 1)
        self.assertEqual(rewritten[0]["rewrittenFrom"], "a1")

    def test_transcript_includes_pre_compact_history(self) -> None:
        self.save([user("压缩前的话", "u1")])
        append_compact_boundary(CWD, self.session_id, "摘要", "manual", 10, 1)
        entries = load_transcript(CWD, self.session_id)
        self.assertIsNotNone(entries)
        bodies = "\n".join(e["body"] for e in entries or [])
        self.assertIn("压缩前的话", bodies)
        self.assertIn("[Context compacted:", bodies)

    def test_transcript_does_not_duplicate_retained_segment(self) -> None:
        """原实现因重复 uuid + 不做边界切分，会让 retained 段重复显示。"""
        append_compact_boundary(
            CWD, self.session_id, "摘要", "auto", 100, 10,
            retained_messages=[assistant("只应出现一次", "a1")],
        )
        entries = load_transcript(CWD, self.session_id) or []
        count = sum(1 for e in entries if e["body"] == "只应出现一次")
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# snip 回放
# ---------------------------------------------------------------------------


class SnipReplayTests(SessionTestCase):
    def _boundary(self, removed_ids: list[str], removed: int = 2) -> dict:
        return {
            "id": "snip-1-" + (removed_ids[0] if removed_ids else "none"),
            "role": "snip_boundary",
            "content": "[Snipped earlier conversation segment]",
            "removedMessageIds": list(removed_ids),
            "removedCount": removed,
            "tokensFreed": 500,
            "timestamp": 1,
        }

    def test_boundary_moves_back_to_its_position(self) -> None:
        self.save([user("被删的", "u1"), assistant("被删的2", "a1")])
        append_snip_boundary(CWD, self.session_id, self._boundary(["u1", "a1"]))
        self.save([], 0)
        # snip 之后写下的消息排在文件尾部
        save_session(CWD, self.session_id, [assistant("snip 之后的", "a2")])

        loaded = self.load() or []
        contents = [m.get("content") for m in loaded]
        self.assertEqual(contents, ["[Snipped earlier conversation segment]", "snip 之后的"])

    def test_unmatched_boundary_is_reported_not_silently_lost(self) -> None:
        """本实现新增可观测性（D9/D12）：丢失的边界会被报出来。"""
        self.save([assistant("现存消息", "a1")])
        append_snip_boundary(CWD, self.session_id, self._boundary(["nonexistent-1", "nonexistent-2"]))

        loaded, dropped = load_session_with_report(CWD, self.session_id)
        self.assertEqual(len(dropped), 1, "匹配不上的边界应被报告")
        self.assertNotIn(
            "snip_boundary", [m.get("role") for m in (loaded or [])]
        )

    def test_synthetic_ids_are_the_cause_of_loss(self) -> None:
        """说明为什么会丢：合成 id 与磁盘 uuid 永不相等。"""
        boundary = self._boundary(["message-0", "message-1"])
        self.save([assistant("现存", "a1")])
        append_snip_boundary(CWD, self.session_id, boundary)
        _, dropped = load_session_with_report(CWD, self.session_id)
        self.assertEqual(len(dropped), 1, "合成 id 无法匹配磁盘 uuid，边界必然丢失")


# ---------------------------------------------------------------------------
# 折叠跨度 / 列表 / 标题
# ---------------------------------------------------------------------------


class CollapsePersistenceTests(SessionTestCase):
    def test_committed_span_round_trips(self) -> None:
        span = {
            "id": "collapse-1-0-u1",
            "startMessageId": "u1",
            "endMessageId": "a1",
            "messageIds": ["u1", "a1"],
            "summary": "折叠摘要",
            "tokensBefore": 900,
            "tokensAfter": 100,
            "status": "committed",
            "createdAt": 1,
            "reason": "manual",
        }
        append_context_collapse_span(CWD, self.session_id, span)
        state = load_context_collapse_state(CWD, self.session_id)
        self.assertIsNotNone(state)
        self.assertEqual(len(state.spans), 1)
        self.assertEqual(state.spans[0].summary, "折叠摘要")

    def test_staged_span_is_not_restored(self) -> None:
        append_context_collapse_span(
            CWD, self.session_id,
            {"id": "s", "messageIds": ["u1"], "summary": "x", "status": "staged"},
        )
        self.assertIsNone(load_context_collapse_state(CWD, self.session_id))

    def test_enabled_and_failure_count_are_not_persisted(self) -> None:
        append_context_collapse_span(
            CWD, self.session_id,
            {"id": "s", "messageIds": ["u1"], "summary": "x", "status": "committed"},
        )
        state = load_context_collapse_state(CWD, self.session_id)
        self.assertTrue(state.enabled)
        self.assertEqual(state.consecutive_failures, 0)


class TitleTests(SessionTestCase):
    def test_first_user_message_is_the_title(self) -> None:
        self.save([user("帮我重构这个模块", "u1"), assistant("好", "a1")])
        metas = list_sessions(CWD)
        self.assertEqual(metas[0].title, "帮我重构这个模块")

    def test_internal_user_messages_do_not_become_title(self) -> None:
        self.save([user("goal 续跑", "u1", internal="goal"), user("真实的第一句", "u2")])
        metas = list_sessions(CWD)
        self.assertEqual(metas[0].title, "真实的第一句")

    def test_long_title_is_truncated_by_utf16_units(self) -> None:
        long_text = "字" * (MAX_TITLE_LENGTH + 20)
        self.save([user(long_text, "u1")])
        title = list_sessions(CWD)[0].title or ""
        self.assertEqual(title[-3:], "...")
        self.assertEqual(len(title), MAX_TITLE_LENGTH + 3)

    def test_rename_wins_over_automatic_title(self) -> None:
        self.save([user("原始", "u1")])
        self.assertTrue(rename_session(CWD, self.session_id, "改过的名字"))
        self.assertEqual(list_sessions(CWD)[0].title, "改过的名字")

    def test_last_rename_wins(self) -> None:
        self.save([user("原始", "u1")])
        rename_session(CWD, self.session_id, "第一次")
        rename_session(CWD, self.session_id, "第二次")
        self.assertEqual(list_sessions(CWD)[0].title, "第二次")

    def test_rename_on_missing_session_returns_false(self) -> None:
        self.assertFalse(rename_session(CWD, "nope", "x"))

    def test_message_count_is_event_line_count(self) -> None:
        """与原实现一致：messageCount 是 JSONL 行数，不是消息条数。"""
        self.save([user("a", "u1"), assistant("b", "a1")])
        rename_session(CWD, self.session_id, "t")
        self.assertEqual(list_sessions(CWD)[0].message_count, 3)


# ---------------------------------------------------------------------------
# fork / clear / cleanup
# ---------------------------------------------------------------------------


class ForkTests(SessionTestCase):
    def test_fork_copies_messages(self) -> None:
        self.save([user("原始提问", "u1"), assistant("原始回答", "a1")])
        new_id = fork_session(CWD, self.session_id)
        self.assertIsNotNone(new_id)
        loaded = load_session(CWD, new_id or "")
        contents = [m.get("content") for m in (loaded or [])]
        self.assertEqual(contents, ["原始提问", "原始回答"])

    def test_fork_title_gets_suffix(self) -> None:
        self.save([user("原始提问", "u1")])
        new_id = fork_session(CWD, self.session_id) or ""
        metas = {m.id: m.title for m in list_sessions(CWD)}
        self.assertEqual(metas[new_id], "原始提问_fork1")

    def test_second_fork_increments(self) -> None:
        self.save([user("t", "u1")])
        first = fork_session(CWD, self.session_id) or ""
        fork_session(CWD, first)
        titles = sorted(m.title or "" for m in list_sessions(CWD))
        self.assertIn("t_fork1", titles)
        self.assertIn("t_fork1_fork1", titles)

    def test_fork_preserves_snip_boundary(self) -> None:
        """本实现对原实现的修正（D15）：原实现 fork 后 snip 边界必然丢失。"""
        self.save([user("被剪的", "u1"), assistant("被剪的2", "a1")])
        boundary = {
            "id": "snip-1-u1",
            "role": "snip_boundary",
            "content": "[Snipped earlier conversation segment]",
            "removedMessageIds": ["u1", "a1"],
            "removedCount": 2,
            "tokensFreed": 300,
            "timestamp": 1,
        }
        append_snip_boundary(CWD, self.session_id, boundary)
        save_session(CWD, self.session_id, [assistant("之后", "a2")])

        new_id = fork_session(CWD, self.session_id) or ""
        # 关键：新会话里不应报告「丢失的边界」
        _, dropped = load_session_with_report(CWD, new_id)
        self.assertEqual(dropped, [], "fork 后不应丢失 snip 边界")

    def test_fork_of_missing_session_returns_none(self) -> None:
        self.assertIsNone(fork_session(CWD, "nope"))


class ClearSessionTests(SessionTestCase):
    def test_clear_removes_the_file(self) -> None:
        self.save([user("a", "u1")])
        self.assertTrue(clear_session(CWD, self.session_id))
        self.assertIsNone(self.load())

    def test_on_cleared_callback_purges_memory(self) -> None:
        """本实现对原实现的修正（D13）：删除必须同时清内存，否则会被复活。"""
        self.save([user("a", "u1")])
        state = {"messages": [user("a", "u1")], "cursor": 1}

        def purge() -> None:
            state["messages"] = []
            state["cursor"] = 0

        clear_session(CWD, self.session_id, on_cleared=purge)
        self.assertEqual(state["messages"], [])
        # 之后照常保存，不应把旧历史写回去
        save_session(CWD, self.session_id, state["messages"], state["cursor"])
        self.assertIsNone(self.load(), "清空后不应残留会话文件")

    def test_resurrection_when_memory_is_not_purged(self) -> None:
        """反证：不传回调时，旧内存会被下一次保存写回（原实现的行为）。"""
        messages = [user("a", "u1"), assistant("b", "a1")]
        self.save(messages)
        clear_session(CWD, self.session_id)
        save_session(CWD, self.session_id, messages, 0)
        self.assertIsNotNone(self.load(), "这正是 D13 要修的问题")

    def test_clear_missing_session_returns_false(self) -> None:
        self.assertFalse(clear_session(CWD, "nope"))


class CleanupTests(SessionTestCase):
    def test_expired_sessions_are_removed(self) -> None:
        self.save([user("a", "u1")])
        path = session_file_path(CWD, self.session_id)
        old = 1_600_000_000.0  # 2020 年
        os.utime(path, (old, old))
        removed = cleanup_expired_sessions(CWD, 1000)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.load())

    def test_fresh_sessions_survive(self) -> None:
        self.save([user("a", "u1")])
        removed = cleanup_expired_sessions(CWD, 30 * 24 * 3600 * 1000)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(self.load())


# ---------------------------------------------------------------------------
# transcript 渲染
# ---------------------------------------------------------------------------


class TranscriptTests(SessionTestCase):
    def test_tool_call_and_result_are_paired_in_place(self) -> None:
        self.save([
            tool_call("t1"),
            tool_result("t1", "文件内容"),
        ])
        entries = load_transcript(CWD, self.session_id) or []
        tools = [e for e in entries if e["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "success")
        self.assertEqual(tools[0]["body"], "文件内容")

    def test_unpaired_tool_call_stays_a_placeholder(self) -> None:
        self.save([tool_call("t1")])
        entries = load_transcript(CWD, self.session_id) or []
        tools = [e for e in entries if e["kind"] == "tool"]
        self.assertEqual(tools[0]["status"], "error")
        self.assertIn("No saved result", tools[0]["body"])

    def test_failed_tool_result_marks_error(self) -> None:
        self.save([tool_call("t1"), tool_result("t1", "boom", is_error=True)])
        tools = [e for e in (load_transcript(CWD, self.session_id) or []) if e["kind"] == "tool"]
        self.assertEqual(tools[0]["status"], "error")

    def test_orphan_tool_result_is_dropped(self) -> None:
        self.save([tool_result("orphan", "无人认领")])
        entries = load_transcript(CWD, self.session_id) or []
        self.assertEqual([e for e in entries if e["kind"] == "tool"], [])

    def test_internal_user_message_is_not_rendered(self) -> None:
        self.save([user("goal 续跑", "u1", internal="goal"), user("真实输入", "u2")])
        bodies = [e["body"] for e in (load_transcript(CWD, self.session_id) or [])]
        self.assertNotIn("goal 续跑", bodies)
        self.assertIn("真实输入", bodies)

    def test_summary_entry_uses_compressed_count(self) -> None:
        self.save([{"role": "context_summary", "content": "摘要", "compressedCount": 7, "id": "s1"}])
        bodies = [e["body"] for e in (load_transcript(CWD, self.session_id) or [])]
        self.assertTrue(any("7 messages compressed" in b for b in bodies))

    def test_missing_session_returns_none(self) -> None:
        self.assertIsNone(load_transcript(CWD, "nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
