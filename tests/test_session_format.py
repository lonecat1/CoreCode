"""会话文件的**格式契约**：能读原版写出来的会话。

## 为什么单独测这个

会话持久化在原版里**只存在于 TUI**：`saveSession` 的三个调用点全在
`tty-app.ts`，`index.ts`（普通 readline 路径）从不保存会话。

本实现放弃了 TUI，把持久化接进了行模式——这是已登记的有意差异。但由此产生
一个**真实风险**：

> 一个从原版过来的用户，`~/.mini-code/projects/` 下已经有一批 TUI 写的会话。
> 如果本实现的 `load_session` 读不懂它们，用户的全部历史就没了。

所以这里的断言不是「我写得出、我读得回」，而是「**按原版的形状写，我能读**」。

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

from minicode.session.store import (  # noqa: E402
    load_session,
    parse_events,
    project_dir_name,
    save_session,
    session_file_path,
    unwrap_message,
)

#: 原版 `roleToType()` 的完整映射。
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
        self._env = patch.dict(os.environ, {"MINICODE_HOME": home}, clear=False)
        self._env.start()
        os.environ.pop("MINI_CODE_HOME", None)
        self.cwd = os.path.join(self._tmp, "workspace")
        os.makedirs(self.cwd, exist_ok=True)

    def tearDown(self) -> None:
        if self._env is not None:
            self._env.stop()

    def write_events(self, session_id: str, events: list[dict]) -> Path:
        """按原版的形状写一份会话文件（每行一个事件）。"""
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
        """造一个**原版形状**的事件。"""
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
    """按原版的形状写，本实现必须读得懂。"""

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
        # `id` 由事件的 `uuid` 覆盖（与原版一致）
        self.assertEqual(messages[1]["id"], "u1")

    def test_reads_every_role_in_the_original_mapping(self) -> None:
        """9 种角色都要能读，且类型映射与原版 `roleToType` 一致。"""
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
        `load_session_with_report` 存在的理由——原实现会**静默**丢掉它们）。
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
        """原版新加了事件类型时，本实现不该因此丢掉整条消息。"""
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
        """目录名变换与原版一致（**有损**，刻意保留）。"""
        # 原版：cwd.replace(/[/\\:]+/g, '-').replace(/^-+/, '')
        for raw, expected in [
            ("D:\\CoreCode\\minicode-py", "D-CoreCode-minicode-py"),
            ("/home/user/proj", "home-user-proj"),
            ("C:/a\\b", "C-a-b"),
        ]:
            with self.subTest(cwd=raw):
                self.assertEqual(project_dir_name(raw), expected)


class WriteShapeTests(_SessionCase):
    """本实现写出来的事件，字段与原版 `wrapEvent` 必须逐一对上。"""

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
        """事件类型名与原版 `roleToType` 逐条一致。

        **注意下标 0 不下盘**：`save_session` 做 `messages.slice(1)`（原版
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
        """那条 `slice(1)` 必须真的生效——这是原版行为，不是遗漏。"""
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
        """`parentUuid` 必须串起来——原版靠它表达「这条消息接在哪条之后」。"""
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
        """原版行为：`id` 一律以事件 `uuid` 为准，避免消息自带的 id 与链冲突。"""
        message = unwrap_message(
            {"message": {"role": "user", "content": "x", "id": "stale"}, "uuid": "real"}
        )
        self.assertEqual(message["id"], "real")

    def test_unwrap_returns_none_without_a_message(self) -> None:
        self.assertIsNone(unwrap_message({"uuid": "x"}))
        self.assertIsNone(unwrap_message({"message": "not a dict"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
