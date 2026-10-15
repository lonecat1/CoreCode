"""MCP 服务器状态汇总。

对应原版 `MiniCode/src/mcp-status.ts`（35 行）。原版把它放在 `src/` 顶层
（与 `mcp.ts` 平级）；本项目把 MCP 拆成了 `mcp/` 包，因此这里放在包内。

## 它解决什么

界面要显示「MCP 有几个连上了」，但调用方手上是一串**服务器快照**，每个带
自己的 `status` 与 `toolCount`。直接在使用处现算会出现两份统计口径（横幅一处、
状态行一处），迟早不一致。

因此汇总收敛成一个纯函数：输入快照列表，输出计数。纯函数也意味着它能脱离
网络与终端单独测试——这正是本模块存在的理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["McpStatusSummary", "summarize_mcp_servers"]

#: 服务器可能的状态。与原版 `McpServerSummary['status']` 一致。
STATUS_CONNECTED = "connected"
STATUS_CONNECTING = "connecting"
STATUS_ERROR = "error"


@dataclass(slots=True)
class McpStatusSummary:
    """MCP 服务器的计数汇总。"""

    total: int = 0
    connected: int = 0
    connecting: int = 0
    error: int = 0
    tool_count: int = 0

    def line(self) -> str:
        """一行摘要，用于状态行 / 横幅。

        格式照抄原版 `tui/chrome.ts:286-296`：

            mcp 2/3          ← 连上数/总数
            mcp 2/3 · 1 等待  ← 还有正在连接的
            mcp 2/3 · 1 失败  ← 还有出错的

        原版用彩色徽章分别显示 `mcp-wait` 与 `mcp-err`；行模式没有徽章，
        因此合并到一行里，用文字区分。
        """
        if not self.total:
            return ""
        text = f"mcp {self.connected}/{self.total}"
        if self.connecting:
            text += f" · {self.connecting} 等待"
        if self.error:
            text += f" · {self.error} 失败"
        return text

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "connected": self.connected,
            "connecting": self.connecting,
            "error": self.error,
            "toolCount": self.tool_count,
        }


def summarize_mcp_servers(servers: Any) -> McpStatusSummary:
    """把服务器快照列表汇总成计数。

    `servers` 是 `ToolRegistry.mcp_servers()` 的返回值——一个 dict 列表，
    每个含 `name` / `status` / `toolCount`。

    **未知状态既不计入 connected/connecting/error，也不报错**：它们仍然计入
    `total`。这样「总数对得上」这条性质永远成立，而未知状态会在
    `total - connected - connecting - error` 里显形，便于排查。
    """
    summary = McpStatusSummary()
    for server in servers or []:
        if not isinstance(server, dict):
            continue
        summary.total += 1
        tool_count = server.get("toolCount")
        if isinstance(tool_count, int) and not isinstance(tool_count, bool):
            summary.tool_count += max(0, tool_count)

        status = server.get("status")
        if status == STATUS_CONNECTED:
            summary.connected += 1
        elif status == STATUS_CONNECTING:
            summary.connecting += 1
        elif status == STATUS_ERROR:
            summary.error += 1

    return summary
