"""

## 为什么单独测这个

`tty-app.ts`，`index.ts`（普通 readline 路径）从不保存会话。

本实现放弃了 TUI，把持久化接进了行模式——这是已登记的有意差异。但由此产生
一个**真实风险**：

> 如果本实现的 `load_session` 读不懂它们，用户的全部历史就没了。

## 形状从哪来

逐字段抄自 `session.ts` 的 `wrapEvent()` / `SessionEvent`：

    type             roleToType(message.role)
    message          原样嵌入
    uuid             ensureMessageId(message)
    timestamp        ISO 8601
    sessionId
    cwd
    parentUuid       链式；第一条为 null
    logicalParentUuid  可选（snip / collapse / compact 边界用）
    subtype          可选
    compactMetadata  { trigger, preTokens, postTokens }
    snipMetadata     { type, removedMessageIds, removedCount, tokensFreed,
                       timestamp, createdAt }
    contextCollapseSpan  可选
    title            可选

`roleToType` 的 9 项映射也逐条对上（`assistant_thinking` → `thinking`、
`context_summary` → `summary` …），未知角色回落 `user`。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from corecode.session.store import (  # noqa: E402
    load_session,
    parse_events,
    project_dir_name,
    save_session,
    session_file_path,
    unwrap_message,
)

TS_ROLE_TO_TYPE = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "assistant_thinking": "thinking",
    "assistant_progress": "progress",
    "assistant_tool_call": "tool_call",
    "tool_result": "tool_result",
    "context_summary": "summary",
    "snip_boundary": "snip_boundary",
}

def iso() -> str:
    return "2024-01-02T03:04:05.000Z"

class _SessionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(dir=os.path.join(_HERE, "_tmp"), prefix="sess-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, True))
        self._env = None
        from unittest.mock import patch

        home = os.path.join(self._tmp, "home")
        os.makedirs(home, exist_ok=True)
        self._env = patch.dict(os.environ, {"CORECODE_HOME": home}, clear=False)
        self._env.start()
        self.cwd = os.path.join(self._tmp, "workspace")
        os.makedirs(self.cwd, exist_ok=True)

    def tearDown(self) -> None:
        if self._env is not None:
            self._env.stop()

    def write_events(self, session_id: str, events: list[dict]) -> Path:
        path = session_file_path(self.cwd, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def event(
        self,
        role: str,
        content: str = "",
        *,
        uuid: str,
        parent: str | None = None,
        **extra,
    ) -> dict:
        message = {"role": role, "content": content, "id": uuid}
        if role == "assistant_tool_call":
            message.pop("content", None)
            message.update({"toolUseId": "t1", "toolName": "read_file", "input": {}})
        if role == "tool_result":
            message.update({"toolUseId": "t1", "toolName": "read_file", "isError": False})
        event = {
            "type": TS_ROLE_TO_TYPE.get(role, "user"),
            "message": message,
            "uuid": uuid,
            "timestamp": iso(),
            "sessionId": "ts-session",
            "cwd": self.cwd,
            "parentUuid": parent,
        }
        event.update(extra)
        return event

class ReadsOriginalShapeTests(_SessionCase):

    def test_reads_a_plain_conversation(self) -> None:
        self.write_events(
            "s1",
            [
                self.event("system", "you are a helper", uuid="sys"),
                self.event("user", "hello", uuid="u1", parent="sys"),
                self.event("assistant", "hi there", uuid="a1", parent="u1"),
            ],
        )
        messages = load_session(self.cwd, "s1")
        self.assertIsNotNone(messages)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "user", "assistant"])
        self.assertEqual(messages[1]["content"], "hello")
        self.assertEqual(messages[1]["id"], "u1")

    def test_reads_every_role_in_the_original_mapping(self) -> None:
        events = [self.event(role, "x", uuid=f"id-{role}") for role in TS_ROLE_TO_TYPE]
        self.write_events("s2", events)
        parsed = parse_events(session_file_path(self.cwd, "s2"))
        self.assertEqual([e["type"] for e in parsed], list(TS_ROLE_TO_TYPE.values()))
        messages = load_session(self.cwd, "s2")
        self.assertIsNotNone(messages)
        self.assertEqual([m["role"] for m in messages], list(TS_ROLE_TO_TYPE))
        self.assertEqual(len(messages), 9)

    def test_reads_a_snip_boundary_event_with_metadata(self) -> None:
        """snip 边界事件带 `snipMetadata`，回放时要能把它当边界。

        **`removedMessageIds` 必须指向文件里真实存在的消息**：回放逻辑要把边界
        搬回它取代的位置，找不到那些消息时边界会被丢弃（这正是
        """
        self.write_events(
            "s3",
            [
                self.event("user", "gone-1", uuid="m1"),
                self.event("assistant", "gone-2", uuid="m2"),
                self.event(
                    "snip_boundary",
                    "",
                    uuid="snip-1",
                    parent="m2",
                    snipMetadata={
                        "type": "snip_boundary",
                        "removedMessageIds": ["m1", "m2"],
                        "removedCount": 2,
                        "tokensFreed": 500,
                        "timestamp": iso(),
                        "createdAt": iso(),
                    },
                ),
                self.event("user", "after", uuid="u2", parent="snip-1"),
            ],
        )
        messages = load_session(self.cwd, "s3")
        self.assertIsNotNone(messages)
        # 边界被搬回 m1 的位置，取代了那两条消息
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["snip_boundary", "user"])
        self.assertEqual(messages[1]["content"], "after")

    def test_reads_a_compact_boundary_event(self) -> None:
        """压缩边界（`type=compactBoundary`）要能识别，恢复时从边界之后开始。"""
        self.write_events(
            "s4",
            [
                self.event("user", "before", uuid="u1"),
                {
                    "type": "compactBoundary",
                    "uuid": "cb-1",
                    "timestamp": iso(),
                    "sessionId": "ts-session",
                    "cwd": self.cwd,
                    "parentUuid": "u1",
                    "logicalParentUuid": "u1",
                    "subtype": "manual",
                    "compactMetadata": {
                        "trigger": "manual",
                        "preTokens": 1000,
                        "postTokens": 200,
                    },
                },
                self.event("user", "after", uuid="u2", parent="cb-1"),
            ],
        )
        messages = load_session(self.cwd, "s4")
        self.assertIsNotNone(messages)
        self.assertIn("after", [m.get("content") for m in messages])

    def test_unknown_event_type_does_not_drop_the_message(self) -> None:
        event = self.event("user", "still here", uuid="u9")
        event["type"] = "some_future_type"
        self.write_events("s5", [event])
        messages = load_session(self.cwd, "s5")
        self.assertIsNotNone(messages)
        # 消息本体仍在（`unwrap_message` 只看 `message`，不看 `type`）
        self.assertEqual(messages[0]["content"], "still here")

    def test_event_without_a_message_is_skipped(self) -> None:
        self.write_events(
            "s6",
            [
                self.event("user", "kept", uuid="u1"),
                {
                    "type": "compactBoundary",
                    "uuid": "cb",
                    "timestamp": iso(),
                    "sessionId": "ts-session",
                    "cwd": self.cwd,
                    "parentUuid": "u1",
                },
            ],
        )
        messages = load_session(self.cwd, "s6")
        self.assertIsNotNone(messages)
        self.assertEqual([m["content"] for m in messages], ["kept"])

    def test_project_dir_name_matches_the_original_transform(self) -> None:
        for raw, expected in [
            ("D:\\CoreCode\\corecode", "D-CoreCode-corecode"),
            ("/home/user/proj", "home-user-proj"),
            ("C:/a\\b", "C-a-b"),
        ]:
            with self.subTest(cwd=raw):
                self.assertEqual(project_dir_name(raw), expected)

class WriteShapeTests(_SessionCase):

    REQUIRED = {"type", "message", "uuid", "timestamp", "sessionId", "cwd", "parentUuid"}

    def test_written_events_have_the_original_field_set(self) -> None:
        save_session(
            self.cwd,
            "mine",
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        events = parse_events(session_file_path(self.cwd, "mine"))
        self.assertTrue(events)
        for event in events:
            with self.subTest(uuid=event.get("uuid")):
                self.assertTrue(
                    self.REQUIRED.issubset(event),
                    f"缺字段：{self.REQUIRED - set(event)}",
                )

    def test_written_types_use_the_original_names(self) -> None:
        """

        `saveSession` 同样），因为下标 0 约定是 system——它每回合重新生成，
        存了反而会与新版系统提示冲突。
        """
        save_session(
            self.cwd,
            "mine2",
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u"},
                {"role": "assistant_thinking", "blocks": []},
                {"role": "assistant_progress", "content": "p"},
                {"role": "assistant_tool_call", "toolUseId": "t", "toolName": "x", "input": {}},
                {"role": "tool_result", "toolUseId": "t", "toolName": "x", "content": "r"},
                {"role": "context_summary", "content": "s", "compressedCount": 0},
                {"role": "snip_boundary", "content": "", "removedMessageIds": []},
            ],
        )
        types = [e["type"] for e in parse_events(session_file_path(self.cwd, "mine2"))]
        self.assertEqual(
            types,
            # 没有 "system"——它在下标 0，被 `slice(1)` 丢掉
            ["user", "thinking", "progress", "tool_call",
             "tool_result", "summary", "snip_boundary"],
        )

    def test_the_system_message_is_not_persisted(self) -> None:
        save_session(
            self.cwd,
            "mine5",
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u"},
            ],
        )
        events = parse_events(session_file_path(self.cwd, "mine5"))
        self.assertEqual([e["type"] for e in events], ["user"])

    def test_parent_chain_is_linked(self) -> None:
        save_session(
            self.cwd,
            "mine3",
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"},
            ],
        )
        events = parse_events(session_file_path(self.cwd, "mine3"))
        self.assertIsNone(events[0]["parentUuid"])
        for previous, current in zip(events, events[1:]):
            self.assertEqual(current["parentUuid"], previous["uuid"])

    def test_round_trip_through_our_own_writer(self) -> None:
        original = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        save_session(self.cwd, "mine4", original)
        messages = load_session(self.cwd, "mine4")
        self.assertIsNotNone(messages)
        # system 没下盘（下标 0），所以读回来只有两条
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "hello")

class UnwrapTests(unittest.TestCase):
    def test_unwrap_overrides_id_with_uuid(self) -> None:
        message = unwrap_message(
            {"message": {"role": "user", "content": "x", "id": "stale"}, "uuid": "real"}
        )
        self.assertEqual(message["id"], "real")

    def test_unwrap_returns_none_without_a_message(self) -> None:
        self.assertIsNone(unwrap_message({"uuid": "x"}))
        self.assertIsNone(unwrap_message({"message": "not a dict"}))

if __name__ == "__main__":
    unittest.main(verbosity=2)
