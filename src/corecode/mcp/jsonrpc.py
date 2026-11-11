"""JSON-RPC 2.0 over stdio —— 两种分帧。

MCP 的 stdio 传输在实际世界里存在**两种互不兼容的分帧方式**，这是接入 MCP
最容易踩的坑：

  1. `content-length`（LSP 风格）：先发一行头
     `Content-Length: 123\\r\\n\\r\\n`，再发正好 123 字节的 JSON。
  2. `newline-json`（NDJSON）：一行一个 JSON，以 `\\n` 结尾。

规范写的是第一种，但不少实现（尤其是自己写的服务器）只支持第二种。
因此这里两种都实现，并由上层的 `McpStdioClient` 负责**协商**。

## 为什么用读取线程

JSON-RPC 是「请求-响应」式的，但服务器也可能主动发通知。用一条读取线程把
收到的消息放进「按 id 索引的等待表」，请求线程就只需要「发出去 → 等在
自己 id 上」。这样避免了两件事：轮询，以及「边写边读」可能造成的死锁
（管道缓冲区可能在你写的时候被读方塞满）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable

#: 分帧方式。
PROTOCOL_CONTENT_LENGTH = "content-length"
PROTOCOL_NEWLINE_JSON = "newline-json"

#: 单次请求的默认超时（秒）。
DEFAULT_REQUEST_TIMEOUT = 5.0

#: 初始化探测超时：故意很短，因为「服务器不支持这种分帧」不该等 10 秒。
INITIALIZE_PROBE_TIMEOUT = 1.2

#: 初始化正式超时：服务器冷启动（首次加载运行时）可能真的慢。
INITIALIZE_TIMEOUT = 10.0


class JsonRpcError(Exception):
    """服务器返回的 JSON-RPC 错误，或协议层面的失败。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class JsonRpcTimeout(JsonRpcError):
    """请求超时。上层靠这个类型判断「该换一种分帧试试」。"""


def encode_content_length(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def encode_newline_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# 接收缓冲
# ---------------------------------------------------------------------------


@dataclass
class MessageBuffer:
    """把字节流切成一条条消息。两种分帧共用这一个状态机。

    设计成「喂字节、吐出完整消息列表」的纯逻辑，因此可以脱离进程与网络
    单独测试——分帧逻辑是整个传输层最容易写错的部分，必须能直接测。
    """

    protocol: str = PROTOCOL_CONTENT_LENGTH
    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(chunk)
        if self.protocol == PROTOCOL_NEWLINE_JSON:
            return self._drain_lines()
        return self._drain_content_length()

    # -- newline-json -------------------------------------------------------

    def _drain_lines(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while b"\n" in self._buffer:
            line, _, rest = bytes(self._buffer).partition(b"\n")
            self._buffer = bytearray(rest)
            text = line.strip()
            if not text:
                continue
            parsed = _try_parse(text)
            if parsed is not None:
                messages.append(parsed)
        return messages

    # -- content-length -----------------------------------------------------

    def _drain_content_length(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while True:
            separator = self._buffer.find(b"\r\n\r\n")
            if separator == -1:
                # 也容忍只用 \n\n 分隔的实现。
                separator = self._buffer.find(b"\n\n")
                if separator == -1:
                    return messages
                header_block = bytes(self._buffer[:separator])
                body_start = separator + 2
            else:
                header_block = bytes(self._buffer[:separator])
                body_start = separator + 4

            length = _parse_content_length(header_block)
            if length is None:
                # 没有 Content-Length 的头块：丢弃后继续扫描，
                # 否则会永远卡在这一段上。
                self._buffer = bytearray(self._buffer[body_start:])
                continue

            if len(self._buffer) - body_start < length:
                return messages  # 正文还没到齐

            body = bytes(self._buffer[body_start : body_start + length])
            self._buffer = bytearray(self._buffer[body_start + length :])
            parsed = _try_parse(body)
            if parsed is not None:
                messages.append(parsed)


def _parse_content_length(header_block: bytes) -> int | None:
    try:
        text = header_block.decode("ascii", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _try_parse(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# stdio 客户端
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class StdioJsonRpc:
    """在一个子进程的 stdin/stdout 上跑 JSON-RPC。

    生命周期：`start()` 启动读取线程 → `request()` 收发 → `close()` 收尾。
    """

    def __init__(
        self,
        write_stream: BinaryIO,
        read_stream: BinaryIO,
        *,
        protocol: str = PROTOCOL_CONTENT_LENGTH,
        on_notification: Callable[[dict[str, Any]], None] | None = None,
        on_protocol_error: Callable[[str], None] | None = None,
    ) -> None:
        self._write = write_stream
        self._read = read_stream
        self._on_notification = on_notification
        self._on_protocol_error = on_protocol_error

        self.protocol = protocol
        self._buffer = MessageBuffer(protocol=protocol)
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._closed = False
        self._reader: threading.Thread | None = None

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-reader", daemon=True
        )
        self._reader.start()

    def set_protocol(self, protocol: str) -> None:
        """切换分帧方式（协商过程中用）。

        会重建缓冲：用错分帧读到的残留字节在新协议下是无意义的。
        """
        self.protocol = protocol
        self._buffer = MessageBuffer(protocol=protocol)

    def close(self) -> None:
        self._closed = True
        # 唤醒所有等待者，避免它们一直等到超时。
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.event.set()

    # -- 收发 ---------------------------------------------------------------

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float
    ) -> dict[str, Any]:
        if self._closed:
            raise JsonRpcError("连接已关闭。")

        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending()
            self._pending[request_id] = pending

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        try:
            self._send(payload)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise JsonRpcTimeout(f"request timed out for {method}")

        response = pending.response or {}
        error = response.get("error")
        if isinstance(error, dict):
            raise JsonRpcError(
                str(error.get("message") or "unknown error"), error.get("code")
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _send(self, payload: dict[str, Any]) -> None:
        encoder = (
            encode_newline_json
            if self.protocol == PROTOCOL_NEWLINE_JSON
            else encode_content_length
        )
        data = encoder(payload)
        with self._write_lock:
            self._write.write(data)
            self._write.flush()

    # -- 读取线程 -----------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                chunk = self._read.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break  # EOF：子进程退出了
            try:
                messages = self._buffer.feed(chunk)
            except Exception as error:  # noqa: BLE001 - 解析异常不该杀掉读取线程
                if self._on_protocol_error:
                    self._on_protocol_error(str(error))
                continue
            for message in messages:
                self._dispatch(message)
        self.close()

    def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        # 只处理**数字 id** 的响应；通知（无 id）转给回调。
        if isinstance(request_id, int):
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.response = message
                pending.event.set()
            return
        if self._on_notification is not None and "method" in message:
            self._on_notification(message)
