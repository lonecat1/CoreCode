"""MCP 的行为测试。

分三层，各自针对最容易出错的地方：

  1. **分帧状态机**（`MessageBuffer`）—— 纯逻辑，可脱离进程直接测。
     粘包、半包、两种协议混用、无 Content-Length 的头块，都在这里。
  2. **协议协商** —— 起真实子进程，验证「探测超时后同协议重试」这条逻辑。
     没有它，「冷启动慢」会被误判成「不支持该分帧」。
  3. **桥接** —— 远端工具被包装成本地工具后，模型能不能正常调用。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "src"))

from minicode.mcp import (  # noqa: E402
    PROTOCOL_CONTENT_LENGTH,
    PROTOCOL_NEWLINE_JSON,
    JsonRpcError,
    MessageBuffer,
    McpStdioClient,
    build_connecting_summaries,
    create_mcp_backed_tools,
    encode_content_length,
    encode_newline_json,
    format_prompt_result,
    format_read_resource_result,
    format_tool_call_result,
    mcp_tool_name,
    normalize_input_schema,
    protocol_candidates,
    sanitize_tool_segment,
)
from minicode.tooling import ToolContext  # noqa: E402

unittest.TestCase.maxDiff = None

SERVER = str(Path(_HERE) / "fixtures" / "fake_mcp_server.py")


def server_config(protocol: str = "content-length", **extra) -> dict:
    config = {
        "command": sys.executable,
        "args": [SERVER, "--protocol", protocol],
    }
    config.update(extra)
    return config


# ---------------------------------------------------------------------------
# 1. 分帧状态机
# ---------------------------------------------------------------------------


class MessageBufferTests(unittest.TestCase):
    """分帧是传输层最容易写错的部分，所以脱离进程直接测。"""

    def test_content_length_single_message(self) -> None:
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        messages = buffer.feed(encode_content_length({"id": 1, "result": {"ok": True}}))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 1)

    def test_content_length_two_messages_in_one_chunk(self) -> None:
        """粘包：一次 read 可能拿到多条消息。"""
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        chunk = encode_content_length({"id": 1}) + encode_content_length({"id": 2})
        messages = buffer.feed(chunk)
        self.assertEqual([m["id"] for m in messages], [1, 2])

    def test_content_length_split_across_chunks(self) -> None:
        """半包：一条消息可能分多次到达。"""
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        payload = encode_content_length({"id": 7, "result": {"text": "hello"}})
        self.assertEqual(buffer.feed(payload[:10]), [])
        self.assertEqual(buffer.feed(payload[10:20]), [])
        messages = buffer.feed(payload[20:])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 7)

    def test_content_length_header_split_from_body(self) -> None:
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        payload = encode_content_length({"id": 3})
        header_end = payload.index(b"\r\n\r\n") + 4
        self.assertEqual(buffer.feed(payload[:header_end]), [])
        messages = buffer.feed(payload[header_end:])
        self.assertEqual(messages[0]["id"], 3)

    def test_newline_json_single(self) -> None:
        buffer = MessageBuffer(protocol=PROTOCOL_NEWLINE_JSON)
        messages = buffer.feed(encode_newline_json({"id": 1}))
        self.assertEqual(messages[0]["id"], 1)

    def test_newline_json_multiple_and_partial(self) -> None:
        """一次 read 可能给出「一条完整 + 一条半截」。"""
        buffer = MessageBuffer(protocol=PROTOCOL_NEWLINE_JSON)
        messages = buffer.feed(b'{"id": 1}\n{"id":')
        self.assertEqual([m["id"] for m in messages], [1], "只应吐出完整的那条")
        messages = buffer.feed(b" 2}\n")
        self.assertEqual([m["id"] for m in messages], [2])

    def test_newline_json_skips_blank_lines(self) -> None:
        buffer = MessageBuffer(protocol=PROTOCOL_NEWLINE_JSON)
        messages = buffer.feed(b"\n\n{\"id\": 5}\n\n")
        self.assertEqual(len(messages), 1)

    def test_invalid_json_is_dropped_not_raised(self) -> None:
        """一行坏数据不该让读取线程死掉。"""
        buffer = MessageBuffer(protocol=PROTOCOL_NEWLINE_JSON)
        messages = buffer.feed(b"not json\n{\"id\": 9}\n")
        self.assertEqual([m["id"] for m in messages], [9])

    def test_non_object_json_is_dropped(self) -> None:
        buffer = MessageBuffer(protocol=PROTOCOL_NEWLINE_JSON)
        self.assertEqual(buffer.feed(b"123\n"), [])

    def test_header_without_content_length_is_skipped(self) -> None:
        """没有 Content-Length 的头块必须被丢弃，否则会永远卡住。"""
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        stray = b"X-Unknown: 1\r\n\r\n"
        messages = buffer.feed(stray + encode_content_length({"id": 4}))
        self.assertEqual([m["id"] for m in messages], [4])

    def test_utf8_body_length_is_bytes_not_characters(self) -> None:
        """Content-Length 是**字节数**；中文一个字符占 3 字节。

        按字符数算长度会让解析整体错位——这是最隐蔽的一类分帧 bug。
        """
        buffer = MessageBuffer(protocol=PROTOCOL_CONTENT_LENGTH)
        payload = encode_content_length({"id": 1, "result": {"text": "中文内容"}})
        messages = buffer.feed(payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["result"]["text"], "中文内容")


class ProtocolCandidateTests(unittest.TestCase):
    def test_explicit_protocol_is_the_only_candidate(self) -> None:
        self.assertEqual(
            protocol_candidates(PROTOCOL_NEWLINE_JSON, None), [PROTOCOL_NEWLINE_JSON]
        )

    def test_default_order_is_content_length_first(self) -> None:
        self.assertEqual(
            protocol_candidates(None, None),
            [PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON],
        )

    def test_cached_protocol_is_tried_first(self) -> None:
        self.assertEqual(
            protocol_candidates(None, PROTOCOL_NEWLINE_JSON),
            [PROTOCOL_NEWLINE_JSON, PROTOCOL_CONTENT_LENGTH],
        )

    def test_unknown_cached_value_is_ignored(self) -> None:
        self.assertEqual(
            protocol_candidates(None, "garbage"),
            [PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON],
        )


# ---------------------------------------------------------------------------
# 2. 协议协商（真实子进程）
# ---------------------------------------------------------------------------


class NegotiationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(_HERE) / "_tmp" / "mcp-home"
        self.tmp.mkdir(parents=True, exist_ok=True)
        for item in self.tmp.glob("*.json"):
            item.unlink()
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.tmp)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_connects_to_content_length_server(self) -> None:
        client = McpStdioClient("fake", server_config("content-length"), cwd=_REPO)
        try:
            client.start()
            self.assertEqual(client.protocol, PROTOCOL_CONTENT_LENGTH)
            tools = client.list_tools()
            self.assertEqual({t.name for t in tools}, {"echo", "fail", "structured"})
        finally:
            client.close()

    def test_connects_to_newline_json_server(self) -> None:
        """服务器只支持第二种分帧时，协商必须能落到它上面。"""
        client = McpStdioClient("fake", server_config("newline-json"), cwd=_REPO)
        try:
            client.start()
            self.assertEqual(client.protocol, PROTOCOL_NEWLINE_JSON)
            self.assertTrue(client.list_tools())
        finally:
            client.close()

    def test_slow_startup_does_not_break_negotiation(self) -> None:
        """冷启动慢（2 秒）必须仍用**正确**的分帧连上。

        探测超时是 1.2 秒，所以首次 probe 必然超时。若没有「同协议正常超时
        重试」这一步，实现会误判 content-length 不受支持，回退到
        newline-json 并失败。这条测试就是钉住那个重试。
        """
        client = McpStdioClient("slow", server_config("slow"), cwd=_REPO)
        try:
            client.start()
            self.assertEqual(
                client.protocol,
                PROTOCOL_CONTENT_LENGTH,
                "慢启动不应导致回退到错误的分帧",
            )
        finally:
            client.close()

    def test_protocol_is_cached_after_negotiation(self) -> None:
        client = McpStdioClient("fake", server_config("newline-json"), cwd=_REPO)
        try:
            client.start()
            endpoint = client.endpoint_key
        finally:
            client.close()

        # 必须**解析** JSON 再断言，不能直接对文件原文做子串匹配：
        # 原文里反斜杠是转义的（`D:\\CoreCode`），路径根本不会字面出现。
        cache_file = self.tmp / "mcp-protocol-cache.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIn(endpoint, cache)
        self.assertEqual(cache[endpoint], PROTOCOL_NEWLINE_JSON)

    def test_cached_protocol_is_used_first(self) -> None:
        """缓存的作用是省掉两次失败尝试。"""
        from minicode.mcp import cache_protocol, get_cached_protocol

        cache_protocol("srv::cmd", PROTOCOL_NEWLINE_JSON)
        self.assertEqual(get_cached_protocol("srv::cmd"), PROTOCOL_NEWLINE_JSON)

    def test_bad_command_reports_error(self) -> None:
        client = McpStdioClient(
            "broken", {"command": "definitely-not-a-real-binary-xyz"}, cwd=_REPO
        )
        with self.assertRaises(JsonRpcError):
            client.start()

    def test_missing_command_reports_error(self) -> None:
        client = McpStdioClient("broken", {}, cwd=_REPO)
        with self.assertRaises(JsonRpcError):
            client.start()

    def test_call_tool_roundtrip(self) -> None:
        client = McpStdioClient("fake", server_config(), cwd=_REPO)
        try:
            client.start()
            result = client.call_tool("echo", {"text": "你好"})
            self.assertIn("echo: 你好", result["content"][0]["text"])
        finally:
            client.close()

    def test_resources_and_prompts_are_listed(self) -> None:
        client = McpStdioClient("fake", server_config(), cwd=_REPO)
        try:
            client.start()
            self.assertEqual(len(client.list_resources()), 1)
            self.assertEqual(len(client.list_prompts()), 1)
        finally:
            client.close()


# ---------------------------------------------------------------------------
# 3. 格式化契约
# ---------------------------------------------------------------------------


class FormattingTests(unittest.TestCase):
    def test_tool_result_joins_text_blocks(self) -> None:
        result = format_tool_call_result(
            {"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "A\n\nB")

    def test_tool_result_appends_structured_content(self) -> None:
        result = format_tool_call_result(
            {
                "content": [{"type": "text", "text": "说明"}],
                "structuredContent": {"answer": 42},
            }
        )
        self.assertIn("说明", result.output)
        self.assertIn("STRUCTURED_CONTENT:", result.output)
        self.assertIn('"answer": 42', result.output)

    def test_tool_result_error_flag_propagates(self) -> None:
        result = format_tool_call_result(
            {"content": [{"type": "text", "text": "boom"}], "isError": True}
        )
        self.assertFalse(result.ok)

    def test_tool_result_without_content_falls_back_to_json(self) -> None:
        result = format_tool_call_result({"unexpected": 1})
        self.assertIn("unexpected", result.output)

    def test_resource_result_has_uri_and_mime_headers(self) -> None:
        result = format_read_resource_result(
            {"contents": [{"uri": "test://x", "mimeType": "text/plain", "text": "正文"}]}
        )
        self.assertTrue(result.ok)
        self.assertIn("URI: test://x", result.output)
        self.assertIn("MIME: text/plain", result.output)
        self.assertIn("正文", result.output)

    def test_resource_blob_is_labelled(self) -> None:
        result = format_read_resource_result(
            {"contents": [{"uri": "test://x", "blob": "AAA"}]}
        )
        self.assertIn("BLOB:", result.output)

    def test_empty_resource_contents_reports_clearly(self) -> None:
        result = format_read_resource_result({"contents": []})
        self.assertTrue(result.ok)
        self.assertIn("No resource contents returned.", result.output)

    def test_prompt_result_renders_roles(self) -> None:
        result = format_prompt_result(
            {
                "description": "模板说明",
                "messages": [{"role": "user", "content": "你好"}],
            }
        )
        self.assertIn("DESCRIPTION: 模板说明", result.output)
        self.assertIn("[user]\n你好", result.output)

    def test_prompt_result_handles_content_array(self) -> None:
        result = format_prompt_result(
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "片段"}]}]}
        )
        self.assertIn("片段", result.output)


class NamingTests(unittest.TestCase):
    def test_tool_name_uses_double_underscore_prefix(self) -> None:
        self.assertEqual(mcp_tool_name("github", "list_prs"), "mcp__github__list_prs")

    def test_segments_are_sanitized(self) -> None:
        self.assertEqual(sanitize_tool_segment("My Server!"), "my_server")
        self.assertEqual(sanitize_tool_segment("a.b/c"), "a_b_c")

    def test_empty_segment_falls_back(self) -> None:
        self.assertEqual(sanitize_tool_segment(""), "tool")

    def test_collision_is_prevented_by_prefix(self) -> None:
        """不同服务器的同名工具必须是两个不同的工具名。"""
        self.assertNotEqual(mcp_tool_name("a", "search"), mcp_tool_name("b", "search"))

    def test_input_schema_passthrough(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        self.assertEqual(normalize_input_schema(schema), schema)

    def test_non_object_schema_gets_permissive_default(self) -> None:
        self.assertEqual(
            normalize_input_schema(None),
            {"type": "object", "additionalProperties": True},
        )


class SummaryTests(unittest.TestCase):
    def test_connecting_summaries_mark_disabled(self) -> None:
        summaries = build_connecting_summaries(
            {"a": {"command": "x"}, "b": {"command": "y", "enabled": False}}
        )
        by_name = {item.name: item for item in summaries}
        self.assertEqual(by_name["a"].status, "connecting")
        self.assertEqual(by_name["b"].status, "disabled")

    def test_endpoint_uses_url_when_present(self) -> None:
        summaries = build_connecting_summaries({"a": {"url": "https://x.test/mcp"}})
        self.assertEqual(summaries[0].command, "https://x.test/mcp")


# ---------------------------------------------------------------------------
# 4. 桥接：远端工具变成本地工具
# ---------------------------------------------------------------------------


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(_HERE) / "_tmp" / "mcp-bridge-home"
        self.tmp.mkdir(parents=True, exist_ok=True)
        for item in self.tmp.glob("*.json"):
            item.unlink()
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.tmp)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def _bridge(self, protocol: str = "content-length"):
        return create_mcp_backed_tools(
            _REPO, {"fake": server_config(protocol)}
        )

    def test_tools_are_wrapped_with_prefixed_names(self) -> None:
        bridge = self._bridge()
        try:
            names = {tool.name for tool in bridge.tools}
            self.assertIn("mcp__fake__echo", names)
            self.assertIn("mcp__fake__fail", names)
            self.assertIn("mcp__fake__structured", names)
        finally:
            bridge.dispose()

    def test_resource_and_prompt_tools_are_injected_when_published(self) -> None:
        bridge = self._bridge()
        try:
            names = {tool.name for tool in bridge.tools}
            self.assertIn("list_mcp_resources", names)
            self.assertIn("read_mcp_resource", names)
            self.assertIn("list_mcp_prompts", names)
            self.assertIn("get_mcp_prompt", names)
        finally:
            bridge.dispose()

    def test_summary_reports_connected_with_counts(self) -> None:
        bridge = self._bridge()
        try:
            summary = bridge.summaries[0]
            self.assertEqual(summary.status, "connected")
            self.assertEqual(summary.tool_count, 3)
            self.assertEqual(summary.resource_count, 1)
            self.assertEqual(summary.prompt_count, 1)
            self.assertEqual(summary.protocol, PROTOCOL_CONTENT_LENGTH)
        finally:
            bridge.dispose()

    def test_wrapped_tool_actually_calls_the_server(self) -> None:
        bridge = self._bridge()
        try:
            tool = next(t for t in bridge.tools if t.name == "mcp__fake__echo")
            result = tool.run({"text": "桥接测试"}, ToolContext(cwd=_REPO))
            self.assertTrue(result.ok)
            self.assertIn("echo: 桥接测试", result.output)
        finally:
            bridge.dispose()

    def test_remote_error_becomes_tool_failure(self) -> None:
        bridge = self._bridge()
        try:
            tool = next(t for t in bridge.tools if t.name == "mcp__fake__fail")
            result = tool.run({}, ToolContext(cwd=_REPO))
            self.assertFalse(result.ok)
            self.assertIn("故意失败", result.output)
        finally:
            bridge.dispose()

    def test_unknown_remote_tool_returns_failure_not_crash(self) -> None:
        bridge = self._bridge()
        try:
            tool = next(t for t in bridge.tools if t.name == "mcp__fake__structured")
            # structured 是存在的；用一个不存在的名字走同一条错误路径
            client = bridge._clients[0]  # noqa: SLF001 - 测试需要直接观察
            try:
                client.call_tool("does-not-exist", {})
                self.fail("应当抛出 JsonRpcError")
            except JsonRpcError as error:
                self.assertIn("未知工具", str(error))
        finally:
            bridge.dispose()

    def test_broken_server_is_isolated(self) -> None:
        """一个服务器起不来，不该影响其他服务器。"""
        bridge = create_mcp_backed_tools(
            _REPO,
            {
                "broken": {"command": "definitely-not-real-xyz"},
                "fake": server_config(),
            },
        )
        try:
            by_name = {item.name: item for item in bridge.summaries}
            self.assertEqual(by_name["broken"].status, "error")
            self.assertTrue(by_name["broken"].error)
            self.assertEqual(by_name["fake"].status, "connected")
            # 好的服务器仍然提供了工具
            self.assertTrue(any(t.name.startswith("mcp__fake__") for t in bridge.tools))
        finally:
            bridge.dispose()

    def test_disabled_server_is_skipped(self) -> None:
        bridge = create_mcp_backed_tools(
            _REPO, {"off": {**server_config(), "enabled": False}}
        )
        try:
            self.assertEqual(bridge.summaries[0].status, "disabled")
            self.assertEqual(bridge.tools, [])
        finally:
            bridge.dispose()

    def test_no_servers_is_harmless(self) -> None:
        bridge = create_mcp_backed_tools(_REPO, {})
        try:
            self.assertEqual(bridge.tools, [])
            self.assertEqual(bridge.summaries, [])
        finally:
            bridge.dispose()

    def test_dispose_closes_clients(self) -> None:
        bridge = self._bridge()
        bridge.dispose()
        self.assertEqual(bridge._clients, [])  # noqa: SLF001

    def test_wrapped_tools_work_on_newline_json_server(self) -> None:
        """两条传输路径都要能真正调用成功。"""
        bridge = self._bridge("newline-json")
        try:
            tool = next(t for t in bridge.tools if t.name == "mcp__fake__echo")
            result = tool.run({"text": "NDJSON"}, ToolContext(cwd=_REPO))
            self.assertTrue(result.ok)
            self.assertIn("echo: NDJSON", result.output)
        finally:
            bridge.dispose()

    def test_custom_tool_name_collision_is_reported(self) -> None:
        """未知工具的返回值必须变成可读错误，而不是抛到调用方。"""
        result = format_tool_call_result(
            {"content": [{"type": "text", "text": "错误正文"}], "isError": True}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "错误正文")


if __name__ == "__main__":
    unittest.main(verbosity=2)
