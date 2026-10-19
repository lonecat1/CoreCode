"""逐文件映射表：TS 的每个 `.ts` 文件对应本实现的哪个 `.py`。

## 为什么要有这个文件

`docs/parity.md` 开头写着核对方法的第一步是「列出 `MiniCode/src/**/*.ts` 全部
77 个文件」。但**那张表并不在文档里**——文档只有一张「需要补的模块」小表
（12 行）。于是「77 个文件全部有对应实现」这句话长期**没有可验证的依据**。

这个模块把表变成**代码**：

    MAPPING:  TS 相对路径 -> 本实现的相对路径（或一个状态标记）

跑 `python tools/file_map.py` 会验证：

    1. 覆盖度——`MAPPING` 的键集合 == TS 仓库里实际的 `.ts` 文件集合
       （多一个、少一个都报）
    2. 存在性——每个映射到的 `.py` 文件真的存在
    3. 反向覆盖——本实现的每个 `.py` 都至少被一个 TS 文件映射到（否则是
       凭空多出来的模块）

## 「合并」与「TUI」两种非一一对应

- **合并**：多个 TS 文件对应同一个 `.py`。例如 `tools/read-file.ts`、
  `tools/write-file.ts`、`tools/list-files.ts` 都在 `tools/files.py`——它们
  共享同一套路径解析与权限检查，拆成三个文件只是复制粘贴。
- **TUI**：`tty-app.ts` 与 `tui/*` 共 9 个文件、4,169 行，是用户明确放弃的
  全屏界面。`ui.ts` 是它们的**纯转出桶**（28 行 `export … from './tui/index.js'`），
  一并归入。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TS_ROOT = Path(r"D:\CoreCode\MiniCode\src")
PY_ROOT = REPO / "src" / "minicode"

#: 明确放弃的全屏 TUI。它们没有 `.py` 对应实现，理由记在 `docs/divergences.md`。
TUI_EXCLUDED: dict[str, str] = {
    "tty-app.ts": "全屏 TUI 主循环",
    "ui.ts": "TUI 的纯转出桶（28 行 export … from './tui/index.js'）",
    "tui/chrome.ts": "边框/面板绘制",
    "tui/index.ts": "TUI 导出汇总",
    "tui/input-parser.ts": "按键序列解析",
    "tui/input.ts": "输入行编辑",
    "tui/markdown.ts": "Markdown 渲染",
    "tui/screen.ts": "终端屏幕缓冲",
    "tui/transcript.ts": "会话记录视图",
    "tui/types.ts": "TUI 类型",
}

#: 本实现**独有**的模块：没有 TS 对应文件，但有明确理由。
#:
#: 分两类，都不是「多做」：
#:
#:   1. **行模式 UI**：原版的界面全在 `tui/*` 里（用户明确放弃的那批）。放弃全屏
#:      TUI 之后，行模式仍然需要渲染、宽度计算、颜色、启动提示——这些能力必须
#:      重新实现一遍，但不该塞进任何一个「TS 同名文件」里。
#:   2. **打包入口**：`python -m minicode` 需要 `__main__.py`；TS 没有这个需要。
PY_ONLY: dict[str, str] = {
    "__main__.py": "`python -m minicode` 的入口（TS 由 package.json 的 bin 承担）",
    "stats.py": (
        "回合/会话计时与统计。原版的 `getSessionStats` 只存在于 `tty-app.ts`"
        "（TUI-only），行模式要重新实现。"
    ),
    "ui/animate.py": "行模式的等待动画（原版在 tui/transcript.ts 里）",
    "ui/caps.py": "终端能力探测（颜色/交互）",
    "ui/console.py": "行模式的输出与提示（原版在 tty-app.ts 里）",
    "ui/render.py": "行模式渲染：横幅/面板/表格（原版在 tui/chrome.ts 里）",
    "ui/startup.py": (
        "启动时的欢迎语与就绪提示。原版这段在 `tty-app.ts` 的初始化流程里，"
        "与全屏终端状态耦合；行模式没有那套状态，得单独实现。"
    ),
    "ui/theme.py": (
        "颜色与样式常量。原版散在 `tui/chrome.ts` 的绘制代码里；行模式把它们"
        "集中成一张表，方便 `NO_COLOR` 与非 TTY 场景统一降级。"
    ),
    "ui/width.py": "显示宽度计算（东亚宽字符）",
}

#: TS 相对路径 → 本实现的相对路径（可多个，用 `+` 连接语义上等价）。
MAPPING: dict[str, tuple[str, ...]] = {
    # --- 顶层 ---
    "abort.ts": ("abort.py",),
    "agent-loop.ts": ("agent_loop.py",),
    "anthropic-adapter.ts": ("adapters/anthropic.py",),
    "background-tasks.ts": ("background_tasks.py",),
    "cli-commands.ts": ("cli_commands.py",),
    "config.ts": ("config.py",),
    "file-review.ts": ("file_review.py",),
    "history.ts": ("history.py",),
    "index.ts": ("cli.py", "host.py"),  # 入口 `main()` + 组合根 `SessionHost`
    "init.ts": ("repo_init.py",),
    "install.ts": ("install.py",),
    "local-tool-shortcuts.ts": ("local_tool_shortcuts.py",),
    "manage-cli.ts": ("manage_cli.py",),
    "mcp-status.ts": ("mcp/status.py",),
    "mcp.ts": (
        "mcp/bridge.py",       # 桥接与通用工具
        "mcp/jsonrpc.py",      # JSON-RPC 编解码
        "mcp/stdio_client.py", # stdio 传输（两种分帧）
        "mcp/http_client.py",  # HTTP 传输
    ),
    "memory.ts": ("memory.py",),
    "mock-model.ts": ("adapters/mock.py",),
    "permissions.ts": ("permissions.py",),
    "prompt.ts": ("prompt.py",),
    "session.ts": ("session/store.py",),
    "skills.ts": ("skills.py",),
    "tool.ts": ("tooling.py",),  # ToolDefinition / ToolRegistry
    "types.ts": ("types.py",),
    "workspace.ts": ("workspace.py",),

    # --- agents ---
    "agents/manager.ts": ("agents/manager.py",),
    "agents/types.ts": ("agents/types.py",),
    "agents/worker-prompt.ts": ("agents/worker_prompt.py",),

    # --- compact ---
    "compact/auto-compact.ts": ("context/compact.py",),
    "compact/compact.ts": ("context/pipeline.py",),
    "compact/constants.ts": ("context/constants.py",),
    "compact/context-collapse.ts": ("context/collapse.py",),
    "compact/manual-compact.ts": ("cli_commands.py", "context/compact.py"),
    "compact/microcompact.ts": ("context/microcompact.py",),
    "compact/prompt.ts": ("context/compact.py",),
    "compact/snipCompact.ts": ("context/snip.py",),

    # --- goal / plan / loop ---
    "goal/context.ts": ("runtime/context.py",),
    "goal/controller.ts": ("runtime/goal_controller.py",),
    "goal/manager.ts": ("runtime/goal.py",),
    "loop/scheduler.ts": ("runtime/loop.py",),
    "plan/context.ts": ("runtime/plan.py",),
    "plan/manager.ts": ("runtime/plan.py",),
    "plan/types.ts": ("runtime/plan.py",),

    # --- runtime ---
    "runtime/context.ts": ("runtime/context.py",),
    "runtime/session-runtime.ts": ("runtime/session.py",),
    "runtime/turn-runner.ts": ("runtime/turn.py",),

    # --- tools ---
    "tools/ask-user.ts": ("tools/ask_user.py",),
    "tools/edit-file.ts": ("tools/edit.py",),
    "tools/goal.ts": ("tools/goal.py",),
    "tools/grep-files.ts": ("tools/grep_files.py",),
    "tools/index.ts": ("tools/__init__.py",),
    "tools/list-files.ts": ("tools/files.py",),
    "tools/load-skill.ts": ("tools/load_skill.py",),
    "tools/modify-file.ts": ("tools/edit.py",),
    "tools/patch-file.ts": ("tools/edit.py",),
    "tools/plan.ts": ("tools/goal.py",),  # update_plan 与 Goal 工具同处
    "tools/read-file.ts": ("tools/files.py",),
    "tools/run-command.ts": ("tools/run_command.py",),
    "tools/sub-agents.ts": ("tools/subagents.py",),
    "tools/web-fetch.ts": ("tools/web.py",),
    "tools/web-search.ts": ("tools/web.py",),
    "tools/write-file.ts": ("tools/files.py",),

    # --- utils ---
    "utils/context.ts": ("context/constants.py", "context/tokens.py"),
    "utils/errors.ts": ("utils/web.py",),  # 见下：不照搬，见 NOTES
    "utils/model-context.ts": ("context/model_context.py",),
    "utils/token-estimator.ts": ("context/tokens.py",),
    "utils/tool-result-storage.ts": ("context/tool_result_storage.py",),
    "utils/web.ts": ("utils/web.py",),
}

#: 逐条说明「为什么是这样对应」——只有非显然的才写。
NOTES: dict[str, str] = {
    "index.ts": "原版命令行入口；本实现的 `main()` 在 `cli.py`。",
    "utils/errors.ts": (
        "**不照搬。** 原版的 `isEnoentError` 存在是因为 Node 没有"
        " `FileNotFoundError` 类；Python 用 `except FileNotFoundError` 即可，"
        "6 个调用点已逐一核对。`getErrorCode` 的等价物是 `utils/web.py` 里的"
        " `node_error_code()`。"
    ),
    "ui.ts": "纯转出桶，内容全在 `tui/`——随 TUI 一起放弃。",
    "compact/manual-compact.ts": "`/compact` 的处理函数在 `cli_commands.py`，压缩本身在 `context/compact.py`。",
    "utils/context.ts": "`COMPACTABLE_TOOLS` 在 `context/constants.py`，输出上限在 `context/tokens.py`。",
}


def python_files() -> set[str]:
    return {
        path.relative_to(PY_ROOT).as_posix()
        for path in PY_ROOT.rglob("*.py")
        # 包的 `__init__.py` 是导出汇总，不直接对应某个 TS 文件
        if path.name != "__init__.py"
    }


def ts_files() -> set[str]:
    return {
        path.relative_to(TS_ROOT).as_posix()
        for path in TS_ROOT.rglob("*.ts")
    }


def audit() -> list[str]:
    problems: list[str] = []
    mapped = set(MAPPING)
    excluded = set(TUI_EXCLUDED)

    if overlap := mapped & excluded:
        problems.append(f"同时出现在 MAPPING 与 TUI_EXCLUDED：{sorted(overlap)}")
    if both := (mapped | excluded) - ts_files():
        problems.append(f"表里有、TS 仓库里没有：{sorted(both)}")
    if missing := ts_files() - (mapped | excluded):
        problems.append(f"TS 有、表里没有（**漏网**）：{sorted(missing)}")

    for source, targets in sorted(MAPPING.items()):
        if not targets:
            problems.append(f"{source} 的映射为空")
        for target in targets:
            if not (PY_ROOT / target).exists():
                problems.append(f"{source} → {target}（文件不存在）")

    referenced = {target for targets in MAPPING.values() for target in targets}
    unreferenced = python_files() - referenced - set(PY_ONLY)
    if unreferenced:
        problems.append(f"本实现有、但既没映射也没登记为独有：{sorted(unreferenced)}")

    for name in sorted(PY_ONLY):
        if not (PY_ROOT / name).exists():
            problems.append(f"PY_ONLY 里登记了 {name}，但文件不存在")
        elif name in referenced:
            problems.append(f"{name} 既被映射又登记为独有——二选一")
    return problems


def main() -> int:
    problems = audit()
    total_ts = len(ts_files())
    print(
        f"TS {total_ts} 个文件 → 映射 {len(MAPPING)} + TUI 排除 {len(TUI_EXCLUDED)}"
        f" = {len(MAPPING) + len(TUI_EXCLUDED)}"
    )
    if not problems:
        print("✓ 覆盖度、存在性、反向覆盖全部通过")
        return 0
    print(f"发现 {len(problems)} 个问题：")
    for item in problems:
        print(f"  ✗ {item}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
