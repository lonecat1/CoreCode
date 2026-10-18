"""把远端 MCP 能力包装成本地工具定义。

## 命名

远端工具 `foo` 来自服务器 `github` 时，对外名字是 `mcp__github__foo`。
前缀是**必需的**，不是审美选择：不同服务器的工具可能重名，而工具名是
模型选择工具的唯一键。`prompt.py` 也会把这个命名约定告诉模型。

段名经过 `sanitize_tool_segment`（小写、非字母数字下划线连字符替换为 `_`），
因为模型 API 对工具名字符集有限制。

## 参数校验交给远端

`validator` 是恒等函数，JSON Schema 原样透传。原因：我们并不知道远端工具的
真实约束，本地再校验一遍只会把「远端能处理、我们误判」的合法输入挡住。
代价是参数错误会以远端错误文本的形式回来——但那本来就是最有用的信息。

## 资源与提示

只有当服务器**真的发布了** resources / prompts 时才注入对应的四个工具。
否则会给模型一堆永远失败的工具，浪费它去试探。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..tooling import ToolContext, ToolDefinition, ToolResult

#: 只有这些字符能出现在工具名里。
_UNSAFE_SEGMENT = re.compile(r"[^a-z0-9_-]+")

MCP_TOOL_PREFIX = "mcp__"


def sanitize_tool_segment(value: str) -> str:
    """把任意名字变成合法的工具名片段。"""
    lowered = (value or "").strip().lower()
    cleaned = _UNSAFE_SEGMENT.sub("_", lowered).strip("_")
    return cleaned or "tool"


def normalize_input_schema(schema: Any) -> dict[str, Any]:
    """透传对象 schema；不是对象就给一个宽松的。"""
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "additionalProperties": True}


def mcp_tool_name(server: str, tool: str) -> str:
    return f"{MCP_TOOL_PREFIX}{sanitize_tool_segment(server)}__{sanitize_tool_segment(tool)}"


def _configured_protocol(config: dict[str, Any]) -> str | None:
    """配置里**显式要求**的协议；`auto` / 未配置返回 `None`。

    对应原版错误分支里的
    `config.protocol === 'auto' || undefined ? undefined : config.protocol`。

    为什么失败时要报配置值而不是协商结果：启动就失败了，协商结果必然是
    `None`，报它等于什么都没说。用户需要知道「我要求的是 content-length，
    它没成」。
    """
    protocol = config.get("protocol")
    if protocol in (None, "auto"):
        return None
    return str(protocol)


# ---------------------------------------------------------------------------
# 结果格式化（契约，改动会影响模型看到的文本）
# ---------------------------------------------------------------------------


def format_content_block(block: Any) -> str:
    if not isinstance(block, dict):
        return json.dumps(block, ensure_ascii=False, indent=2)
    if block.get("type") == "text" and "text" in block:
        return str(block["text"])
    return json.dumps(block, ensure_ascii=False, indent=2)


def format_tool_call_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))

    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list) and content:
        parts.append("\n\n".join(format_content_block(item) for item in content))

    if result.get("structuredContent") is not None:
        parts.append(
            "STRUCTURED_CONTENT:\n"
            + json.dumps(result["structuredContent"], ensure_ascii=False, indent=2)
        )

    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False, indent=2))

    text = "\n\n".join(parts).strip()
    return ToolResult(ok=not result.get("isError"), output=text)


def format_read_resource_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult.failure(json.dumps(result, ensure_ascii=False, indent=2))

    contents = result.get("contents")
    if not isinstance(contents, list) or not contents:
        return ToolResult.success("No resource contents returned.")

    blocks: list[str] = []
    for item in contents:
        if not isinstance(item, dict):
            blocks.append(json.dumps(item, ensure_ascii=False, indent=2))
            continue
        header_lines = [f"URI: {item.get('uri') or '(unknown)'}"]
        if item.get("mimeType"):
            header_lines.append(f"MIME: {item['mimeType']}")
        header = "\n".join(header_lines) + "\n\n"

        if isinstance(item.get("text"), str):
            blocks.append(f"{header}{item['text']}")
        elif isinstance(item.get("blob"), str):
            blocks.append(f"{header}BLOB:\n{item['blob']}")
        else:
            blocks.append(f"{header}{json.dumps(item, ensure_ascii=False, indent=2)}")

    return ToolResult.success("\n\n".join(blocks))


def format_prompt_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult.failure(json.dumps(result, ensure_ascii=False, indent=2))

    header = (
        f"DESCRIPTION: {result['description']}\n\n" if result.get("description") else ""
    )

    rendered: list[str] = []
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role") or "unknown"
        content = message.get("content")
        if isinstance(content, str):
            rendered.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, str):
                    pieces.append(part)
                elif isinstance(part, dict) and "text" in part:
                    pieces.append(str(part["text"]))
                else:
                    pieces.append(json.dumps(part, ensure_ascii=False, indent=2))
            rendered.append(f"[{role}]\n" + "\n".join(pieces))
        else:
            rendered.append(
                f"[{role}]\n{json.dumps(content, ensure_ascii=False, indent=2)}"
            )

    body = "\n\n".join(rendered)
    text = f"{header}{body}".strip()
    return ToolResult.success(text or json.dumps(result, ensure_ascii=False, indent=2))


def summarize_server_endpoint(config: dict[str, Any]) -> str:
    url = str(config.get("url") or "").strip()
    if url:
        return url
    command = str(config.get("command") or "").strip()
    args = " ".join(str(item) for item in (config.get("args") or []))
    return f"{command} {args}".strip()


# ---------------------------------------------------------------------------
# 服务器摘要
# ---------------------------------------------------------------------------


@dataclass
class McpServerSummary:
    name: str
    command: str
    status: str  # connecting | connected | error | disabled
    tool_count: int = 0
    protocol: str | None = None
    error: str | None = None
    resource_count: int | None = None
    prompt_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "toolCount": self.tool_count,
            "protocol": self.protocol,
            "error": self.error,
            "resourceCount": self.resource_count,
            "promptCount": self.prompt_count,
        }


def build_connecting_summaries(servers: dict[str, dict[str, Any]]) -> list[McpServerSummary]:
    """hydrate 之前的占位摘要，让 UI 立刻能显示「正在连接」。"""
    result: list[McpServerSummary] = []
    for name, config in servers.items():
        protocol = config.get("protocol")
        result.append(
            McpServerSummary(
                name=name,
                command=summarize_server_endpoint(config),
                status="disabled" if config.get("enabled") is False else "connecting",
                protocol=None if protocol in (None, "auto") else str(protocol),
            )
        )
    return result


# ---------------------------------------------------------------------------
# 桥接
# ---------------------------------------------------------------------------


@dataclass
class McpBridge:
    """把若干 MCP 服务器桥接成本地工具定义。"""

    cwd: str
    servers: dict[str, dict[str, Any]]
    tools: list[ToolDefinition] = field(default_factory=list)
    summaries: list[McpServerSummary] = field(default_factory=list)
    _clients: list[Any] = field(default_factory=list)
    #: 发布资源的服务器 → 它的 client。用于**全局注册一次**的 `list/read_mcp_resource`。
    _published_resources: dict[str, Any] = field(default_factory=dict)
    #: 发布提示的服务器 → 它的 client。用于全局注册一次的 `list/get_mcp_prompt`。
    _published_prompts: dict[str, Any] = field(default_factory=dict)

    # -- 构建 ---------------------------------------------------------------

    def connect(self) -> None:
        """连接全部服务器。单个失败不影响其他服务器。"""
        from .stdio_client import McpStdioClient

        for name, config in self.servers.items():
            if config.get("enabled") is False:
                self.summaries.append(
                    McpServerSummary(
                        name=name,
                        command=summarize_server_endpoint(config),
                        status="disabled",
                    )
                )
                continue

            try:
                client = self._create_client(name, config, McpStdioClient)
                client.start()
            except Exception as error:  # noqa: BLE001 - 单服务器失败要隔离
                self.summaries.append(
                    McpServerSummary(
                        name=name,
                        command=summarize_server_endpoint(config),
                        status="error",
                        # **报「配置里写的」协议，而不是协商结果。**
                        #
                        # 启动就失败了，协商结果必然是 None——报它等于什么都没说。
                        # 原版同理：`config.protocol === 'auto' || undefined
                        # ? undefined : config.protocol`。用户需要知道
                        # 「我要求的是 content-length，它没成」。
                        protocol=_configured_protocol(config),
                        error=str(error),
                    )
                )
                continue

            self._clients.append(client)
            self._hydrate_server(name, config, client)

        # **循环之后再注册通用工具**，全局只注册一次。
        #
        # 与原版一致：它在 `for` 循环外 `tools.push(...)`。这 4 个工具是跨服务器
        # 共用的，靠 `server` 参数路由——按服务器各注册一份会让模型收到同名工具
        # 声明，且第二台服务器的资源/提示不可达。
        if self._published_resources:
            self.tools.extend(self._build_resource_tools())
        if self._published_prompts:
            self.tools.extend(self._build_prompt_tools())

    def _create_client(self, name: str, config: dict[str, Any], stdio_cls: Any) -> Any:
        url = str(config.get("url") or "").strip()
        if url:
            from .http_client import McpHttpClient

            headers = {
                str(key): _interpolate(str(value))
                for key, value in (config.get("headers") or {}).items()
            }
            return McpHttpClient(
                name=name,
                url=url,
                headers=headers,
                auth_token=_load_token(name),
            )
        return stdio_cls(name, config, cwd=self.cwd)

    def _hydrate_server(
        self, name: str, config: dict[str, Any], client: Any
    ) -> None:
        """列工具/资源/提示，并把它们包装成工具定义。"""
        try:
            remote_tools = client.list_tools()
        except Exception as error:  # noqa: BLE001
            # **失败要立刻关掉这个 client。**
            #
            # 原版在 catch 里 `await client.close()`。早先这里只记一条错误摘要
            # 就返回，而 client 已经进了 `self._clients`——于是「已报错」的服务器
            # 仍有一个活着的子进程/连接，一直挂到 dispose 才收。
            #
            # 更麻烦的是它对用户不可见：摘要写着 error，进程却还在跑，
            # 占着端口与内存。关掉它是这一层唯一正确的事。
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该掩盖原始错误
                pass
            self.summaries.append(
                McpServerSummary(
                    name=name,
                    command=summarize_server_endpoint(config),
                    status="error",
                    protocol=getattr(client, "protocol", None),
                    error=str(error),
                )
            )
            return

        for descriptor in remote_tools:
            self.tools.append(self._wrap_tool(name, descriptor, client))

        # 资源与提示是**可选能力**：失败不该让整个服务器算失败。
        resources: list[Any] = []
        prompts: list[Any] = []
        try:
            resources = client.list_resources()
        except Exception:  # noqa: BLE001
            resources = []
        try:
            prompts = client.list_prompts()
        except Exception:  # noqa: BLE001
            prompts = []

        # **这里只登记「谁会发布资源/提示」，不注册通用工具。**
        #
        # 那 4 个工具（`list_mcp_resources` / `read_mcp_resource` /
        # `list_mcp_prompts` / `get_mcp_prompt`）是**跨服务器共用**的，必须
        # 全局只注册一份——原版就是在循环外 `tools.push(...)` 一次。
        #
        # 早先这里按服务器各注册一份，于是两台服务器都会发布资源时，模型会收到
        # **两组同名工具声明**（`model_tools()` 不查重），而 `find()` 只返回第一个
        # ——第二台服务器的资源/提示彻底不可达。
        if resources:
            self._published_resources[name] = client
        if prompts:
            self._published_prompts[name] = client

        self.summaries.append(
            McpServerSummary(
                name=name,
                command=summarize_server_endpoint(config),
                status="connected",
                tool_count=len(remote_tools),
                protocol=getattr(client, "protocol", None),
                resource_count=len(resources) if resources else None,
                prompt_count=len(prompts) if prompts else None,
            )
        )

    # -- 包装 ---------------------------------------------------------------

    def _wrap_tool(
        self, server: str, descriptor: Any, client: Any
    ) -> ToolDefinition:
        remote_name = descriptor.name
        local_name = mcp_tool_name(server, remote_name)
        # **描述要 `strip()` 后再判空。**
        #
        # 原版是 `descriptor.description?.trim() || 兜底文案`。只写 `or` 的话，
        # `"   "`（纯空白）是**真值**，于是模型拿到一条空白描述——等于没描述，
        # 而它本来会得到「Call MCP tool X from server Y.」这句有用的兜底。
        cleaned_description = (descriptor.description or "").strip()
        description = cleaned_description or (
            f"Call MCP tool {remote_name} from server {server}."
        )

        def run(data: Any, context: ToolContext) -> ToolResult:
            try:
                result = client.call_tool(remote_name, data)
            except Exception as error:  # noqa: BLE001 - 远端失败要变成工具失败
                return ToolResult.failure(f"MCP {server} 调用失败：{error}")
            return format_tool_call_result(result)

        return ToolDefinition(
            name=local_name,
            description=description,
            input_schema=normalize_input_schema(descriptor.input_schema),
            # 参数校验交给远端：我们不知道它的真实约束。
            validator=lambda data: data if isinstance(data, dict) else {},
            run=run,
        )

    def _build_resource_tools(self) -> list[ToolDefinition]:
        """`list_mcp_resources` / `read_mcp_resource` —— **跨服务器共用一份**。

        对应原版 `mcp.ts:1113-1178`。两者的 `server` 语义**不同**，这一点容易漏：

        | 工具 | `server` | 缺省时 |
        |---|---|---|
        | `list_mcp_resources` | **可选** | 遍历**所有**服务器 |
        | `read_mcp_resource` | **必填**（`min(1)`） | 不可能缺省 |

        行格式也照抄：`{server}: {uri}{ (name)}{ - description}`。
        服务器名前缀是**必要的**——跨服务器列出时，不写名字模型就分不清
        同名资源属于谁。
        """

        def _targets(server: str) -> list[tuple[str, Any]]:
            if server:
                client = self._published_resources.get(server)
                return [(server, client)] if client is not None else []
            return sorted(self._published_resources.items())

        def list_resources(data: Any, context: ToolContext) -> ToolResult:
            server = str((data or {}).get("server") or "").strip()
            lines: list[str] = []
            for name, client in _targets(server):
                try:
                    items = client.list_resources()
                except Exception as error:  # noqa: BLE001
                    lines.append(f"{name}: failed to list resources ({error})")
                    continue
                for item in items:
                    shown_name = f" ({item.name})" if item.name else ""
                    described = f" - {item.description}" if item.description else ""
                    lines.append(f"{name}: {item.uri}{shown_name}{described}")
            if lines:
                return ToolResult.success("\n".join(lines))
            return ToolResult.success(
                "Connected MCP servers did not publish any MCP resources. "
                "This does not mean MCP tools are unavailable."
            )

        def read_resource(data: Any, context: ToolContext) -> ToolResult:
            server = str((data or {}).get("server") or "").strip()
            uri = str((data or {}).get("uri") or "").strip()
            if not server:
                return ToolResult.failure("server 不能为空。")
            if not uri:
                return ToolResult.failure("uri 不能为空。")
            client = self._published_resources.get(server)
            if client is None:
                return ToolResult.failure(f"Unknown MCP server: {server}")
            try:
                result = client.read_resource(uri)
            except Exception as error:  # noqa: BLE001
                return ToolResult.failure(f"MCP {server} 读取资源失败：{error}")
            return format_read_resource_result(result)

        return [
            ToolDefinition(
                name="list_mcp_resources",
                description=(
                    "List optional MCP resources exposed by connected MCP "
                    "servers when a server actually publishes them."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"server": {"type": "string"}},
                },
                validator=lambda data: data if isinstance(data, dict) else {},
                run=list_resources,
            ),
            ToolDefinition(
                name="read_mcp_resource",
                description="Read a specific optional MCP resource by server and URI.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "uri": {"type": "string"},
                    },
                    "required": ["server", "uri"],
                },
                validator=lambda data: data if isinstance(data, dict) else {},
                run=read_resource,
            ),
        ]

    def _build_prompt_tools(self) -> list[ToolDefinition]:
        """`list_mcp_prompts` / `get_mcp_prompt` —— **跨服务器共用一份**。

        对应原版 `mcp.ts:1182-1240`。`server` 的语义与资源那一对**相同**：
        `list_mcp_prompts` 可选、`get_mcp_prompt` 必填。

        行格式照抄原版：`{server}: {name} args=[a*, b] - description`
        （`*` 标记必填参数）。`args=[...]` 那段是模型判断「这个提示要填什么」
        的唯一依据，漏掉它模型只能猜。
        """

        def _targets(server: str) -> list[tuple[str, Any]]:
            if server:
                client = self._published_prompts.get(server)
                return [(server, client)] if client is not None else []
            return sorted(self._published_prompts.items())

        def list_prompts(data: Any, context: ToolContext) -> ToolResult:
            server = str((data or {}).get("server") or "").strip()
            lines: list[str] = []
            for name, client in _targets(server):
                try:
                    items = client.list_prompts()
                except Exception as error:  # noqa: BLE001
                    lines.append(f"{name}: failed to list prompts ({error})")
                    continue
                for item in items:
                    rendered = ", ".join(
                        f"{arg.get('name')}{'*' if arg.get('required') else ''}"
                        for arg in item.arguments
                        if isinstance(arg, dict)
                    )
                    args_summary = f" args=[{rendered}]" if rendered else ""
                    described = f" - {item.description}" if item.description else ""
                    lines.append(f"{name}: {item.name}{args_summary}{described}")
            if lines:
                return ToolResult.success("\n".join(lines))
            return ToolResult.success(
                "Connected MCP servers did not publish any MCP prompts. "
                "This does not mean MCP tools are unavailable."
            )

        def get_prompt(data: Any, context: ToolContext) -> ToolResult:
            server = str((data or {}).get("server") or "").strip()
            name = str((data or {}).get("name") or "").strip()
            if not server:
                return ToolResult.failure("server 不能为空。")
            if not name:
                return ToolResult.failure("name 不能为空。")
            client = self._published_prompts.get(server)
            if client is None:
                return ToolResult.failure(f"Unknown MCP server: {server}")
            arguments = (data or {}).get("arguments")
            try:
                result = client.get_prompt(
                    name, arguments if isinstance(arguments, dict) else None
                )
            except Exception as error:  # noqa: BLE001
                return ToolResult.failure(f"MCP {server} 获取提示失败：{error}")
            return format_prompt_result(result)

        return [
            ToolDefinition(
                name="list_mcp_prompts",
                description=(
                    "List optional MCP prompts exposed by connected MCP servers "
                    "when a server actually publishes them."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"server": {"type": "string"}},
                },
                validator=lambda data: data if isinstance(data, dict) else {},
                run=list_prompts,
            ),
            ToolDefinition(
                name="get_mcp_prompt",
                description=(
                    "Fetch a rendered optional MCP prompt by server, prompt name, "
                    "and optional arguments."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "name": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["server", "name"],
                },
                validator=lambda data: data if isinstance(data, dict) else {},
                run=get_prompt,
            ),
        ]

    # -- 收尾 ---------------------------------------------------------------

    def dispose(self) -> None:
        for client in self._clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 清理失败不该掩盖真实错误
                pass
        self._clients.clear()


def _interpolate(value: str) -> str:
    """把 `$VAR` / `${VAR}` 替换成环境变量值。"""
    import os

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, "")

    return re.sub(r"\$(\w+)|\$\{(\w+)\}", replace, value)


def _load_token(server_name: str) -> str | None:
    """从 `mcp-tokens.json` 读该服务器的 bearer token。"""
    from ..config import minicode_home

    path = minicode_home() / "mcp-tokens.json"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(server_name)
    return str(value) if value else None


def create_mcp_backed_tools(
    cwd: str, servers: dict[str, dict[str, Any]]
) -> McpBridge:
    """连接全部服务器并返回桥接结果。"""
    bridge = McpBridge(cwd=cwd, servers=servers)
    bridge.connect()
    return bridge
