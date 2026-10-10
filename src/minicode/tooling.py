"""统一工具契约与注册表。

这是全项目的扩展点。新增一个能力 = 实现一个 `ToolDefinition` 并注册进
`ToolRegistry`，不需要改动 agent loop 一行代码。

TypeScript 版的契约只有 5 个字段（name / description / inputSchema / schema / run），
这里保持同样的最小面：

    ToolDefinition(name, description, input_schema, validator, run)

两个 schema 字段的职责不同，不要合并：
  - `input_schema` 是给**模型**看的 JSON Schema，决定模型怎么填参数；
  - `validator` 是**本地**校验，把不可信的模型输出变成可信的 Python 值。

`ToolResult` 除 ok/output 外还有四个**控制位**，它们让工具能够反向影响 agent loop
的控制流，而不需要在循环里写特例分支：

    await_user      挂起回合等用户回答（ask_user）
    stop            受控结束回合（如 goal 报告已完成）
    fatal           致命错误，配合 stop_on_fatal_tool_error 终止回合
    background_task 返回一个后台任务句柄，供 UI 观测
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .types import ChatMessage

# ---------------------------------------------------------------------------
# 结果与控制位
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackgroundTaskResult:
    """后台 shell 任务的句柄。注意：它刻意不携带 stdout/stderr。"""

    task_id: str
    command: str
    pid: int
    status: str  # running | completed | failed
    started_at: float


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str
    await_user: bool = False
    stop: bool = False
    fatal: bool = False
    background_task: BackgroundTaskResult | None = None

    @classmethod
    def success(cls, output: str, **control: Any) -> ToolResult:
        return cls(ok=True, output=output, **control)

    @classmethod
    def failure(cls, output: str, **control: Any) -> ToolResult:
        return cls(ok=False, output=output, **control)


# ---------------------------------------------------------------------------
# 调用上下文
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolContext:
    """一次工具调用携带的全部环境。

    `permissions` 与 `signal` 都是可选的：子 Agent 只拿到只读工具子集，
    没有权限管理器，因此路径围栏退化为纯工作区检查。
    """

    cwd: str
    permissions: Any | None = None
    signal: Any | None = None  # threading.Event | None，见 runtime.cancellation


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


class ToolRunner(Protocol):
    def __call__(self, data: Any, context: ToolContext) -> ToolResult: ...


@dataclass(slots=True)
class ToolDefinition:
    """一个可被模型调用的能力。"""

    name: str
    description: str
    #: 给模型的 JSON Schema。
    input_schema: dict[str, Any]
    #: 本地校验：把模型给的原始 dict 变成可信值；不合法时抛异常。
    validator: Callable[[Any], Any]
    #: 实际执行体。
    run: ToolRunner


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    validator: Callable[[Any], Any] | None = None,
) -> Callable[[ToolRunner], ToolDefinition]:
    """把一个函数装饰成 ToolDefinition。

    用法::

        @tool(name="read_file", description="...")
        def read_file(data: dict, context: ToolContext) -> ToolResult:
            ...
    """

    def decorate(runner: ToolRunner) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema
            or {"type": "object", "properties": {}, "additionalProperties": True},
            validator=validator or (lambda value: value),
            run=runner,
        )

    return decorate


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolRegistry:
    """工具集合。子 Agent 的工具隔离就是靠 `subset()` 实现的。"""

    _tools: list[ToolDefinition] = field(default_factory=list)
    _skills: list[dict[str, Any]] = field(default_factory=list)
    _mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    _disposers: list[Callable[[], None]] = field(default_factory=list)

    # -- 查询 ---------------------------------------------------------------

    def list(self) -> list[ToolDefinition]:
        return list(self._tools)

    def model_tools(self) -> list[dict[str, Any]]:
        """转成模型 API 需要的工具声明列表。"""
        return [
            {
                "name": entry.name,
                "description": entry.description,
                "input_schema": entry.input_schema,
            }
            for entry in self._tools
        ]

    def find(self, name: str) -> ToolDefinition | None:
        return next((entry for entry in self._tools if entry.name == name), None)

    def skills(self) -> list[dict[str, Any]]:
        return list(self._skills)

    def mcp_servers(self) -> list[dict[str, Any]]:
        return list(self._mcp_servers)

    # -- 组合 ---------------------------------------------------------------

    def subset(self, names: list[str] | tuple[str, ...]) -> ToolRegistry:
        """只保留指定名字的工具。这是子 Agent 工具隔离的实现手段。"""
        allowed = set(names)
        return ToolRegistry([entry for entry in self._tools if entry.name in allowed])

    def add_tools(self, tools: list[ToolDefinition]) -> None:
        """追加工具，重名跳过（MCP 工具是异步注入的，可能晚于内置工具到达）。"""
        existing = {entry.name for entry in self._tools}
        for entry in tools:
            if entry.name in existing:
                continue
            self._tools.append(entry)
            existing.add(entry.name)

    def set_mcp_servers(self, servers: list[dict[str, Any]]) -> None:
        self._mcp_servers = list(servers)

    def set_skills(self, skills: list[dict[str, Any]]) -> None:
        self._skills = list(skills)

    def add_disposer(self, disposer: Callable[[], None]) -> None:
        self._disposers.append(disposer)

    # -- 执行 ---------------------------------------------------------------

    def execute(self, name: str, data: Any, context: ToolContext) -> ToolResult:
        """校验并执行一个工具。这里**不抛异常**：所有失败都变成 ToolResult。"""
        entry = self.find(name)
        if entry is None:
            return ToolResult.failure(f"Unknown tool: {name}")

        try:
            parsed = entry.validator(data)
        except Exception as error:  # noqa: BLE001 - 校验失败要变成模型可读的文本
            return ToolResult.failure(f"Invalid input for {name}: {error}")

        try:
            if context.signal is not None and getattr(context.signal, "is_set", lambda: False)():
                return ToolResult.failure("Cancelled before execution.", fatal=True)
            return entry.run(parsed, context)
        except KeyboardInterrupt:
            raise
        except Exception as error:  # noqa: BLE001 - 工具崩溃不能拖垮整个回合
            return ToolResult.failure(
                f"{type(error).__name__}: {error}", fatal=True
            )

    def dispose(self) -> None:
        for disposer in self._disposers:
            try:
                disposer()
            except Exception:  # noqa: BLE001 - 清理失败不应掩盖真实错误
                pass
        self._disposers.clear()
