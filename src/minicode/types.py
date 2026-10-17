"""核心类型定义。

设计取向（与 TypeScript 版保持一致，但按 Python 习惯重写）：

1. **消息用 dict，不用 dataclass。** 会话里流动的消息同时要满足三个消费方：
   模型 API 的 wire 格式、JSONL 会话日志的可序列化格式、以及 UI 的渲染输入。
   dict 让这三者的转换成本都为零。代价是失去静态字段校验——所以校验统一
   收敛到 `tooling.ToolDefinition.validator` 和本模块的构造辅助函数里。

2. **消息是判别联合，靠 `role` 区分。** 可辨识字段是 `role`，每个取值对应一组
   固定字段。这与 TypeScript 版的 `ChatMessage` 联合类型一一对应。

3. **`internal` 标记用户消息的来源。** Goal / Loop / continuation 注入的用户消息
   会带上它，UI 据此显示「continuing...」而不是把它当成用户手输的内容。

4. **provider usage 只挂在 assistant 侧消息上。** 它是上下文计量的真值来源，
   本地字符估算只用来补「最后一次 provider usage 之后的尾巴」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------

#: 可被模型看到并算入上下文的 assistant 侧消息角色。
#: provider usage 只会附加在这三类消息上。
USAGE_BEARING_ROLES = ("assistant", "assistant_progress", "assistant_tool_call")

#: 用户消息的来源标记。缺失表示用户手输。
InternalOrigin = Literal["goal", "loop", "continuation"]

#: 一条会话消息。字段含义按 `role` 解释。
ChatMessage = dict[str, Any]

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_ASSISTANT_PROGRESS = "assistant_progress"
ROLE_ASSISTANT_TOOL_CALL = "assistant_tool_call"
ROLE_TOOL_RESULT = "tool_result"
ROLE_CONTEXT_SUMMARY = "context_summary"
ROLE_SNIP_BOUNDARY = "snip_boundary"

#: 扩展思考块。必须在后续请求里原样回传，否则模型会丢失推理上下文。
ROLE_ASSISTANT_THINKING = "assistant_thinking"


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 模型交互
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCall:
    id: str
    toolName: str
    input: Any


@dataclass(slots=True)
class StepDiagnostics:
    """模型返回的原始诊断信息，用于区分「真的空响应」与「thinking 被截断」。"""

    stop_reason: str | None = None
    block_types: list[str] = field(default_factory=list)
    ignored_block_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentStep:
    """模型一次响应的解析结果。

    只有两种形态：给出最终/进度文本，或要求调用工具。
    """

    type: Literal["assistant", "tool_calls"]
    content: str = ""
    #: `progress` 表示模型显式声明「我还在干活」，循环会继续而不是收尾。
    kind: Literal["final", "progress"] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    content_kind: Literal["progress"] | None = None
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: StepDiagnostics | None = None
    usage: ProviderUsage | None = None


@dataclass(slots=True)
class ProviderUsage:
    """模型服务端上报的真实用量。上下文计量以它为准。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    source: str = "provider"


class ModelAdapter(Protocol):
    """模型适配器的唯一契约。任何后端（Anthropic、OpenAI、mock）都实现它。

    `signal` 是可选的取消信号。适配器应当在发起 I/O 前与等待过程中检查它，
    这样一次取消能同时打断网络请求与退避等待。

    `on_delta` 是**可选**的流式回调。支持流式的适配器应当在收到正文增量时
    调用它，让界面能逐字显示。不支持流式的适配器忽略这个参数即可——
    界面会退化为「等完整回答再一次性显示」，功能不受影响。

    这是刻意的设计：**流式是体验增强，不是功能前提**。与动画同理。
    """

    def next(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[Any] | None = None,
        signal: Any | None = None,
        on_delta: "Callable[[str], None] | None" = None,
    ) -> AgentStep: ...


# ---------------------------------------------------------------------------
# 回合结果
# ---------------------------------------------------------------------------

#: 一个回合的结束方式。见 agent_loop 的 finish() 调用点。
AgentTurnOutcome = Literal[
    "final",  # 模型交回控制权
    "awaiting_user",  # ask_user 挂起，等用户回答
    "controlled_stop",  # 工具主动要求结束（如 goal 完成）
    "failed",  # 致命工具错误或空响应兜底
    "max_steps",  # 达到步数上限
    "aborted",  # 被 AbortSignal 取消
]


@dataclass(slots=True)
class AgentTurnResult:
    messages: list[ChatMessage]
    outcome: AgentTurnOutcome
    tool_calls: int = 0
    error: str | None = None
