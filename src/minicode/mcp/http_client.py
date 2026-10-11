"""MCP streamable HTTP 客户端。

比 stdio 简单得多：单个 POST 端点，请求体是 JSON-RPC，响应体是 JSON-RPC。

## 与 TS 实现的一处不同

原实现声明的 `Accept` 里包含 `text/event-stream`，但**只做整体 `JSON.parse`**，
并不解析 SSE 流。也就是说它对 SSE 响应的处理是坏的（会解析失败）。

本实现同样只支持「整体 JSON 响应」，但**不再声明接受 SSE**——
声明了却不处理比不支持更糟：它会让服务器选择一条我们走不通的路径。
如需 SSE，应当真正实现它，而不是挂着这个头。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

#: 单次请求超时（秒）。
HTTP_TIMEOUT = 30.0


class McpHttpError(Exception):
    """HTTP 层面的失败，带可诊断信息。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _extract_auth_hint(headers: Any) -> str:
    """从 WWW-Authenticate 里抽出认证提示。

    很多 MCP 服务器用 OAuth，401 时的这个头是唯一能告诉用户
    「去哪里授权」的线索。
    """
    try:
        raw = headers.get("www-authenticate") if headers else None
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    for key in ("resource_metadata", "authorization_uri"):
        marker = f'{key}="'
        index = raw.find(marker)
        if index != -1:
            start = index + len(marker)
            end = raw.find('"', start)
            if end != -1:
                return f"{key}={raw[start:end]}"
    return raw[:200]


@dataclass
class McpHttpClient:
    """一个 streamable HTTP MCP 服务器连接。"""

    name: str
    url: str
    headers: dict[str, str]
    auth_token: str | None = None

    def __post_init__(self) -> None:
        self._next_id = 1
        self._closed = False

    @property
    def endpoint_key(self) -> str:
        return f"{self.name}::{self.url}"

    def close(self) -> None:
        # 无状态传输：没有需要清理的连接。
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- 收发 ---------------------------------------------------------------

    def _request(
        self, method: str, params: dict[str, Any] | None, *, notify: bool = False
    ) -> dict[str, Any]:
        if self._closed:
            raise McpHttpError("连接已关闭。")

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._next_id
            self._next_id += 1
        if params is not None:
            payload["params"] = params

        request_headers = {
            "content-type": "application/json",
            # 只声明我们真正能处理的内容类型。
            "accept": "application/json",
            **self.headers,
        }
        if self.auth_token:
            request_headers["Authorization"] = f"Bearer {self.auth_token}"

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers=request_headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", errors="replace").strip()[:600]
            except Exception:  # noqa: BLE001
                pass
            hint = _extract_auth_hint(error.headers)
            parts = [f"HTTP {error.code} {error.reason}", f"服务器 {self.name}"]
            if hint:
                parts.append(f"AUTH: {hint}")
            if detail:
                parts.append(detail)
            raise McpHttpError(" | ".join(parts), status=error.code) from error
        except urllib.error.URLError as error:
            raise McpHttpError(f"网络错误（{self.name}）：{error.reason}") from error
        except OSError as error:
            raise McpHttpError(f"请求失败（{self.name}）：{error}") from error

        if not raw.strip() or notify:
            return {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise McpHttpError(
                f"服务器 {self.name} 返回了非 JSON 内容：{raw.strip()[:200]}"
            ) from error

        if not isinstance(parsed, dict):
            raise McpHttpError(f"服务器 {self.name} 返回了非对象 JSON-RPC 响应。")

        rpc_error = parsed.get("error")
        if isinstance(rpc_error, dict):
            raise McpHttpError(str(rpc_error.get("message") or "unknown error"))
        result = parsed.get("result")
        return result if isinstance(result, dict) else {"result": result}

    # -- MCP 方法 -----------------------------------------------------------

    def start(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "minicode", "version": "0.1.0"},
            },
        )
        self._request("notifications/initialized", {}, notify=True)

    def list_tools(self) -> list[Any]:
        from .stdio_client import McpToolDescriptor

        result = self._request("tools/list", {})
        items = result.get("tools")
        if not isinstance(items, list):
            return []
        return [
            McpToolDescriptor(
                name=str(item.get("name")),
                description=str(item.get("description") or ""),
                input_schema=item.get("inputSchema")
                if isinstance(item.get("inputSchema"), dict)
                else {},
            )
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments or {}})

    def list_resources(self) -> list[Any]:
        from .stdio_client import McpResourceDescriptor

        result = self._request("resources/list", {})
        items = result.get("resources")
        if not isinstance(items, list):
            return []
        return [
            McpResourceDescriptor(
                uri=str(item.get("uri")),
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                mime_type=str(item.get("mimeType") or ""),
            )
            for item in items
            if isinstance(item, dict) and item.get("uri")
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        return self._request("resources/read", {"uri": uri})

    def list_prompts(self) -> list[Any]:
        from .stdio_client import McpPromptDescriptor

        result = self._request("prompts/list", {})
        items = result.get("prompts")
        if not isinstance(items, list):
            return []
        return [
            McpPromptDescriptor(
                name=str(item.get("name")),
                description=str(item.get("description") or ""),
                arguments=item.get("arguments")
                if isinstance(item.get("arguments"), list)
                else [],
            )
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("prompts/get", {"name": name, "arguments": arguments or {}})
