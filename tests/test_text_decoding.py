"""非法 UTF-8 输入下的健壮性。

## 为什么单独立一个文件

这是一个**横切**契约，不归属某一个模块：配置、历史、记忆、会话、权限、
`.gitignore`、技能文件、协议缓存……全都要在「文件里混进了非法字节」时
给出合理反应。

## 两种合理的反应，不能混用

**① 只读的状态文件 → 有损解码，绝不崩。**

实测过的崩点（修复前）：`settings.json`、`history.jsonl`，以及 `/init` 读
`.gitignore` 时（那个只 `except OSError`，于是 `/init` 直接崩）。

修法是给这些读取加 `errors="replace"`——正是 Node 的语义。

**② 会被写回的文件 → 明确失败，不要有损读取。**

`edit_file` / `modify_file` / `patch_file` 会读旧内容、生成 diff、再写盘。
若这里也做有损解码，用户的文件会被**真的改坏**（U+FFFD 落盘）。
所以 `file_review.load_existing_file` 抛 `FileReviewError`，本次修改取消。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from corecode import config, history, memory, repo_init  # noqa: E402
from corecode.file_review import FileReviewError, load_existing_file  # noqa: E402

def _bad_bytes() -> bytes:
    """一段合法 UTF-8 里嵌一个 0xFF —— 0xFF 在 UTF-8 中永不出现。"""
    return b'{"key": "value", "broken": "\xff\xfe"}'

class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(dir=os.path.join(_HERE, "_tmp"), prefix="utf8-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, True))

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self._tmp, name)
        Path(path).write_bytes(data)
        return path

class LenientReadTests(_TempDirCase):
    """只读的状态文件：有损解码，绝不崩。"""

    def test_settings_with_invalid_bytes_is_read_lossily(self) -> None:
        path = self.write("settings.json", _bad_bytes())
        parsed = config.read_settings_file(Path(path))
        # 有损解码后仍是合法 JSON，只是坏字节变成 U+FFFD
        self.assertEqual(parsed["key"], "value")
        self.assertIn("\ufffd", parsed["broken"])

    def test_mcp_config_with_invalid_bytes_is_read_lossily(self) -> None:
        path = self.write(
            "mcp.json", b'{"mcpServers": {"s": {"command": "cmd", "x": "\xff"}}}'
        )
        servers = config.read_mcp_config_file(Path(path))
        self.assertIn("s", servers)
        self.assertEqual(servers["s"]["command"], "cmd")

    def test_mcp_tokens_with_invalid_bytes_is_read_lossily(self) -> None:
        path = self.write("tokens.json", b'{"s": "\xff"}')
        tokens = config.read_mcp_tokens_file(Path(path))
        self.assertIn("s", tokens)

    def test_history_with_invalid_bytes_does_not_crash(self) -> None:
        """**修复前这里会抛 UnicodeDecodeError**，整个 CLI 起不来。"""
        path = self.write("history.jsonl", b'{"display": "\xff\xfe"}\n')
        entries = history.load_history_entries(Path(path))
        # 有损解码后仍是合法 JSON，所以这一行**能**读出来（内容带 U+FFFD）
        self.assertEqual(len(entries), 1)
        self.assertIn("\ufffd", entries[0].display)

    def test_history_keeps_good_lines_around_a_bad_one(self) -> None:
        path = self.write(
            "history.jsonl",
            b'{"display": "good one"}\n{"display": "\xff"}\n{"display": "good two"}\n',
        )
        displays = [entry.display for entry in history.load_history_entries(Path(path))]
        self.assertIn("good one", displays)
        self.assertIn("good two", displays)

    def test_history_skips_a_truly_broken_line(self) -> None:
        """真坏的行（不是编码问题，是 JSON 断了）仍然跳过。"""
        path = self.write(
            "history.jsonl",
            b'{"display": "kept"}\n{"display": broken json\n{"display": "also kept"}\n',
        )
        displays = [entry.display for entry in history.load_history_entries(Path(path))]
        self.assertEqual(displays, ["kept", "also kept"])

    def test_gitignore_with_invalid_bytes_does_not_crash(self) -> None:
        """**修复前 `/init` 会带着 traceback 崩掉**（那里只 `except OSError`）。"""
        path = self.write(".gitignore", b"# \xff\xfe\n")
        status = repo_init.ensure_gitignore_entries(path)
        self.assertIn(status, ("created", "updated", "unchanged"))
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        for entry in repo_init.GITIGNORE_ENTRIES:
            self.assertIn(entry, content, "缺失条目应当被补上")

    def test_memory_with_invalid_bytes_does_not_crash(self) -> None:
        """记忆文件里有非法字节时，有损读入而不是让程序起不来。"""
        self.write("CORECODE.md", b"# \xff\xfe broken\ncontent line\n")
        result = memory.load_memory(self._tmp)
        # 有损解码后内容仍在，坏字节变成 U+FFFD
        self.assertIn("content line", result)

class StrictReadTests(_TempDirCase):
    """会被写回的文件：明确失败，不要有损读取。"""

    def test_non_utf8_file_raises_a_clear_error(self) -> None:
        path = self.write("binary.bin", b"\x00\x01\xff\xfe")
        with self.assertRaises(FileReviewError) as caught:
            load_existing_file(path)
        message = str(caught.exception)
        self.assertIn("UTF-8", message)
        self.assertIn("取消", message, f"应说明本次修改已取消，实际：{message}")

    def test_missing_file_is_still_treated_as_empty(self) -> None:
        """`FileNotFoundError` 仍然返回空串——那是「新建文件」的正常路径。"""
        self.assertEqual(load_existing_file(os.path.join(self._tmp, "nope.txt")), "")

    def test_error_message_does_not_leak_the_raw_codec_text(self) -> None:
        """不要把 `'utf-8' codec can't decode byte 0xff ...` 直接给用户。"""
        path = self.write("binary.bin", b"\xff")
        with self.assertRaises(FileReviewError) as caught:
            load_existing_file(path)
        self.assertNotIn("codec", str(caught.exception))

    def test_the_file_is_not_modified_on_failure(self) -> None:
        """失败路径必须**一个字节都不写**。"""
        original = b"\x00\x01\xff\xfe original"
        path = self.write("binary.bin", original)
        with self.assertRaises(FileReviewError):
            load_existing_file(path)
        self.assertEqual(Path(path).read_bytes(), original)

if __name__ == "__main__":
    unittest.main(verbosity=2)
