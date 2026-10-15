"""输入历史（`history.ts`）的测试。

对应原版 `MiniCode/src/history.ts`（71 行）。

## 三条值得钉住的性质

**① 只追加不重复。** 原版先读回已有条目、过滤掉出现过的，没有新条目就
**直接返回不碰文件**。少了这个判断，每次退出都会把整份历史再写一遍，
文件成倍增长——而且增长是静默的。

**② 坏行跳过，不是整体失败。** 历史是追加写的，一次断电就可能留下半行。
让整个文件读不出来，代价是用户丢掉全部历史，而坏行只有一行。

**③ 超过上限时保留最近的。** 「刚才敲过什么」比「上个月敲过什么」有用。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.history import (  # noqa: E402
    MAX_ENTRIES,
    HistoryEntry,
    append_history,
    clear_history,
    load_history,
    load_history_entries,
    save_history_entries,
)


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="minicode-history-"))
        self.path = self.root / "history.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [
            line
            for line in self.path.read_text(encoding="utf-8").strip().split("\n")
            if line
        ]


class LoadTests(HistoryTestCase):
    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(load_history(self.path), [])
        self.assertEqual(load_history_entries(self.path), [])

    def test_reads_display_text(self) -> None:
        append_history("hello", "/proj", "s1", self.path)
        self.assertEqual(load_history(self.path), ["hello"])

    def test_preserves_order(self) -> None:
        for text in ("a", "b", "c"):
            append_history(text, "/proj", "s1", self.path)
        self.assertEqual(load_history(self.path), ["a", "b", "c"])

    def test_entries_carry_metadata(self) -> None:
        append_history("hello", "/proj", "sess-1", self.path)
        entry = load_history_entries(self.path)[0]
        self.assertEqual(entry.display, "hello")
        self.assertEqual(entry.project, "/proj")
        self.assertEqual(entry.session_id, "sess-1")
        self.assertGreater(entry.timestamp, 0)

    def test_malformed_line_is_skipped_not_fatal(self) -> None:
        """**坏行跳过。** 一行损坏不该让用户丢掉全部历史。"""
        self.path.write_text(
            "\n".join(
                [
                    json.dumps({"display": "good1", "timestamp": 1}),
                    "{ 这不是 JSON",
                    json.dumps({"display": "good2", "timestamp": 2}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(load_history(self.path), ["good1", "good2"])

    def test_entry_without_display_is_skipped(self) -> None:
        self.path.write_text(
            json.dumps({"timestamp": 1}) + "\n", encoding="utf-8"
        )
        self.assertEqual(load_history(self.path), [])

    def test_non_dict_json_is_skipped(self) -> None:
        self.path.write_text("[1,2,3]\n\"text\"\n", encoding="utf-8")
        self.assertEqual(load_history(self.path), [])

    def test_blank_lines_are_ignored(self) -> None:
        self.path.write_text("\n\n\n", encoding="utf-8")
        self.assertEqual(load_history(self.path), [])

    def test_missing_timestamp_becomes_zero(self) -> None:
        self.path.write_text(
            json.dumps({"display": "x"}) + "\n", encoding="utf-8"
        )
        self.assertEqual(load_history_entries(self.path)[0].timestamp, 0.0)

    def test_boolean_timestamp_is_not_a_number(self) -> None:
        """`True` 是 `int` 的子类，但它不是时间戳。"""
        self.path.write_text(
            json.dumps({"display": "x", "timestamp": True}) + "\n", encoding="utf-8"
        )
        self.assertEqual(load_history_entries(self.path)[0].timestamp, 0.0)

    def test_unicode_round_trips(self) -> None:
        append_history("帮我看看这个项目", "/proj", "s1", self.path)
        self.assertEqual(load_history(self.path), ["帮我看看这个项目"])


class SaveTests(HistoryTestCase):
    def test_appends_new_entries(self) -> None:
        count = save_history_entries(["a", "b"], "/proj", "s1", self.path)
        self.assertEqual(count, 2)
        self.assertEqual(load_history(self.path), ["a", "b"])

    def test_duplicate_entries_are_not_appended(self) -> None:
        """**核心性质**：已经出现过的条目不再追加。"""
        save_history_entries(["a"], "/proj", "s1", self.path)
        count = save_history_entries(["a"], "/proj", "s1", self.path)
        self.assertEqual(count, 0)
        self.assertEqual(load_history(self.path), ["a"])

    def test_no_new_entries_does_not_touch_the_file(self) -> None:
        """**没有新条目时必须完全不碰文件。**

        原版 `if (newEntries.length === 0) return`。少了它，每次退出都会把
        整份历史再写一遍，文件成倍增长——而且增长是静默的。
        """
        save_history_entries(["a"], "/proj", "s1", self.path)
        before = self.path.read_bytes()
        mtime_before = self.path.stat().st_mtime_ns
        save_history_entries(["a"], "/proj", "s1", self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime_before)

    def test_duplicates_within_one_batch_are_collapsed(self) -> None:
        count = save_history_entries(["a", "a", "b"], "/proj", "s1", self.path)
        self.assertEqual(count, 2)
        self.assertEqual(load_history(self.path), ["a", "b"])

    def test_only_new_entries_are_appended(self) -> None:
        save_history_entries(["a"], "/proj", "s1", self.path)
        count = save_history_entries(["a", "b", "c"], "/proj", "s1", self.path)
        self.assertEqual(count, 2)
        self.assertEqual(load_history(self.path), ["a", "b", "c"])

    def test_empty_strings_are_skipped(self) -> None:
        count = save_history_entries(["", "a", ""], "/proj", "s1", self.path)
        self.assertEqual(count, 1)
        self.assertEqual(load_history(self.path), ["a"])

    def test_creates_parent_directory(self) -> None:
        nested = self.root / "deep" / "nested" / "history.jsonl"
        save_history_entries(["a"], "/proj", "s1", nested)
        self.assertTrue(nested.exists())

    def test_returns_zero_for_empty_input(self) -> None:
        self.assertEqual(save_history_entries([], "/proj", "s1", self.path), 0)
        self.assertFalse(self.path.exists(), "没有条目时不该创建文件")

    def test_each_line_is_valid_json(self) -> None:
        save_history_entries(["a", "b"], "/proj", "s1", self.path)
        for line in self.lines():
            json.loads(line)  # 不抛即通过

    def test_stored_keys_match_the_original(self) -> None:
        """字段名与原版一致（`display` / `timestamp` / `project` / `sessionId`）。

        用驼峰 `sessionId` 是刻意的：它与原版的 JSON 结构逐字段对应，
        便于用同一个文件在两种实现之间切换。
        """
        save_history_entries(["a"], "/proj", "s1", self.path)
        record = json.loads(self.lines()[0])
        self.assertEqual(
            sorted(record), ["display", "project", "sessionId", "timestamp"]
        )

    def test_explicit_timestamp_is_used(self) -> None:
        save_history_entries(["a"], "/proj", "s1", self.path, now=1234.5)
        self.assertEqual(load_history_entries(self.path)[0].timestamp, 1234.5)


class TrimTests(HistoryTestCase):
    def test_trims_to_max_entries(self) -> None:
        save_history_entries(
            [f"entry-{i}" for i in range(MAX_ENTRIES + 50)], "/proj", "s1", self.path
        )
        self.assertEqual(len(self.lines()), MAX_ENTRIES)

    def test_keeps_the_most_recent(self) -> None:
        """**保留最近的**——「刚才敲过什么」比「上个月敲过什么」有用。"""
        entries = [f"entry-{i}" for i in range(MAX_ENTRIES + 10)]
        save_history_entries(entries, "/proj", "s1", self.path)
        loaded = load_history(self.path)
        self.assertEqual(loaded[-1], entries[-1])
        self.assertEqual(loaded[0], entries[-MAX_ENTRIES])

    def test_under_the_limit_is_untouched(self) -> None:
        save_history_entries(["a", "b"], "/proj", "s1", self.path)
        self.assertEqual(len(self.lines()), 2)

    def test_trim_happens_across_separate_calls(self) -> None:
        """分多次追加时也要截断，而不是只在单次大批量时。

        刻意把上限调小：按真实上限（500）逐条追加 505 次会让这条测试跑几秒，
        而它验证的性质与上限大小无关。测试**为了覆盖行为**而调参，不该为了
        真实感牺牲整个套件的速度。
        """
        from unittest.mock import patch

        from minicode import history as module

        with patch.object(module, "MAX_ENTRIES", 5):
            for index in range(8):
                append_history(f"e{index}", "/proj", "s1", self.path)
        self.assertEqual(len(self.lines()), 5)
        self.assertEqual(load_history(self.path), ["e3", "e4", "e5", "e6", "e7"])

    def test_trim_keeps_the_most_recent_when_called_repeatedly(self) -> None:
        from unittest.mock import patch

        from minicode import history as module

        with patch.object(module, "MAX_ENTRIES", 3):
            for index in range(6):
                append_history(f"e{index}", "/proj", "s1", self.path)
        self.assertEqual(load_history(self.path), ["e3", "e4", "e5"])


class AppendHistoryTests(HistoryTestCase):
    def test_appends_one_entry(self) -> None:
        self.assertEqual(append_history("hi", "/proj", "s1", self.path), 1)
        self.assertEqual(load_history(self.path), ["hi"])

    def test_second_identical_append_is_a_noop(self) -> None:
        append_history("hi", "/proj", "s1", self.path)
        self.assertEqual(append_history("hi", "/proj", "s1", self.path), 0)
        self.assertEqual(load_history(self.path), ["hi"])


class ClearTests(HistoryTestCase):
    def test_removes_the_file(self) -> None:
        append_history("a", "/proj", "s1", self.path)
        clear_history(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(load_history(self.path), [])

    def test_missing_file_is_not_an_error(self) -> None:
        clear_history(self.path)  # 不应抛


class HistoryEntryTests(unittest.TestCase):
    def test_to_json_uses_camel_case_session_id(self) -> None:
        entry = HistoryEntry("x", 1.0, "/p", "s")
        self.assertEqual(
            entry.to_json(),
            {"display": "x", "timestamp": 1.0, "project": "/p", "sessionId": "s"},
        )

    def test_is_frozen(self) -> None:
        entry = HistoryEntry("x", 1.0)
        with self.assertRaises(Exception):
            entry.display = "y"  # type: ignore[misc]


class DefaultPathTests(HistoryTestCase):
    """默认路径必须走**动态**访问器（`history_path()`）。"""

    def test_default_path_follows_minicode_home(self) -> None:
        from unittest.mock import patch

        append_history("via-default", "/proj", "s1")  # 不传 path
        with patch.dict(os.environ, {"MINICODE_HOME": str(self.root)}):
            from minicode.config import history_path

            self.assertEqual(history_path(), self.root / "history.jsonl")

    def test_writes_to_the_current_home(self) -> None:
        from unittest.mock import patch

        with patch.dict(os.environ, {"MINICODE_HOME": str(self.root)}):
            append_history("scoped", "/proj", "s1")
        self.assertTrue((self.root / "history.jsonl").exists())
        self.assertEqual(load_history(self.root / "history.jsonl"), ["scoped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
