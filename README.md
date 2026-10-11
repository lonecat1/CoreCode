# MiniCode (Python)

一个轻量的终端编码助手：**Claude Code 式的 agent 架构，用 Python 从零重写。**

这是一个学习与自用导向的项目。它刻意保持小：核心循环可通读，模块边界单向，
运行时零第三方依赖（只用标准库）。

> 架构蓝本来自 [LiuMengxuan04/MiniCode](https://github.com/LiuMengxuan04/MiniCode)
> 的 TypeScript 实现。本仓库不是逐行翻译，而是按同一套架构取向重写，
> 并在少数地方做了有意的取舍（见「与 TypeScript 版的差异」）。

---

## 快速开始

```bash
# 跑测试（不需要任何第三方依赖）
python -m unittest discover -s tests -v

# 用离线 mock 模型跑通一个完整回合
python -c "
import sys; sys.path.insert(0, 'src')
from minicode.agent_loop import run_agent_turn
from minicode.adapters.mock import MockModelAdapter
from minicode.tools import create_default_registry
r = run_agent_turn(
    model=MockModelAdapter(), tools=create_default_registry(),
    messages=[{'role':'user','content':'/ls'}], cwd='.',
)
print(r.outcome)
for m in r.messages: print(' ', m['role'], '|', str(m.get('content'))[:60])
"
```

---

## 架构

```
                  宿主的全部输出行为都通过回调注入
                                │
   ┌────────────────────────────┴────────────────────────────┐
   │  agent_loop         model → tool → model 主循环（核心）  │
   │  ├─ types           消息联合类型、AgentStep、回合结局     │
   │  ├─ tooling         统一工具契约 + 注册表 + 控制位        │
   │  ├─ context/        四级上下文压缩 + provider-usage 计量  │
   │  ├─ session/        JSONL 会话树与恢复                    │
   │  ├─ runtime/        单飞回合、Goal、Loop 编排             │
   │  ├─ tui/            无框架 ANSI 界面、差分帧渲染          │
   │  ├─ tools/          内置工具（文件/搜索/命令/提问）        │
   │  └─ adapters/       模型后端（Anthropic / mock）          │
   └──────────────────────────────────────────────────────────┘
```

### 三条不可动摇的边界

1. **`agent_loop` 不知道 UI、会话与权限提示的存在。**
   它只依赖 `types` / `tooling` / `abort`。宿主通过 `TurnHooks` 与
   `ToolContext` 接收全部输出行为。因此同一个循环可以接到全屏 TUI、
   行模式 REPL、子 Agent、或测试里，而循环本身一行都不用改。

2. **工具通过「控制位」反向影响控制流，而不是靠循环里的特例分支。**
   `ToolResult` 带四个控制位：`await_user`（挂起等用户）、`stop`（受控结束）、
   `fatal`（致命）、`background_task`（后台句柄）。新增一种控制需求时，
   优先考虑扩展控制位，而不是在 `agent_loop` 里加 `if tool_name == ...`。

3. **写操作必须经过写前审查。**
   所有改文件的工具都收敛到 `file_review.apply_reviewed_file_change()`：
   读旧内容 → 无变化则跳过 → 生成 unified diff → 请求批准 → 落盘。
   模型无法在不展示 diff 的情况下改动文件。

### 一个回合内部发生了什么

```
for step in range(max_steps):
    ① 上下文工程     context_pipeline.prepare() → (真实消息, 模型可见投影)
    ② 请求模型       model.next(模型可见投影, tools=...)
    ③ 分类响应
       ├─ 显式 <progress>       → 记进度、注入续跑提示、继续循环
       ├─ 空响应 + thinking 截断 → 注入续跑提示（上限 3 次）
       ├─ 普通空响应             → 注入续跑提示（上限 2 次）
       ├─ 空响应且重试用尽       → 带诊断信息兜底收尾，回合 failed
       └─ 正常文本               → 回合 final
    ④ 若有工具调用 → 先为每个调用占位（保证 tool_use/tool_result 严格配对）
                    → 逐个执行并就地回填结果
                    → 按控制位决定：继续循环 / 挂起 / 受控结束 / 失败
```

`③` 这一层是整个循环健壮性的关键：它明确区分「模型真的完成了」与
「模型话说一半停了」，后者是这个循环能不能连续干活的分水岭。

---

## 实现进度

| 模块 | 状态 |
|---|---|
| 核心循环 `agent_loop` | ✅ 完成，16 项行为测试通过 |
| 工具契约 `tooling` | ✅ 完成（控制位、注册表、子集隔离） |
| 取消信号 `abort` | ✅ 完成（贯穿模型/工具/审批） |
| 写前审查 `file_review` | ✅ 完成 |
| 路径围栏 `workspace` | ✅ 完成 |
| 权限管理器 `permissions` | ✅ 完成（三入口 + 四级授权 + 持久化） |
| 内置工具 | ✅ 文件读写 · grep（rg + 回退）· web_fetch / web_search · ask_user |
| 离线 mock + Anthropic 适配器 | ✅ 完成（含 thinking 块原样回传） |
| 上下文工程 `context/` | ✅ 完成（四级压缩 + 计量 + 大结果落盘） |
| 会话持久化 `session/` | ✅ 完成（JSONL 树 + 边界 + fork/resume） |
| 运行时编排 `runtime/` | ✅ 完成（Plan / Goal / Loop + 四道互斥闸门） |
| 子 Agent `agents/` | ✅ 完成（隔离上下文 + 受限工具 + ≤3 并发） |
| 项目记忆 `memory` + 系统提示 `prompt` | ✅ 完成 |
| CLI 入口 `cli` | ✅ 完成（行模式 REPL + 单次提问 + 本地命令） |
| Skills | ⬜ 待实现 |
| MCP | ⬜ 待实现 |
| 差分帧渲染 TUI | ⬜ 待实现 |

> **测试隔离**：`tests/__init__.py` 在任何 `minicode` 导入之前把家目录钉在仓库内
> （`tests/_tmp/mini-home`），并做一次 fail-closed 断言。运行时的路径解析走
> `config.projects_dir()` 这类**动态**访问器，因此测试可以逐用例覆盖
> `MINICODE_HOME`。模块级的 `PROJECTS_DIR` 只是展示用快照，不要用它读写文件。

---

## 开发工具

不需要任何第三方包，全部零依赖：

```bash
python tools/check_imports.py    # 静态导入检查（模块/名字是否存在、相对层级是否越界）
python tools/mutation_check.py   # 变异检查：故意破坏保证，验证测试真的会变红
python tools/self_check.py       # 环境自检（依赖 + 导入 + 测试）
```

`mutation_check.py` 的价值在于：绿色测试只说明测试通过，不说明测试**有效**。
它已经抓出过 2 个「形同虚设」的测试。

`self_check.py` 刻意是独立脚本而不是 CLI 子命令——早先的 `minicode --agent`
会跑测试，而测试里又有一个用 `--agent` 的用例，于是无限递归并**挂住**。

---

## 与 TypeScript 版的差异

这些是**有意的**取舍，不是移植遗漏：

| 主题 | TypeScript 版 | 本仓库 | 原因 |
|---|---|---|---|
| 消息表示 | 判别联合类型 | `dict` + 构造辅助函数 | 消息同时要喂给模型 wire 格式、JSONL 日志、UI 渲染；dict 让三者转换成本为零。校验收敛到工具 validator 与构造函数 |
| 上下文压缩 | 四套独立机制 | 合并进 `ContextPipeline` | 循环只需知道「给我真实消息和模型可见投影」这一个接缝 |
| 取消传播 | `AbortSignal`（平台原生） | 自建 `AbortSignal`（`threading.Event`） | 工具执行多为同步阻塞调用，全链路 async 化得不偿失 |
| UI 框架 | 无（手写 ANSI） | 无（手写 ANSI） | 保持一致 |

---

## 目录

```
src/minicode/
  types.py          消息类型、AgentStep、回合结局
  tooling.py        工具契约、注册表、控制位
  abort.py          贯穿式取消信号
  agent_loop.py     model → tool → model 主循环
  file_review.py    写前 diff 审查
  workspace.py      路径围栏
  adapters/         模型后端
  tools/            内置工具
docs/               逆向提取的行为规格
tests/              行为测试
```
