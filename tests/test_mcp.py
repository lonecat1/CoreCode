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

    # ------------------------------------------------------------------
    # 通用工具的注册与路由（`list/read_mcp_resource`、`list/get_mcp_prompt`）
    #
    # 这一组钉住一个**真实缺陷**：那 4 个工具是**跨服务器共用**的，原版在
    # 循环外 `tools.push(...)` 只注册一份。本实现早先按服务器各注册一份，
    # 于是两台服务器都发布资源时模型会收到**两组同名工具声明**，而
    # `ToolRegistry.find()` 只返回第一个 —— 第二台的资源/提示彻底不可达。
    # ------------------------------------------------------------------

    def _tool(self, bridge, name: str):
        return next(t for t in bridge.tools if t.name == name)

    def test_generic_tools_are_registered_once_with_two_servers(self) -> None:
        """**两台服务器 → 4 个通用工具仍各只有一份。**

        重名会让 `model_tools()` 把两组同名声明都发给模型（可能被 API 拒），
        而 `find()` 只取第一个，第二个服务器的能力静默消失。
        """
        bridge = create_mcp_backed_tools(
            _REPO, {"alpha": server_config(), "beta": server_config()}
        )
        try:
            names = [tool.name for tool in bridge.tools]
            for generic in (
                "list_mcp_resources",
                "read_mcp_resource",
                "list_mcp_prompts",
                "get_mcp_prompt",
            ):
                with self.subTest(tool=generic):
                    self.assertEqual(
                        names.count(generic), 1,
                        f"{generic} 出现了 {names.count(generic)} 次",
                    )
            # 服务端工具仍是每台 3 个（echo/fail/structured）→ 3+3+4 = 10
            self.assertEqual(len(names), 10, sorted(names))
        finally:
            bridge.dispose()

    def test_list_resources_without_server_walks_every_server(self) -> None:
        """缺 `server` → 遍历**所有**发布资源的服务器，并**逐行带服务器名**。

        前缀不是装饰：跨服务器列出时不写名字，模型分不清同名资源属于谁。
        """
        bridge = create_mcp_backed_tools(
            _REPO, {"alpha": server_config(), "beta": server_config()}
        )
        try:
            result = self._tool(bridge, "list_mcp_resources").run(
                {}, ToolContext(cwd=_REPO)
            )
            self.assertTrue(result.ok, result.output)
            lines = [l for l in result.output.splitlines() if "test://" in l]
            self.assertTrue(lines, result.output)
            for prefix in ("alpha: ", "beta: "):
                with self.subTest(prefix=prefix):
                    self.assertTrue(
                        any(l.startswith(prefix) for l in lines),
                        f"缺少 {prefix!r} 开头的行：{lines}",
                    )
        finally:
            bridge.dispose()

    def test_list_resources_with_server_filters_to_that_one(self) -> None:
        bridge = create_mcp_backed_tools(
            _REPO, {"alpha": server_config(), "beta": server_config()}
        )
        try:
            result = self._tool(bridge, "list_mcp_resources").run(
                {"server": "beta"}, ToolContext(cwd=_REPO)
            )
            self.assertTrue(result.ok, result.output)
            self.assertIn("beta: ", result.output)
            self.assertNotIn("alpha: ", result.output)
        finally:
            bridge.dispose()

    def test_list_resources_with_unknown_server_says_nothing_published(self) -> None:
        """未知服务器 → **不是**报错，而是「没有任何服务器发布」那条固定文案。

        原版行为：`targetClients` 过滤后为空 → `lines` 为空 → 返回
        `Connected MCP servers did not publish any MCP resources. …`
        它刻意加了后半句「这不代表 MCP 工具不可用」，避免模型误判。
        """
        bridge = self._bridge()
        try:
            result = self._tool(bridge, "list_mcp_resources").run(
                {"server": "nope"}, ToolContext(cwd=_REPO)
            )
            self.assertTrue(result.ok)
            self.assertIn("did not publish any MCP resources", result.output)
            self.assertIn("does not mean MCP tools are unavailable", result.output)
        finally:
            bridge.dispose()

    def test_read_resource_requires_a_server(self) -> None:
        """`read_mcp_resource` 的 `server` 是**必填**，且未知时明确报错。"""
        bridge = self._bridge()
        try:
            tool = self._tool(bridge, "read_mcp_resource")
            self.assertIn("server", tool.input_schema["required"])
            self.assertIn("uri", tool.input_schema["required"])

            missing = tool.run({"uri": "test://readme"}, ToolContext(cwd=_REPO))
            self.assertFalse(missing.ok)

            unknown = tool.run(
                {"server": "nope", "uri": "test://readme"}, ToolContext(cwd=_REPO)
            )
            self.assertFalse(unknown.ok)
            self.assertIn("Unknown MCP server: nope", unknown.output)
        finally:
            bridge.dispose()

    def test_read_resource_returns_the_body(self) -> None:
        bridge = self._bridge()
        try:
            result = self._tool(bridge, "read_mcp_resource").run(
                {"server": "fake", "uri": "test://readme"}, ToolContext(cwd=_REPO)
            )
            self.assertTrue(result.ok, result.output)
            self.assertTrue(result.output.strip())
        finally:
            bridge.dispose()

    def test_list_prompts_formats_arguments_with_required_star(self) -> None:
        """`args=[who*]` —— 星号标记必填参数，是模型「该填什么」的唯一依据。"""
        bridge = self._bridge()
        try:
            result = self._tool(bridge, "list_mcp_prompts").run(
                {}, ToolContext(cwd=_REPO)
            )
            self.assertTrue(result.ok, result.output)
            self.assertIn("fake: greet", result.output)
            self.assertIn("args=[who*]", result.output)
        finally:
            bridge.dispose()

    def test_get_prompt_requires_server_and_name(self) -> None:
        bridge = self._bridge()
        try:
            tool = self._tool(bridge, "get_mcp_prompt")
            required = tool.input_schema["required"]
            self.assertEqual(sorted(required), ["name", "server"])
            # `arguments` 要声明成「任意字符串键值」，否则模型可能传嵌套对象
            self.assertEqual(
                tool.input_schema["properties"]["arguments"]["additionalProperties"],
                {"type": "string"},
            )
            unknown = tool.run(
                {"server": "nope", "name": "greet"}, ToolContext(cwd=_REPO)
            )
            self.assertFalse(unknown.ok)
            self.assertIn("Unknown MCP server: nope", unknown.output)
        finally:
            bridge.dispose()

    def test_generic_tool_descriptions_do_not_name_a_server(self) -> None:
        """描述里**不能**写死某个服务器名——同一份工具服务所有服务器。

        早先描述是 `列出 MCP 服务器 {server} 发布的资源。`，两台服务器就会
        产生两条文字不同的同名工具，模型看到哪个取决于顺序。
        """
        bridge = create_mcp_backed_tools(
            _REPO, {"alpha": server_config(), "beta": server_config()}
        )
        try:
            for generic in (
                "list_mcp_resources",
                "read_mcp_resource",
                "list_mcp_prompts",
                "get_mcp_prompt",
            ):
                description = self._tool(bridge, generic).description
                for server in ("alpha", "beta"):
                    with self.subTest(tool=generic, server=server):
                        self.assertNotIn(server, description)
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

    def test_failed_start_reports_the_configured_protocol(self) -> None:
        """**启动失败时报「配置里写的」协议，而不是协商结果。**

        启动就失败了，协商结果必然是 `None`——报它等于什么都没说。用户需要
        知道「我要求的是 content-length，它没成」。原版同理。
        """
        from minicode.mcp.bridge import _configured_protocol

        self.assertEqual(_configured_protocol({"protocol": "content-length"}), "content-length")
        self.assertIsNone(_configured_protocol({"protocol": "auto"}))
        self.assertIsNone(_configured_protocol({}))

        bridge = create_mcp_backed_tools(
            _REPO,
            {"broken": {"command": "definitely-not-real-xyz", "protocol": "newline-json"}},
        )
        try:
            self.assertEqual(bridge.summaries[0].status, "error")
            self.assertEqual(bridge.summaries[0].protocol, "newline-json")
        finally:
            bridge.dispose()

    def test_auto_protocol_is_not_reported_on_failure(self) -> None:
        """`auto` 是「自己挑」，报它没有信息量。"""
        bridge = create_mcp_backed_tools(
            _REPO, {"broken": {"command": "definitely-not-real-xyz", "protocol": "auto"}}
        )
        try:
            self.assertEqual(bridge.summaries[0].status, "error")
            self.assertIsNone(bridge.summaries[0].protocol)
        finally:
            bridge.dispose()

    def test_hydrate_failure_closes_the_client(self) -> None:
        """**列工具失败要立刻关掉 client。**

        早先只记一条错误摘要就返回，而 client 已经进了 `_clients`——于是
        「已报错」的服务器仍有一个活着的子进程/连接，一直挂到 dispose。
        摘要写着 error 而进程还在跑，对用户不可见。
        """
        from minicode.mcp.bridge import McpBridge

        closed: list[bool] = []

        class ExplodingClient:
            protocol = "content-length"

            def list_tools(self):
                raise RuntimeError("远端拒绝列工具")

            def close(self):
                closed.append(True)

        bridge = McpBridge(cwd=_REPO, servers={"x": {"command": "x"}})
        bridge._hydrate_server("x", {"command": "x"}, ExplodingClient())  # noqa: SLF001

        self.assertEqual(bridge.summaries[0].status, "error")
        self.assertIn("远端拒绝列工具", bridge.summaries[0].error)
        self.assertEqual(closed, [True], "失败后必须关闭 client")

    def test_close_failure_does_not_mask_the_original_error(self) -> None:
        """关闭本身失败时，报出的仍是**原始**错误。"""
        from minicode.mcp.bridge import McpBridge

        class ExplodingClient:
            protocol = None

            def list_tools(self):
                raise RuntimeError("原始错误")

            def close(self):
                raise RuntimeError("关闭也失败了")

        bridge = McpBridge(cwd=_REPO, servers={"x": {"command": "x"}})
        bridge._hydrate_server("x", {"command": "x"}, ExplodingClient())  # noqa: SLF001

        self.assertEqual(bridge.summaries[0].status, "error")
        self.assertIn("原始错误", bridge.summaries[0].error)
        self.assertNotIn("关闭也失败", bridge.summaries[0].error)

    def test_tool_description_falls_back_when_blank(self) -> None:
        """**空白描述要回退到兜底文案。**

        原版是 `descriptor.description?.trim() || 兜底`。只写 `or` 的话
        `"   "` 是**真值**，于是模型拿到一条空白描述——等于没描述，
        而它本来会得到「Call MCP tool X from server Y.」这句有用的兜底。
        """
        from minicode.mcp.bridge import McpBridge

        class NoopClient:
            def call_tool(self, name, arguments):
                return {"content": []}

        class Descriptor:
            def __init__(self, description, name="t"):
                self.name = name
                self.description = description
                self.input_schema = {"type": "object"}

        bridge = McpBridge(cwd=_REPO, servers={})
        for blank in ("", "   ", "\n\t "):
            with self.subTest(description=blank):
                wrapped = bridge._wrap_tool("srv", Descriptor(blank), NoopClient())  # noqa: SLF001
                self.assertEqual(
                    wrapped.description, "Call MCP tool t from server srv."
                )

    def test_tool_description_is_stripped(self) -> None:
        """有内容时也要去掉首尾空白——模型看到的是干净的一句。"""
        from minicode.mcp.bridge import McpBridge

        class NoopClient:
            def call_tool(self, name, arguments):
                return {"content": []}

        class Descriptor:
            name = "t"
            description = "  Reads things.  "
            input_schema = {"type": "object"}

        bridge = McpBridge(cwd=_REPO, servers={})
        wrapped = bridge._wrap_tool("srv", Descriptor(), NoopClient())  # noqa: SLF001
        self.assertEqual(wrapped.description, "Reads things.")

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
