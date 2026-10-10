"""离线模型适配器。

用途有三个：跑测试、无网络时演示、以及**在没有 API key 的情况下验证整条循环**。
它按用户输入里的斜杠命令决定调用哪个工具，因此可以完整走通
`model -> tool -> model` 的多步路径。
"""

from __future__ import annotations

import time
from typing import Any

from ..types import AgentStep, ChatMessage, ToolCall


class MockModelAdapter:
    """把「用户说的话」直接映射成工具调用或最终答复。"""

    name = "mock"

    def next(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[Any] | None = None,
        signal: Any | None = None,
    ) -> AgentStep:
        # 优先级 1：如果上一轮刚有工具结果，把它转成最终答复。
        # 这个顺序很重要——否则「继续」类输入会被误当成新指令。
        last_call_name = self._last_tool_call_name(messages)
        last_result = self._last_tool_result(messages)
        if last_result is not None and last_call_name:
            return AgentStep(
                type="assistant",
                content=f"工具 {last_call_name} 返回：\n\n{last_result}",
                kind="final",
            )

        user_text = self._last_user_text(messages).strip()

        if user_text.startswith("/ls"):
            target = user_text[len("/ls") :].strip()
            return self._call("list_files", {"path": target} if target else {})

        if user_text.startswith("/read "):
            return self._call("read_file", {"path": user_text[len("/read ") :].strip()})

        if user_text.startswith("/write "):
            payload = user_text[len("/write ") :]
            path, separator, content = payload.partition("::")
            if not separator:
                return AgentStep(type="assistant", content="用法：/write <路径>::<内容>")
            return self._call(
                "write_file", {"path": path.strip(), "content": content}
            )

        if user_text.startswith("/ask "):
            return self._call("ask_user", {"question": user_text[len("/ask ") :].strip()})

        if user_text == "/boom":
            return self._call("explode", {})

        if user_text == "/empty":
            # 触发空响应路径，用于验证重试与兜底逻辑。
            return AgentStep(type="assistant", content="", kind="final")

        if user_text == "/progress":
            return AgentStep(type="assistant", content="正在分析…", kind="progress")

        return AgentStep(
            type="assistant",
            content=f"（mock 模型）收到指令：{user_text}",
            kind="final",
        )

    # -- 辅助 ---------------------------------------------------------------

    def _call(self, tool_name: str, tool_input: Any) -> AgentStep:
        return AgentStep(
            type="tool_calls",
            calls=[
                ToolCall(
                    id=f"call-{int(time.time() * 1000)}",
                    toolName=tool_name,
                    input=tool_input,
                )
            ],
        )

    @staticmethod
    def _last_user_text(messages: list[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    @staticmethod
    def _last_tool_result(messages: list[ChatMessage]) -> str | None:
        for message in reversed(messages):
            if message.get("role") == "tool_result":
                return str(message.get("content", ""))
        return None

    @staticmethod
    def _last_tool_call_name(messages: list[ChatMessage]) -> str | None:
        for message in reversed(messages):
            if message.get("role") == "assistant_tool_call":
                return str(message.get("toolName", ""))
        return None
