"""MCP stdio 客户端 —— 启动服务器并协商分帧。

## 协商策略（本模块唯一有难度的地方）

服务器不会告诉你它用哪种分帧，**只能试**。策略是：

    候选顺序：显式配置 > 缓存偏好 > [content-length, newline-json]

    对每个候选：
      1. 用 1.2 秒的**探测超时**发 initialize
      2. 探测超时 → 先用同一分帧以正常的 10 秒重试一次
         （服务器可能只是冷启动慢，误判会导致整体回退到错误的分帧）
      3. 仍失败 → 换下一个候选
    全部失败 → 抛出最后一个错误

第 2 步容易被忽略但很重要：很多 MCP 服务器首次启动要加载运行时（比如
`npx` 拉包、Python 导入重库），1.2 秒根本不够。直接回退会得出「这种分帧
不支持」的错误结论，然后一直用错的分帧。

协商结果会缓存到磁盘（key 是「服务器名 + 端点」），下次直接用对的分帧，
省掉两次失败尝试。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonrpc import (
    INITIALIZE_PROBE_TIMEOUT,
    INITIALIZE_TIMEOUT,
    PROTOCOL_CONTENT_LENGTH,
    PROTOCOL_NEWLINE_JSON,
    JsonRpcError,
    JsonRpcTimeout,
    StdioJsonRpc,
)

#: MCP 协议版本（客户端声明）。
PROTOCOL_VERSION = "2024-11-05"

CLIENT_NAME = "minicode"
CLIENT_VERSION = "0.1.0"

#: 协议缓存路径。刻意放在真实家目录下的 `.mini-code`：
#: 它是**跨项目共享**的机器级缓存，不该被单个项目的配置影响。
#: （这一点与原实现一致，属于有意保留的取舍，见 docs/divergences.md。）
def protocol_cache_path() -> Path:
    override = os.environ.get("MINICODE_HOME")
    base = Path(override).expanduser().resolve() if override else Path.home() / ".mini-code"
    return base / "mcp-protocol-cache.json"


#: 各类请求的默认超时（秒）。
DEFAULT_REQUEST_TIMEOUT = 5.0
LIST_REQUEST_TIMEOUT = 3.0


def _read_protocol_cache() -> dict[str, str]:
    try:
        raw = protocol_cache_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def _write_protocol_cache(cache: dict[str, str]) -> None:
    try:
        path = protocol_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        # 缓存写不进去只是失去优化，不该影响功能。
        pass


def get_cached_protocol(endpoint_key: str) -> str | None:
    value = _read_protocol_cache().get(endpoint_key)
    return value if value in (PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON) else None


def cache_protocol(endpoint_key: str, protocol: str) -> None:
    cache = _read_protocol_cache()
    if cache.get(endpoint_key) == protocol:
        return
    cache[endpoint_key] = protocol
    _write_protocol_cache(cache)


def protocol_candidates(
    configured: str | None, cached: str | None
) -> list[str]:
    """算出尝试顺序。"""
    if configured in (PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON):
        return [configured]
    if cached == PROTOCOL_NEWLINE_JSON:
        return [PROTOCOL_NEWLINE_JSON, PROTOCOL_CONTENT_LENGTH]
    if cached == PROTOCOL_CONTENT_LENGTH:
        return [PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON]
    return [PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON]


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


@dataclass
class McpToolDescriptor:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpResourceDescriptor:
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass
class McpPromptDescriptor:
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


class McpStdioClient:
    """一个 stdio MCP 服务器连接。"""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.cwd = cwd
        self._extra_env = env or {}

        self.protocol: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._rpc: StdioJsonRpc | None = None
        self._stderr_tail: list[str] = []
        self._closed = False

    # -- 端点标识 -----------------------------------------------------------

    @property
    def endpoint_key(self) -> str:
        command = str(self.config.get("command") or "")
        args = " ".join(str(a) for a in (self.config.get("args") or []))
        return f"{self.name}::{command} {args}".strip()

    # -- 生命周期 -----------------------------------------------------------

    def _spawn(self) -> None:
        command = str(self.config.get("command") or "").strip()
        if not command:
            raise JsonRpcError(f"MCP 服务器 {self.name} 没有配置 command。")

        args = [str(item) for item in (self.config.get("args") or [])]
        working_dir = self.config.get("cwd")
        resolved_cwd = (
            os.path.abspath(os.path.join(self.cwd, str(working_dir)))
            if working_dir
            else self.cwd
        )

        environment = dict(os.environ)
        for key, value in (self.config.get("env") or {}).items():
            environment[str(key)] = str(value)
        environment.update(self._extra_env)

        try:
            self._process = subprocess.Popen(
                [command, *args],
                cwd=resolved_cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as error:
            raise JsonRpcError(
                f"无法启动 MCP 服务器 {self.name}（{command}）：{error}"
            ) from error

        self._start_stderr_reader()

    def _start_stderr_reader(self) -> None:
        """收集 stderr 的最后若干行——服务器报错时这是唯一线索。"""

        def pump() -> None:
            process = self._process
            if process is None or process.stderr is None:
                return
            try:
                for raw in process.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self._stderr_tail.append(line)
                        if len(self._stderr_tail) > 8:
                            self._stderr_tail.pop(0)
            except (OSError, ValueError):
                pass

        threading.Thread(target=pump, name=f"mcp-stderr-{self.name}", daemon=True).start()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def start(self) -> None:
        """启动并完成 initialize 握手，含分帧协商。"""
        configured = self.config.get("protocol")
        if configured == "auto":
            configured = None
        cached = get_cached_protocol(self.endpoint_key)

        last_error: Exception | None = None
        for protocol in protocol_candidates(configured, cached):
            try:
                self._initialize_with(protocol)
            except Exception as error:  # noqa: BLE001 - 换下一种分帧继续试
                last_error = error
                self._teardown()
                continue

            self.protocol = protocol
            # 只缓存 stdio 的协商结果。
            if configured is None:
                cache_protocol(self.endpoint_key, protocol)
            return

        detail = f"（stderr: {self.stderr_tail}）" if self._stderr_tail else ""
        raise JsonRpcError(
            f"MCP 服务器 {self.name} 初始化失败：{last_error}{detail}"
        )

    def _initialize_with(self, protocol: str) -> None:
        self._spawn()
        process = self._process
        assert process is not None and process.stdin and process.stdout

        rpc = StdioJsonRpc(process.stdin, process.stdout, protocol=protocol)
        rpc.start()
        self._rpc = rpc

        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }

        # ① 短探测：快速排除「服务器根本不支持这种分帧」。
        try:
            rpc.request("initialize", params, timeout=INITIALIZE_PROBE_TIMEOUT)
        except JsonRpcTimeout:
            # ② 同一种分帧、正常超时重试：冷启动可能真的很慢。
            #    少了这一步会把「慢」误判成「不支持」，然后一直用错的分帧。
            rpc.request("initialize", params, timeout=INITIALIZE_TIMEOUT)

        rpc.notify("notifications/initialized", {})

    def _teardown(self) -> None:
        rpc = self._rpc
        if rpc is not None:
            rpc.close()
            self._rpc = None
        process = self._process
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            self._process = None

    def close(self) -> None:
        self._closed = True
        self._teardown()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- MCP 方法 -----------------------------------------------------------

    def _require_rpc(self) -> StdioJsonRpc:
        if self._rpc is None:
            raise JsonRpcError(f"MCP 服务器 {self.name} 未连接。")
        return self._rpc

    def list_tools(self) -> list[McpToolDescriptor]:
        result = self._require_rpc().request(
            "tools/list", {}, timeout=LIST_REQUEST_TIMEOUT
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            return []
        found: list[McpToolDescriptor] = []
        for item in tools:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            found.append(
                McpToolDescriptor(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    input_schema=item.get("inputSchema")
                    if isinstance(item.get("inputSchema"), dict)
                    else {},
                )
            )
        return found

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        return self._require_rpc().request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def list_resources(self) -> list[McpResourceDescriptor]:
        result = self._require_rpc().request(
            "resources/list", {}, timeout=LIST_REQUEST_TIMEOUT
        )
        items = result.get("resources")
        if not isinstance(items, list):
            return []
        found: list[McpResourceDescriptor] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("uri"):
                continue
            found.append(
                McpResourceDescriptor(
                    uri=str(item["uri"]),
                    name=str(item.get("name") or ""),
                    description=str(item.get("description") or ""),
                    mime_type=str(item.get("mimeType") or ""),
                )
            )
        return found

    def read_resource(self, uri: str) -> dict[str, Any]:
        return self._require_rpc().request(
            "resources/read", {"uri": uri}, timeout=DEFAULT_REQUEST_TIMEOUT
        )

    def list_prompts(self) -> list[McpPromptDescriptor]:
        result = self._require_rpc().request(
            "prompts/list", {}, timeout=LIST_REQUEST_TIMEOUT
        )
        items = result.get("prompts")
        if not isinstance(items, list):
            return []
        found: list[McpPromptDescriptor] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            found.append(
                McpPromptDescriptor(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    arguments=item.get("arguments")
                    if isinstance(item.get("arguments"), list)
                    else [],
                )
            )
        return found

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._require_rpc().request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
