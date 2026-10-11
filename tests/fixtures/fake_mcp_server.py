"""一个真实的 MCP 测试服务器（stdio 传输）。

刻意实现两种分帧，并通过 `--protocol` 选择：

    content-length   LSP 风格（先发头再发正文）
    newline-json     NDJSON（一行一个 JSON）
    slow             与 content-length 相同，但 initialize 延迟 2 秒
                     —— 用来验证「探测超时后同协议重试」这条逻辑

`slow` 模式尤其重要：没有它，我就无法证明「冷启动慢」不会被误判成
「不支持这种分帧」。

它提供：
  - 工具 `echo`（回显）、`fail`（返回 isError）、`structured`（带 structuredContent）
  - 资源 `test://readme`
  - 提示 `greet`（带必填参数 name）

由 `tests/test_mcp.py` 以子进程方式启动，因此走的是真实管道与真实分帧，
而不是 mock。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

PROTOCOL = "content-length"
RESOURCES = [{"uri": "test://readme", "name": "README", "mimeType": "text/plain"}]
PROMPTS = [
    {
        "name": "greet",
        "description": "打招呼",
        "arguments": [{"name": "who", "required": True}],
    }
]
TOOLS = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "总是返回错误结果",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "structured",
        "description": "返回结构化内容",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def write_message(payload: dict[str, Any]) -> None:
    out = sys.stdout.buffer
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if PROTOCOL == "newline-json":
        out.write(body + b"\n")
    else:
        out.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    out.flush()


def read_message() -> dict[str, Any] | None:
    stream = sys.stdin.buffer
    if PROTOCOL == "newline-json":
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        return json.loads(line) if line else None

    length: int | None = None
    while True:
        header = stream.readline()
        if not header:
            return None
        text = header.decode("ascii", errors="replace").strip()
        if not text:
            break
        name, _, value = text.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length is None:
        return None
    body = stream.read(length)
    return json.loads(body.decode("utf-8")) if body else None


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        return None  # 通知：不需要回复

    if method == "initialize":
        if SLOW:
            time.sleep(2.0)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}],
                    "isError": False,
                },
            }
        if name == "fail":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "故意失败"}],
                    "isError": True,
                },
            }
        if name == "structured":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "见结构化内容"}],
                    "structuredContent": {"answer": 42, "ok": True},
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"未知工具 {name}"},
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": RESOURCES}}

    if method == "resources/read":
        uri = params.get("uri")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "contents": [
                    {"uri": uri, "mimeType": "text/plain", "text": "资源正文内容"}
                ]
            },
        }

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": PROMPTS}}

    if method == "prompts/get":
        who = (params.get("arguments") or {}).get("who", "世界")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "description": "打招呼模板",
                "messages": [{"role": "user", "content": f"你好，{who}！"}],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"未知方法 {method}"},
    }


SLOW = False


def main(argv: list[str]) -> int:
    global PROTOCOL, SLOW
    for index, arg in enumerate(argv):
        if arg == "--protocol" and index + 1 < len(argv):
            PROTOCOL = argv[index + 1]
        elif arg == "--slow":
            SLOW = True
    if PROTOCOL == "slow":
        PROTOCOL = "content-length"
        SLOW = True

    while True:
        try:
            message = read_message()
        except Exception:  # noqa: BLE001 - 解析失败就退出，让客户端看到 EOF
            return 1
        if message is None:
            return 0
        reply = handle(message)
        if reply is not None:
            write_message(reply)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
