"""模型后端适配器。

每个适配器只需要实现一个方法：

    next(messages, *, tools=None, signal=None) -> AgentStep

新增一个后端不需要改动 agent loop 一行代码。
"""

from .anthropic import AnthropicModelAdapter
from .mock import MockModelAdapter

__all__ = ["AnthropicModelAdapter", "MockModelAdapter"]
