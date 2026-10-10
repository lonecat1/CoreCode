"""MiniCode —— 一个轻量的终端编码助手。

架构分层（自下而上）：

    types / tooling        核心类型与统一工具契约
    agent_loop             model -> tool -> model 主循环（全项目的核心）
    context/               四级上下文压缩与 provider-usage 计量
    session/               JSONL 会话树与恢复
    runtime/               单飞回合、Goal、Loop 编排
    tui/                   无框架 ANSI 终端界面
    tools/                 内置工具
    adapters/              模型后端（Anthropic / mock）

模块边界是单向的：`agent_loop` 不知道 TUI、会话与权限提示的存在，
宿主通过回调参数接收全部输出行为。
"""

__version__ = "0.1.0"
