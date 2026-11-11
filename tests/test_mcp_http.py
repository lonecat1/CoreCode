"""MCP streamable HTTP 客户端的测试。

## 这个模块此前完全没有测试

228 行真实的协议代码（JSON-RPC over HTTP、鉴权提示解析、各种失败的降级），
而 `tests/` 里没有任何一处引用它。这是本项目里唯一一个**完全未覆盖**的模块。

没有测试的地方就是差异藏身处——`memory.py` 就是这样藏了两处（见该文件的
说明）。因此这里把它的每条分支都钉住。

## 一条与安全相关的性质

`accept` 头**只声明 `application/json`**，不声明 `text/event-stream`。

"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.mcp.http_client import (  # noqa: E402
    HTTP_TIMEOUT,
    McpHttpClient,
    McpHttpError,
    _extract_auth_hint,
)

def _ok_response(payload, *, status: int = 200):
    """造一个 urlopen 的上下文管理器返回值。"""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    response = MagicMock()
    response.status = status
    response.read.return_value = raw
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    return response

class AuthHintTests(unittest.TestCase):
    """`WWW-Authenticate` 的解析——401 时唯一能告诉用户「去哪授权」的线索。"""

    def test_extracts_resource_metadata(self) -> None:
        header = 'Bearer resource_metadata="https://auth.example/.well-known/oauth"'
        self.assertEqual(
            _extract_auth_hint({"www-authenticate": header}),
            "resource_metadata=https://auth.example/.well-known/oauth",
        )

    def test_extracts_authorization_uri(self) -> None:
        header = 'Bearer authorization_uri="https://auth.example/authorize"'
        self.assertEqual(
            _extract_auth_hint({"www-authenticate": header}),
            "authorization_uri=https://auth.example/authorize",
        )

    def test_falls_back_to_a_prefix_of_the_raw_header(self) -> None:
        """认不出结构时给原文前 200 字——总比什么都不给好。"""
        header = "Bearer realm=something-unstructured"
        self.assertEqual(_extract_auth_hint({"www-authenticate": header}), header)

    def test_missing_header_returns_empty(self) -> None:
        self.assertEqual(_extract_auth_hint({}), "")
        self.assertEqual(_extract_auth_hint(None), "")

    def test_header_object_raising_is_tolerated(self) -> None:
        """headers 对象可能不支持 `.get`——不能让它把错误路径也搞崩。"""
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("boom")
        self.assertEqual(_extract_auth_hint(broken), "")

    def test_long_raw_header_is_truncated(self) -> None:
        header = "x" * 500
        self.assertEqual(len(_extract_auth_hint({"www-authenticate": header})), 200)

    def test_unterminated_quoted_value(self) -> None:
        """引号没闭合时不该崩——退回原文。"""
        header = 'Bearer resource_metadata="unterminated'
        self.assertEqual(_extract_auth_hint({"www-authenticate": header}), header)

class RequestShapeTests(unittest.TestCase):
    """请求体的构造。"""

    def _client(self, **kwargs):
        return McpHttpClient(name="srv", url="https://x/mcp", headers={}, **kwargs)

    def _capture(self, client, method, params, *, notify=False, response=None):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            return _ok_response(response if response is not None else {"result": {}})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client._request(method, params, notify=notify)
        return captured

    def test_payload_is_json_rpc_2(self) -> None:
        captured = self._capture(self._client(), "tools/list", {})
        self.assertEqual(captured["body"]["jsonrpc"], "2.0")
        self.assertEqual(captured["body"]["method"], "tools/list")

    def test_ids_increment(self) -> None:
        client = self._client()
        first = self._capture(client, "a", None)
        second = self._capture(client, "b", None)
        self.assertEqual(first["body"]["id"], 1)
        self.assertEqual(second["body"]["id"], 2)

    def test_notify_omits_the_id(self) -> None:
        """通知（notification）按 JSON-RPC 规范**不带 id**。"""
        captured = self._capture(
            self._client(), "notifications/initialized", {}, notify=True
        )
        self.assertNotIn("id", captured["body"])

    def test_none_params_are_omitted(self) -> None:
        captured = self._capture(self._client(), "m", None)
        self.assertNotIn("params", captured["body"])

    def test_empty_params_are_kept(self) -> None:
        captured = self._capture(self._client(), "m", {})
        self.assertIn("params", captured["body"])

    def test_content_type_is_json(self) -> None:
        captured = self._capture(self._client(), "m", {})
        self.assertEqual(captured["headers"]["content-type"], "application/json")

    def test_accept_does_not_advertise_sse(self) -> None:
        """**声明了却不处理比不支持更糟。**

        """
        captured = self._capture(self._client(), "m", {})
        self.assertEqual(captured["headers"]["accept"], "application/json")
        self.assertNotIn("text/event-stream", captured["headers"]["accept"])

    def test_custom_headers_are_sent(self) -> None:
        client = McpHttpClient(
            name="srv", url="https://x/mcp", headers={"X-Custom": "v"}
        )
        captured = self._capture(client, "m", {})
        self.assertEqual(captured["headers"]["x-custom"], "v")

    def test_auth_token_becomes_bearer(self) -> None:
        client = self._client(auth_token="tok")
        captured = self._capture(client, "m", {})
        self.assertEqual(captured["headers"]["authorization"], "Bearer tok")

    def test_no_authorization_header_without_token(self) -> None:
        captured = self._capture(self._client(), "m", {})
        self.assertNotIn("authorization", captured["headers"])

    def test_timeout_is_passed(self) -> None:
        captured = self._capture(self._client(), "m", {})
        self.assertEqual(captured["timeout"], HTTP_TIMEOUT)

    def test_method_is_post(self) -> None:
        captured = self._capture(self._client(), "m", {})
        self.assertEqual(captured["request"].get_method(), "POST")

    def test_unicode_payload_is_not_escaped(self) -> None:
        """`ensure_ascii=False`：中文参数按 UTF-8 原样发出，不变成 `\\uXXXX`。"""
        captured = self._capture(self._client(), "m", {"q": "中文"})
        self.assertEqual(captured["body"]["params"]["q"], "中文")

class ResponseHandlingTests(unittest.TestCase):
    def _client(self):
        return McpHttpClient(name="srv", url="https://x/mcp", headers={})

    def _call(self, response):
        with patch("urllib.request.urlopen", return_value=response):
            return self._client()._request("m", {})

    def test_returns_the_result_object(self) -> None:
        self.assertEqual(
            self._call(_ok_response({"jsonrpc": "2.0", "id": 1, "result": {"a": 1}})),
            {"a": 1},
        )

    def test_scalar_result_is_wrapped(self) -> None:
        """结果是标量时包成 `{"result": ...}`，调用方不必区分两种情况。"""
        self.assertEqual(
            self._call(_ok_response({"jsonrpc": "2.0", "id": 1, "result": 42})),
            {"result": 42},
        )

    def test_missing_result_gives_none_value(self) -> None:
        self.assertEqual(
            self._call(_ok_response({"jsonrpc": "2.0", "id": 1})), {"result": None}
        )

    def test_empty_body_returns_empty_dict(self) -> None:
        self.assertEqual(self._call(_ok_response(b"")), {})
        self.assertEqual(self._call(_ok_response(b"   \n")), {})

    def test_non_json_body_raises_with_a_preview(self) -> None:
        with self.assertRaises(McpHttpError) as caught:
            self._call(_ok_response(b"<html>not json</html>"))
        self.assertIn("非 JSON", str(caught.exception))
        self.assertIn("<html>", str(caught.exception))

    def test_json_array_raises(self) -> None:
        with self.assertRaises(McpHttpError) as caught:
            self._call(_ok_response([1, 2, 3]))
        self.assertIn("非对象", str(caught.exception))

    def test_json_rpc_error_becomes_an_exception(self) -> None:
        with self.assertRaises(McpHttpError) as caught:
            self._call(
                _ok_response(
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such method"}}
                )
            )
        self.assertIn("no such method", str(caught.exception))

    def test_json_rpc_error_without_message(self) -> None:
        with self.assertRaises(McpHttpError) as caught:
            self._call(_ok_response({"jsonrpc": "2.0", "id": 1, "error": {}}))
        self.assertIn("unknown error", str(caught.exception))

class FailureTests(unittest.TestCase):
    def _client(self):
        return McpHttpClient(name="srv", url="https://x/mcp", headers={})

    def _http_error(self, code, reason, body=b"", headers=None):
        return urllib.error.HTTPError(
            "https://x/mcp", code, reason, headers or {}, io.BytesIO(body)
        )

    def test_http_error_carries_the_status(self) -> None:
        with patch("urllib.request.urlopen", side_effect=self._http_error(404, "Not Found")):
            with self.assertRaises(McpHttpError) as caught:
                self._client()._request("m", {})
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("404", str(caught.exception))
        self.assertIn("srv", str(caught.exception))

    def test_http_error_body_is_included(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=self._http_error(500, "Server Error", b"internal detail"),
        ):
            with self.assertRaises(McpHttpError) as caught:
                self._client()._request("m", {})
        self.assertIn("internal detail", str(caught.exception))

    def test_401_includes_the_auth_hint(self) -> None:
        """**401 时的提示是唯一能告诉用户「去哪授权」的线索。**"""
        headers = {"www-authenticate": 'Bearer resource_metadata="https://a/meta"'}
        with patch(
            "urllib.request.urlopen",
            side_effect=self._http_error(401, "Unauthorized", b"", headers),
        ):
            with self.assertRaises(McpHttpError) as caught:
                self._client()._request("m", {})
        self.assertIn("AUTH:", str(caught.exception))
        self.assertIn("https://a/meta", str(caught.exception))

    def test_url_error_is_reported_as_a_network_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(McpHttpError) as caught:
                self._client()._request("m", {})
        self.assertIn("网络错误", str(caught.exception))
        self.assertIn("srv", str(caught.exception))

    def test_closed_client_refuses_to_send(self) -> None:
        client = self._client()
        client.close()
        with self.assertRaises(McpHttpError) as caught:
            client._request("m", {})
        self.assertIn("已关闭", str(caught.exception))

    def test_closed_property(self) -> None:
        client = self._client()
        self.assertFalse(client.closed)
        client.close()
        self.assertTrue(client.closed)

    def test_http_error_with_unreadable_body_still_reports_status(self) -> None:
        """读取正文失败不该掩盖状态码——状态码才是主要信息。"""
        error = self._http_error(503, "Unavailable")
        error.read = MagicMock(side_effect=OSError("cannot read"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(McpHttpError) as caught:
                self._client()._request("m", {})
        self.assertEqual(caught.exception.status, 503)

class EndpointKeyTests(unittest.TestCase):
    def test_key_combines_name_and_url(self) -> None:
        client = McpHttpClient(name="srv", url="https://x/mcp", headers={})
        self.assertEqual(client.endpoint_key, "srv::https://x/mcp")

    def test_keys_differ_across_urls(self) -> None:
        a = McpHttpClient(name="s", url="https://a", headers={})
        b = McpHttpClient(name="s", url="https://b", headers={})
        self.assertNotEqual(a.endpoint_key, b.endpoint_key)

class ProtocolMethodTests(unittest.TestCase):
    """各个 MCP 方法的请求参数与结果整形。"""

    def _client(self):
        return McpHttpClient(name="srv", url="https://x/mcp", headers={})

    def _run(self, method_name, response, *args, **kwargs):
        client = self._client()
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["body"] = json.loads(request.data.decode("utf-8"))
            return _ok_response(response)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = getattr(client, method_name)(*args, **kwargs)
        return result, sent["body"]

    def test_start_sends_initialize_then_initialized(self) -> None:
        """两帧：`initialize`（带 id）与 `notifications/initialized`（不带）。"""
        client = self._client()
        bodies = []

        def fake_urlopen(request, timeout=None):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return _ok_response({"result": {}})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.start()

        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["method"], "initialize")
        self.assertIn("id", bodies[0])
        self.assertEqual(bodies[1]["method"], "notifications/initialized")
        self.assertNotIn("id", bodies[1])

    def test_start_declares_the_protocol_version(self) -> None:
        client = self._client()
        bodies: list[dict] = []

        def fake_urlopen(request, timeout=None):
            # 收集**全部**请求体：`start()` 发两帧，只留最后一帧会拿到
            # `notifications/initialized`（它本来就不带 protocolVersion）。
            bodies.append(json.loads(request.data.decode("utf-8")))
            return _ok_response({"result": {}})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.start()
        self.assertEqual(bodies[0]["params"]["protocolVersion"], "2024-11-05")

    def test_list_tools_maps_descriptors(self) -> None:
        result, body = self._run(
            "list_tools",
            {
                "result": {
                    "tools": [
                        {
                            "name": "read",
                            "description": "Read a file",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                }
            },
        )
        self.assertEqual(body["method"], "tools/list")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "read")
        self.assertEqual(result[0].description, "Read a file")

    def test_list_tools_skips_entries_without_a_name(self) -> None:
        result, _ = self._run(
            "list_tools", {"result": {"tools": [{"description": "no name"}, {"name": "ok"}]}}
        )
        self.assertEqual([item.name for item in result], ["ok"])

    def test_list_tools_handles_a_missing_list(self) -> None:
        result, _ = self._run("list_tools", {"result": {}})
        self.assertEqual(result, [])

    def test_list_tools_defaults_a_non_dict_schema(self) -> None:
        result, _ = self._run(
            "list_tools", {"result": {"tools": [{"name": "x", "inputSchema": "nope"}]}}
        )
        self.assertEqual(result[0].input_schema, {})

    def test_call_tool_sends_name_and_arguments(self) -> None:
        result, body = self._run(
            "call_tool", {"result": {"content": []}}, "read", {"path": "a.txt"}
        )
        self.assertEqual(body["method"], "tools/call")
        self.assertEqual(body["params"]["name"], "read")
        self.assertEqual(body["params"]["arguments"], {"path": "a.txt"})

    def test_call_tool_defaults_arguments_to_empty_object(self) -> None:
        _result, body = self._run("call_tool", {"result": {}}, "read", None)
        self.assertEqual(body["params"]["arguments"], {})

    def test_list_resources_maps_descriptors(self) -> None:
        result, body = self._run(
            "list_resources",
            {
                "result": {
                    "resources": [
                        {
                            "uri": "file:///a",
                            "name": "A",
                            "description": "d",
                            "mimeType": "text/plain",
                        }
                    ]
                }
            },
        )
        self.assertEqual(body["method"], "resources/list")
        self.assertEqual(result[0].uri, "file:///a")
        self.assertEqual(result[0].mime_type, "text/plain")

    def test_list_resources_skips_entries_without_a_uri(self) -> None:
        result, _ = self._run(
            "list_resources", {"result": {"resources": [{"name": "no uri"}, {"uri": "u"}]}}
        )
        self.assertEqual([item.uri for item in result], ["u"])

    def test_read_resource_sends_the_uri(self) -> None:
        _result, body = self._run("read_resource", {"result": {}}, "file:///a")
        self.assertEqual(body["method"], "resources/read")
        self.assertEqual(body["params"]["uri"], "file:///a")

    def test_list_prompts_maps_descriptors(self) -> None:
        result, body = self._run(
            "list_prompts",
            {"result": {"prompts": [{"name": "p", "description": "d", "arguments": []}]}},
        )
        self.assertEqual(body["method"], "prompts/list")
        self.assertEqual(result[0].name, "p")

    def test_list_prompts_skips_entries_without_a_name(self) -> None:
        result, _ = self._run(
            "list_prompts", {"result": {"prompts": [{"description": "x"}, {"name": "ok"}]}}
        )
        self.assertEqual([item.name for item in result], ["ok"])

    def test_get_prompt_sends_name_and_arguments(self) -> None:
        _result, body = self._run(
            "get_prompt", {"result": {}}, "greet", {"who": "world"}
        )
        self.assertEqual(body["method"], "prompts/get")
        self.assertEqual(body["params"]["name"], "greet")
        self.assertEqual(body["params"]["arguments"], {"who": "world"})

    def test_get_prompt_defaults_arguments(self) -> None:
        _result, body = self._run("get_prompt", {"result": {}}, "greet")
        self.assertEqual(body["params"]["arguments"], {})

if __name__ == "__main__":
    unittest.main(verbosity=2)
