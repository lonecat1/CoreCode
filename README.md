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
| Skills `skills` | ✅ 完成（四层发现 + `load_skill` + 安装管理） |
| MCP `mcp/` | ✅ 完成（stdio 双分帧协商 + HTTP + 工具桥接） |
| 差分帧渲染 TUI `tui/` | ⚠️ 可用，但**非 `tty-app.ts` 的逐行移植**（见下节） |

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

`mutation_check.py` 还会**自愈**：变异前先把原文另存为 `.mutation-backup`，
下次运行开头检查并恢复。原因是脚本被中途杀死时 `finally` 不会执行，变异会
留在源码里——这真实发生过一次，导致后续测试以难以理解的方式失败。
空操作变异（替换后内容不变）会被报成 `ERROR` 而不是误报「测试无效」。

---

## MCP 接入

在 `~/.mini-code/mcp.json`、项目根的 `.mcp.json`，或 `settings.json` 的
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

写入 `~/.mini-code/settings.json`：

```json
{
  "model": "qwen3.8-max-0902",
  "env": {
    "ANTHROPIC_BASE_URL": "https://dashscope.aliyuncs.com/apps/anthropic",
    "ANTHROPIC_API_KEY": "sk-...",
    "MINICODE_THINKING": "disabled"
  },
  "maxOutputTokens": 16000
}
```

**优先级：`settings.json` 的 `env` > 进程环境变量。** 配置文件优先是刻意的
（见 [divergences.md](docs/divergences.md) D25）：机器上往往早已存在
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（为别的工具配的），若让它们压过
本工具的配置文件，你写在这里的端点与密钥会被静默作废。凭据成对决策（D26）——
文件提供了任一凭据时就不再从环境回退，避免「端点来自 A、密钥来自 B」。

`MINICODE_*` 前缀是本工具专有的，**始终**以进程环境为准，便于临时覆盖：

```bash
MINICODE_MODEL=claude-sonnet-4 minicode
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
2. **必须设 `"MINICODE_THINKING": "disabled"`**（或 settings 里的 `"thinking": "disabled"`）。
   百炼的 `qwen3.8-max` 系列默认开启思考，而思考 token 与正文共享 `max_tokens`；
   预算被思考吃光时模型返回**空内容**。症状是「模型不回答」，根因却是 token 预算。

如果 `base_url` 指向别的服务商而模型名是 `anthropic` 系的，或反之，都会失败——
端点与模型必须来自同一家。

---

## 全屏界面（内联实时区域）

```bash
minicode --tui
```

外观仿 Claude Code：圆角输入框、`⏺` 起头的工具调用、`⎿` 承接结果、
底部带转圈动画的状态行。

```
MiniCode  session abc123  tools 12
                                        ← 转录区（不足时补空行，输入框始终贴底）
> 帮我看看这个文件

⏺ read_file
  ⎿ 读取了 pyproject.toml
这个项目的名字是 minicode。

╭──────────────────────────────────────╮
│ > 下一个问题                          │
╰──────────────────────────────────────╯
⠋ 思考中…  ·  qwen3.8-27b  ·  ctx 12%
```

### 为什么是「内联」而不是「全屏」

早先的实现（也是照搬原版 `tty-app.ts` 的做法）用**备用屏幕 + 绝对行定位**
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
或 PowerShell**，或用行模式 `minicode`。

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
- **没有剪贴板集成**、没有会话选择器、没有 `/init` 流程、没有欢迎动画。
- **原版 `tty-app.ts` 有 2394 行，本目录约 1300 行**，交互循环是重新实现的，
  不是逐行移植。真正复刻的是**差分渲染算法**（对照 `screen.ts`）。
- **`NO_COLOR` 被设置时颜色全部关闭**（这是[约定](https://no-color.org/)，也是
  色彩显示异常时唯一的自救手段）。布局不受影响。
- **原始终端取键无法在自动化环境里验证**（没有 TTY）。按键源与尺寸源可注入，
  因此交互循环能用脚本按键跑完；但 `msvcrt.getwch()` / `select()` 那一小段
  只能人工验证。

非真实终端（管道、CI、重定向）下 `--tui` 会**明确提示原因**并回退到行模式，
不会静默失效。

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
