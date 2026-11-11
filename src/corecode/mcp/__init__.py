"""MCP（Model Context Protocol）客户端。

    jsonrpc.py      JSON-RPC 2.0 收发 + 两种分帧（content-length / newline-json）
    stdio_client.py 子进程传输 + **分帧协商** + MCP 方法
    http_client.py  streamable HTTP 传输
    bridge.py       把远端工具/资源/提示包装成本地 ToolDefinition

对外只需要一个入口：

    from corecode.mcp import create_mcp_backed_tools
    bridge = create_mcp_backed_tools(cwd, servers)
    registry.add_tools(bridge.tools)
    registry.set_mcp_servers([s.to_dict() for s in bridge.summaries])

`bridge.dispose()` 负责关闭全部连接。
"""

from .bridge import (
    MCP_TOOL_PREFIX,
    McpBridge,
    McpServerSummary,
    build_connecting_summaries,
    create_mcp_backed_tools,
    format_prompt_result,
    format_read_resource_result,
    format_tool_call_result,
    mcp_tool_name,
    normalize_input_schema,
    sanitize_tool_segment,
    summarize_server_endpoint,
)
from .http_client import McpHttpClient, McpHttpError
from .jsonrpc import (
    INITIALIZE_PROBE_TIMEOUT,
    INITIALIZE_TIMEOUT,
    PROTOCOL_CONTENT_LENGTH,
    PROTOCOL_NEWLINE_JSON,
    JsonRpcError,
    JsonRpcTimeout,
    MessageBuffer,
    StdioJsonRpc,
    encode_content_length,
    encode_newline_json,
)
from .stdio_client import (
    McpPromptDescriptor,
    McpResourceDescriptor,
    McpStdioClient,
    McpToolDescriptor,
    cache_protocol,
    get_cached_protocol,
    protocol_candidates,
)

__all__ = [
    "INITIALIZE_PROBE_TIMEOUT",
    "INITIALIZE_TIMEOUT",
    "MCP_TOOL_PREFIX",
    "PROTOCOL_CONTENT_LENGTH",
    "PROTOCOL_NEWLINE_JSON",
    "JsonRpcError",
    "JsonRpcTimeout",
    "McpBridge",
    "McpHttpClient",
    "McpHttpError",
    "McpPromptDescriptor",
    "McpResourceDescriptor",
    "McpServerSummary",
    "McpStdioClient",
    "McpToolDescriptor",
    "MessageBuffer",
    "StdioJsonRpc",
    "build_connecting_summaries",
    "cache_protocol",
    "create_mcp_backed_tools",
    "encode_content_length",
    "encode_newline_json",
    "format_prompt_result",
    "format_read_resource_result",
    "format_tool_call_result",
    "get_cached_protocol",
    "mcp_tool_name",
    "normalize_input_schema",
    "protocol_candidates",
    "sanitize_tool_segment",
    "summarize_server_endpoint",
]
