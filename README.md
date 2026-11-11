# CoreCode

一个轻量的终端编码助手：**Claude Code 式的 agent 架构，用 Python 实现。**

这是一个学习与自用导向的项目。它刻意保持小：核心循环可通读，模块边界单向，
运行时零第三方依赖（只用标准库）。

---

## 界面

**行模式 REPL，仿 Claude Code 的观感。** 这是默认也是唯一的交互方式：

```bash
corecode
```

```
╭────────────────────────────────────────────────────────────────────────────╮
│ CoreCode  终端编码助手                                                     │
│ 模型  qwen3.8-27b                                                          │
│ 会话  a1b2c3d4                                                             │
│ 目录  D:\CoreCode\corecode                                              │
│ 工具  20 个（6 只读 · 11 受限 coder）                                      │
│ 技能  2 个                                                                 │
╰────────────────────────────────────────────────────────────────────────────╯
输入 /help 查看命令 · /exit 退出

> 帮我看看 pyproject.toml 里声明了哪些依赖

⏺ read_file
  ⎿ PATH: pyproject.toml
这个项目的运行时依赖是空的——它在 pyproject.toml 里把 dependencies
显式写成了 []。

> 再检查一下有没有语法错误

⏺ run_command
  ⎿ command not allowed: ruff
· 改用 compileall 做语法检查
⏺ run_command
  ⎿ 64 个模块全部编译通过
所有模块语法正常。
```

视觉记号沿用 Claude Code：`⏺` 起头的工具调用、`⎿` 承接结果、`>` 提示符、
圆角框。模型反问时用独立的方框突出，并明确提示「请直接输入你的回答」。

### 流式输出

回答**逐字显示**，不必等模型说完：

```
⏺ read_file
  ⎿ PATH: pyproject.toml
该项目的版本号是 0.1.0。      ← 边收边打
  ⏱ 2.4s · 1 次工具调用        ← 回合统计
```

实现走 SSE（`stream: true`），把增量事件重建成与非流式等价的结构，再走
同一条解析路径——这样两种模式的行为差异被压到最小。

**流式失败会自动退回非流式**：某些服务商或代理不支持 SSE，或连接中途中断。
此时静默退回一次性请求，用户看不到逐字效果，但**功能完全不受影响**。

流式引入两处需要显式处理的地方，代码里都写明了理由：

- **协议标记要剥掉。** 模型用 `<final>` / `<progress>` 声明文本性质，那是
  给程序看的。但流式是边收边打的——因此先缓冲开头若干字符，**一旦能确定
  「不是标记」就立刻放行**。无条件缓冲会让短增量卡顿，反而抵消流式的意义。
- **内容会重复。** 同一段文本既走流式回调，又走 `hooks.progress()` /
  `hooks.assistant()`。界面显式去重，否则用户会看到两遍。

### 回合统计

每个回合结束后显示耗时与工具调用次数：

```
  ⏱ 8.1s · 3 次工具调用
```

`/status` 里还有会话累计（回合数、总耗时、平均耗时）。计时从「用户提交」
算到「模型交回控制权」，**不含用户自己思考的时间**——两者混在一起得出的
数字没有意义。

### 为什么是行模式，不是全屏 TUI

Claude Code 的默认界面本身就是**美化过的行模式 REPL**，不是全屏 TUI。
这个选择决定了可靠性——全屏 TUI 依赖三样与终端实现强耦合的能力：

| 依赖 | 在什么情况下失效 |
|---|---|
| raw mode | Windows 用 `msvcrt` / POSIX 用 `termios`，行为不一致 |
| 非阻塞读键 | **Git Bash / MSYS2 下 `msvcrt.kbhit()` 永远返回 False** |
| 光标定位与原地重绘 | 需要精确预测行数与宽度，任一偏差就错乱 |

本项目为此**反复失败过多次**，最后的表现始终是同一句话：**「界面画出来了，
但按键完全没反应」**。根因是这些能力在你的实际终端里不成立，而程序无从
得知——它只能看到「界面正常显示了」。

行模式把这三样依赖**全部消除**，只剩「往 stdout 写文本」：

- 在任何终端里都能工作（Windows 控制台、Windows Terminal、Git Bash、SSH…）
- 在管道、重定向、CI 里也安全——**非终端时自动关闭颜色**，不会把转义
  序列写进文件
- 历史由终端的滚动缓冲负责，用户能用自己熟悉的滚轮/搜索
- 可以被完整测试（纯字符串变换，不需要伪终端）

**这个取舍的代价**：没有鼠标交互、没有固定底部的输入框、没有转圈动画。
换来的是一句话——**它不会假装能用，然后什么都不响应。**

---

## 快速开始

```bash
# 跑测试（不需要任何第三方依赖）
python -m unittest discover -s tests -v

# 用离线 mock 模型跑通一个完整回合
python -c "
import sys; sys.path.insert(0, 'src')
from corecode.agent_loop import run_agent_turn
from corecode.adapters.mock import MockModelAdapter
from corecode.tools import create_default_registry
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
   │  ├─ evolution/      自进化记忆：反思/提炼/索引/召回/Skill │
   │  ├─ session/        JSONL 会话树与恢复                    │
   │  ├─ runtime/        单飞回合、Goal、Loop 编排             │
   │  ├─ ui/             无框架 ANSI 界面（行模式）            │
   │  ├─ tools/          内置工具（文件/搜索/命令/提问）        │
   │  ├─ agents/         Fork / Worktree / Team 中心化协作     │
   │  └─ adapters/       模型后端（Anthropic / mock）          │
   └──────────────────────────────────────────────────────────┘
```

配套的**命令行层**（不属于循环）：`cli` 行模式 REPL、`cli_commands` 斜杠命令、
`manage_cli` 子命令（`corecode mcp` / `corecode skills`）、`install` 安装器。

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
    ② 运行时/记忆注入 Goal/Loop/Plan + 按需召回记忆资产
    ③ 请求模型       model.next(模型可见投影, tools=...)
    ④ 分类响应
       ├─ 显式 <progress>       → 记进度、注入续跑提示、继续循环
       ├─ 空响应 + thinking 截断 → 注入续跑提示（上限 3 次）
       ├─ 普通空响应             → 注入续跑提示（上限 2 次）
       ├─ 空响应且重试用尽       → 带诊断信息兜底收尾，回合 failed
       └─ 正常文本               → 回合 final
    ⑤ 若有工具调用 → 先为每个调用占位（保证 tool_use/tool_result 严格配对）
                    → 逐个执行并就地回填结果
                    → 按控制位决定：继续循环 / 挂起 / 受控结束 / 失败
```

`④` 这一层是整个循环健壮性的关键：它明确区分「模型真的完成了」与
「模型话说一半停了」，后者是这个循环能不能连续干活的分水岭。

---

## 自进化记忆沉淀

每个完成或失败的回合结束后，系统会从真实执行轨迹中反思并提炼四类资产：

| 类型 | 内容 | 复用方式 |
|---|---|---|
| 情景记忆 | 请求、动作、错误、结局 | 相似任务的历史上下文 |
| 程序经验 | 成功的工具调用序列 | 重复出现后生长为 Skill |
| 修复经验 | 错误签名与后续恢复动作 | 工具再次报错时优先召回 |
| 用户画像 | 明确的长期偏好、约束、身份 | 跨会话、跨项目注入系统上下文 |

资产保存在 `CORECODE_HOME/evolved-memory/`，使用增量 BM25 索引检索。检索发生在
每一次模型调用之前，只修改模型可见投影，不污染真实会话。重复成功的流程写到
项目 `.corecode/skills/evolved-*/SKILL.md`，并在当前会话立即刷新到技能目录。
完整机制见 [`docs/spec-evolution.md`](docs/spec-evolution.md)。

---

## 中心化多 Agent 协作

主 Agent 始终是唯一的计划者、审批者、质量门禁和代码合并者。子 Agent 只能通过
根 Agent 可见的 Tool Call 创建，没有 Agent 间消息通道，也不能继续创建 Agent。

| 模式 | 工作方式 | 写权限 |
|---|---|---|
| `research` | 在主工作区只读调研，交回最短结论 | 无 |
| `fork` | 接收主对话的不可变文本快照，并行分析 | 无 |
| `worktree` | 在独立 Git worktree 内编码与验证 | 仅工作树内 |
| `team` | 用 `spawn_team` 创建 1–3 个扁平角色分组 | 按成员模式 |

Worktree 子 Agent 必须提供 `acceptance` 验收标准，只能使用受限的验证命令；
它不能 commit、push、reset 或操作工作树外路径。完成后主 Agent 必须先调用
`inspect_agent_changes` 审阅 patch，再调用 `apply_agent_changes`。后者会请求
用户审批，并在 `git apply --check` 通过后才写入根工作区，冲突不会留下半合并状态。
主 Agent 合并后仍需在根工作区运行最终验证。

完整设计见 [`docs/spec-agents.md`](docs/spec-agents.md)。

---

## Skill 分层路由

Skill 不再全量列表注入。系统提示只保留分类目录摘要，每次模型调用前按当前任务
执行两阶段路由：

1. **粗召回**：意图、标签、分类和词法重叠，从全部 Skill 中最多保留 32 个；
2. **精排**：结合 tags、intents、适用边界、示例、priority，去重叠后只注入
   top-3 技能卡片。

Skill 支持可选 front matter：`category`、`tags`、`intents`、`when_to_use`、
`when_not_to_use`、`examples`、`priority`、`capabilities`。旧 Skill 会自动从
正文推导最小元信息。完整正文仍由 `load_skill` 按需读取，路由上下文默认限制在
2,000 字符内。

自进化生成的 Skill 现在自带可路由元信息；宿主刷新技能目录时会同步刷新路由
缓存，根 Agent 与两类子 Agent 都能收到任务相关的 Skill 卡片。

完整设计见 [`docs/spec-skill-routing.md`](docs/spec-skill-routing.md)。

---

## 实现进度

<!-- BEGIN GENERATED STATS -->

| 项目 | 数量 |
|---|---|
| 未映射 | 0 |
| Python 模块 | 108 |
| 模块级常量 | 334 |
| 模块级函数/类 | 681 |
| 变异用例 | 340 |
| 单元测试 | 1661 |

<!-- END GENERATED STATS -->

| 模块 | 状态 |
|---|---|
| 核心循环 `agent_loop` | ✅ 完成，16 项行为测试通过 |
| 工具契约 `tooling` | ✅ 完成（控制位、注册表、子集隔离） |
| 取消信号 `abort` | ✅ 完成（贯穿模型/工具/审批） |
| 写前审查 `file_review` | ✅ 完成 |
| 路径围栏 `workspace` | ✅ 完成 |
| 权限管理器 `permissions` | ✅ 完成（三入口 + 四级授权 + 持久化） |
| 内置工具 | ✅ **13 / 13** + 7 个子 Agent 管理工具（根注册表 20） |
| 离线 mock + Anthropic 适配器 | ✅ 完成（含 thinking 块原样回传） |
| 上下文工程 `context/` | ✅ 完成（四级压缩 + 计量 + 大结果落盘） |
| 会话持久化 `session/` | ✅ 完成（JSONL 树 + 边界 + fork/resume） |
| 运行时编排 `runtime/` | ✅ 完成（Plan / Goal / Loop + 四道互斥闸门） |
| 中心化多 Agent `agents/` | ✅ 完成（Fork / Worktree / Agent Team + 审阅合并门禁） |
| 项目记忆 `memory` + 系统提示 `prompt` | ✅ 完成 |
| 自进化记忆沉淀 `evolution/` | ✅ 完成（执行-反思-提炼-分类储-索引-按需复用 + Skill 生长） |
| Skill 分层路由 | ✅ 完成（目录分层 + 意图/标签/边界/示例 + 两阶段召回精排） |
| CLI 入口 `cli` | ✅ 完成（行模式 REPL + 单次提问 + 本地命令） |
| Skills `skills` | ✅ 完成（四层发现 + `load_skill` + 安装管理） |
| MCP `mcp/` | ✅ 完成（stdio 双分帧协商 + HTTP + 工具桥接） |
| 后台任务 `background_tasks` | ✅ 完成（注册表 + PID 存活探测） |
| 差分帧渲染 TUI `tui/` | 🔶 **已移除**——用户明确放弃全屏 TUI，改为行模式（见下节） |

**对等状态**：**没有缺口了**。

| 维度 | 状态 |
|---|---|
| 工具（默认注册表） | **13 / 13** ✅ |
| 工具（含 runtime 追加的 goal 工具） | **15 / 15** ✅ |
| 命令 | **29 / 29** ✅ |
| `src/` 各模块 | 全部覆盖，或记为有意差异 ✅ |
| 完全缺失 / 能力缩水 | **0 / 0** |

每个 ✅ 都有对应测试兜底：命令数量与能力集合由 `tools/check_docs.py` 校验，
文档与实测不一致会直接变红。

> 早先这张表把工具层写成「✅ 完成」而在括号里省略了缺失项，是**表述误导**。
> 后来它又反向漂移了一次：所有项目都已补齐，表里却仍列着一堆 ❌。
> 两次是同一个病——**文档没有跟着代码走**。现在的做法是：缺口一补齐就改这张表。

> **测试隔离**：`tests/__init__.py` 在任何 `corecode` 导入之前把家目录钉在仓库内
> （`tests/_tmp/corecode-home`），并做一次 fail-closed 断言。运行时的路径解析走
> `config.projects_dir()` 这类**动态**访问器，因此测试可以逐用例覆盖
> `CORECODE_HOME`。模块级的 `PROJECTS_DIR` 只是展示用快照，不要用它读写文件。

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

`self_check.py` 刻意是独立脚本而不是 CLI 子命令——早先的 `corecode --agent`
会跑测试，而测试里又有一个用 `--agent` 的用例，于是无限递归并**挂住**。

`mutation_check.py` 还会**自愈**：变异前先把原文另存为 `.mutation-backup`，
下次运行开头检查并恢复。原因是脚本被中途杀死时 `finally` 不会执行，变异会
留在源码里——这真实发生过一次，导致后续测试以难以理解的方式失败。
空操作变异（替换后内容不变）会被报成 `ERROR` 而不是误报「测试无效」。

---

## MCP 接入

在 `~/.corecode/mcp.json`、项目根的 `.mcp.json`，或 `settings.json` 的
`mcpServers` 中配置（后者优先级最高）：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer $MY_TOKEN" }
    }
  }
}
```

远端工具会以 `mcp__<server>__<tool>` 的名字出现。用 `/mcp` 查看连接状态。

**两种分帧会自动协商**：MCP 规范写的是 `Content-Length`（LSP 风格），但不少
实现只支持 NDJSON（一行一个 JSON）。客户端先试前者；若 1.2 秒探测超时，会
用**同一种分帧**以正常超时重试（服务器可能只是冷启动慢），仍然失败才换另
一种。协商结果缓存到 `mcp-protocol-cache.json`，下次直接用对的分帧。

`load_skill` 与 MCP 工具**不需要 API key**——MCP 配置与模型凭据完全解耦。

---

## 配置

写入 `~/.corecode/settings.json`：

```json
{
  "model": "qwen3.8-max-0902",
  "env": {
    "ANTHROPIC_BASE_URL": "https://dashscope.aliyuncs.com/apps/anthropic",
    "ANTHROPIC_API_KEY": "sk-...",
    "CORECODE_THINKING": "disabled"
  },
  "maxOutputTokens": 16000
}
```

**优先级：`settings.json` 的 `env` > 进程环境变量。** 配置文件优先是刻意的
（见 [divergences.md](docs/divergences.md) D25）：机器上往往早已存在
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（为别的工具配的），若让它们压过
本工具的配置文件，你写在这里的端点与密钥会被静默作废。凭据成对决策（D26）——
文件提供了任一凭据时就不再从环境回退，避免「端点来自 A、密钥来自 B」。

`CORECODE_*` 前缀是本工具专有的，**始终**以进程环境为准，便于临时覆盖：

```bash
CORECODE_MODEL=claude-sonnet-4 corecode
```

### 用阿里云百炼（Model Studio）

百炼提供 Anthropic 兼容端点，可直接使用：

| 区域 | `ANTHROPIC_BASE_URL` |
|---|---|
| 中国（北京） | `https://dashscope.aliyuncs.com/apps/anthropic` |
| 新加坡 | `https://dashscope-intl.aliyuncs.com/apps/anthropic` |
| 中国（香港） | `https://cn-hongkong.dashscope.aliyuncs.com/apps/anthropic` |

端点也支持工作空间专属域名（`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/apps/anthropic`），
按[官方文档](https://www.alibabacloud.com/help/en/model-studio/anthropic-api-messages)填写即可。

两点注意：

1. **不要**把 `base_url` 写到 `/v1` 结尾——客户端会自行追加 `/v1/messages`，
   写成 `.../apps/anthropic/v1` 会产生 `/v1/v1/messages` 并返回 404。
2. **必须设 `"CORECODE_THINKING": "disabled"`**（或 settings 里的 `"thinking": "disabled"`）。
   百炼的 `qwen3.8-max` 系列默认开启思考，而思考 token 与正文共享 `max_tokens`；
   预算被思考吃光时模型返回**空内容**。症状是「模型不回答」，根因却是 token 预算。

如果 `base_url` 指向别的服务商而模型名是 `anthropic` 系的，或反之，都会失败——
端点与模型必须来自同一家。

---

## 全屏界面（内联实时区域）

```bash
corecode --tui
```

外观仿 Claude Code：圆角输入框、`⏺` 起头的工具调用、`⎿` 承接结果、
底部带转圈动画的状态行。

```
CoreCode  session abc123  tools 12
                                        ← 转录区（不足时补空行，输入框始终贴底）
> 帮我看看这个文件

⏺ read_file
  ⎿ 读取了 pyproject.toml
这个项目的名字是 corecode。

╭──────────────────────────────────────╮
│ > 下一个问题                          │
╰──────────────────────────────────────╯
⠋ 思考中…  ·  qwen3.8-27b  ·  ctx 12%
```

### 为什么是「内联」而不是「全屏」

早先的实现用**备用屏幕 + 绝对行定位**
（`ESC[<行>;1H`）。它有一个致命脆弱点：**要求「渲染行数」恰好等于「终端行数」**。
一旦不等——哪怕只多一行——写入就会触发滚动，屏幕整体上移，之后所有绝对行号
全部错位，画面必然错乱。

实测中它就是这样坏掉的：空会话首屏 25 行 > 终端 24 行。

现在改为内联模式：只在自占的几行里原地重绘，**不需要知道终端总行数**。
历史留在正常滚动缓冲里（仍可向上滚动查看），退出后终端状态天然干净。
两个不变式取代了那个脆弱前提：

1. **每行可见宽度 ≤ 终端列数**（`clamp_width` 强制）。超宽会让终端自动折行，
   而我们的行数记账仍按一行算，光标就会移错位置。
2. **区域高度 ≤ 终端行数 - 1**。留一行给退出后的 shell 提示符。

### 分层

| 文件 | 内容 |
|---|---|
| `tui/theme.py` | 配色与字形（遵守 `NO_COLOR`） |
| `tui/width.py` | 显示宽度（CJK/Emoji 2 列、组合符 0 列） |
| `tui/screen.py` | **内联实时区域**：逐行比对，只重绘变化的行 |
| `tui/app.py` | 状态 · 转录条目 · 布局组装 · 按键处理（全纯函数） |
| `tui/terminal.py` | 平台原生取键 + ANSI 能力探测 |
| `tui/runner.py` | 交互循环（回合跑在后台线程） |

### Windows 上的两个关键点

**① 必须启用 VT 处理。** Windows 10 起支持 ANSI，但**默认关闭**。不启用的话
所有转义序列会以 `←[38;5;209m` 这类形式原样打印——看起来就像界面彻底坏了。
启动时自动调用 `SetConsoleMode(..., ENABLE_VIRTUAL_TERMINAL_PROCESSING)`；
失败则**降级为纯文本输出**（不发任何转义序列），而不是打印乱码。

**② HANDLE 必须声明为指针宽度。** `ctypes` 默认 `restype` 是 `c_int`（32 位），
会把 64 位的控制台句柄截断，导致 `SetConsoleMode` 静默失败。

### 按键无反应怎么办

如果界面显示正常但**按键完全没反应**，先跑诊断脚本：

```bash
python tools/tui_doctor.py
```

它会逐项检查输入通道并给出结论。最常见的两个原因：

**① stdin 不是真正的 Windows 控制台。** 在 **Git Bash / MSYS2 / Cygwin**
下，`isatty()` 返回真，但标准句柄不是控制台，`msvcrt.kbhit()` 会**永远返回
False**——界面画出来了，按键永远读不到。解决办法：换用 **Windows Terminal
或 PowerShell**，或用行模式 `corecode`。

**② stdout 被重定向。** `--tui` 需要 stdin 与 stdout 都是终端；管道或重定向
下会明确提示并回退到行模式。

程序自己也会检测：启动时做一次输入通道自检，可疑就把原因显示在界面上；
若自检可疑且 **10 秒内一个按键都没收到**，会主动退回行模式——不会让你
对着一个不响应的界面反复重试。

### 输入层为什么要用「读取线程」

早先的实现按平台选非阻塞接口（Windows 用 `msvcrt.kbhit()`，POSIX 用
`select()`）。两者都有一个致命前提：**stdin 必须是那类接口认得的终端**。
在 Git Bash 下这个前提不成立，于是按键全失效。

现在改成**一条后台线程做阻塞读取、主线程从队列取**：不依赖任何「非阻塞
探测」接口，因此对控制台、pty、管道一视同仁。转义序列与 Windows 扫描码的
解码都在读取线程里完成。

其中有一处容易漏掉：单独的 `\x1b` 既可能是「按了 Esc」，也可能是方向键
序列的开头（`\x1b[A`）。因此必须**限时等待**后续字节——等到了是方向键，
等够了还没有就是 Esc。没有这个兜底，按 Esc 永远没反应。

### 已知限制

- **没有鼠标支持**：点击、拖选、滚轮都不可用。也因此**不开启鼠标跟踪**——
  开了却不解析，鼠标移动产生的 `ESC[M…` 会被当成乱码打进输入行。
- **没有剪贴板集成**、没有会话选择器、没有 `/init` 流程。
- **交互循环约 1300 行**，渲染走差分算法：只重画变化的部分，不整屏刷新。
- **`NO_COLOR` 被设置时颜色全部关闭**（这是[约定](https://no-color.org/)，也是
  色彩显示异常时唯一的自救手段）。布局不受影响。
- **原始终端取键无法在自动化环境里验证**（没有 TTY）。按键源与尺寸源可注入，
  因此交互循环能用脚本按键跑完；但 `msvcrt.getwch()` / `select()` 那一小段
  只能人工验证。

非真实终端（管道、CI、重定向）下 `--tui` 会**明确提示原因**并回退到行模式，
不会静默失效。

---

## 目录

```
src/corecode/                     行数    职责
  __main__.py / __init__.py               包入口
  cli.py                    759   行模式 REPL、参数解析、单次提问
  cli_commands.py           625   斜杠命令（29 条）
  manage_cli.py             421   子命令：corecode mcp / corecode skills
  install.py                268   交互式本地安装器（python -m corecode.install）
  repo_init.py              416   /init：探测仓库并生成 CORECODE.md
  history.py                223   输入历史（JSONL）
  host.py                   381   会话宿主：装配模型/工具/权限/计划
  config.py                 549   配置装载、路径访问、Claude 兼容回退
  permissions.py            532   权限：只读白名单、审批、持久化
  memory.py                 279   CORECODE.md 发现与装载
  evolution/                     自进化记忆：反思、资产库、BM25 索引、召回、Skill 生长
  skill_metadata.py              SKILL.md 元信息解析与兼容回退
  skill_routing.py               分层目录、一阶段召回、二阶段精排、Token 预算
  skills.py                 265   技能的发现、装载、安装、移除
  prompt.py                 125   系统提示
  types.py                  215   消息类型、AgentStep、回合结局
  tooling.py                233   工具契约、注册表、控制位
  abort.py                   85   贯穿式取消信号
  agent_loop.py             681   model → tool → model 主循环
  file_review.py             98   写前 diff 审查
  workspace.py               52   路径围栏
  stats.py                  150   回合统计（耗时、工具调用次数）
  background_tasks.py       199   后台 shell 任务注册表
  local_tool_shortcuts.py   193   /ls /grep /read /write /edit /patch /cmd
  ui/                       内置行模式界面（渲染、动画、宽度、主题）
  adapters/                 模型后端（anthropic 736 行 / mock）
  tools/                    内置工具（13 个）
  agents/                   中心化多 Agent：Fork / Worktree / Team + 合并门禁
  context/                  上下文管线：四级压缩、投影式 collapse、token 估算
  runtime/                  单飞回合、Goal、Loop、计划（session 编排）
  session/                  JSONL 会话树存储与恢复
  mcp/                      MCP：stdio 双分帧协商、HTTP、工具桥接
  utils/                    网页抓取/搜索、其它辅助
docs/                       行为规格：对等清单、有意差异
tests/                      行为测试（1671 项）
tools/                      开发工具：导入检查、变异检查、自检
```

各部分行数刻意列出：**最容易出错的地方往往是最不起眼的那些**
（`grep_files` 的路径判断、`config.py` 的路径解析），而不是最长的那几个。
