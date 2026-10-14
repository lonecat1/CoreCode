# 与 TypeScript 原版的功能对等清单

> 对照仓库（只读）：`D:\CoreCode\MiniCode`
> 本清单由**逐文件核对**得出，不是印象。核对方法：
> 1. 列出 `MiniCode/src/**/*.ts` 全部 77 个文件与行数；
> 2. 列出 `minicode-py/src/**/*.py` 全部文件与行数；
> 3. 对每个 TS 文件，在 Python 版找出对应实现；
> 4. 对工具与命令，直接读**运行时注册表**与**命令表**，而不是看有没有同名文件。

## 为什么要有这份文档

早先 `README.md` 用「✅ 完成（文件读写 · grep · web · ask_user）」这样的写法
描述工具层，**在括号里省略了 4 个缺失的工具**，读者会以为「完成」＝对等。

这是表述问题，也是方法问题：我对复杂的部分（上下文压缩、会话树）做了逐行
对照，对看起来简单的部分（工具层）**凭印象**。而恰恰是工具层决定了 agent
能做什么。

因此从现在起，**每一项都显式标注状态**，不允许用省略代替缺失。

---

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ | 已实现，行为对齐 |
| ⚠️ | 已实现但能力缩水（缺具体特性） |
| ❌ | **完全缺失** |
| 🔶 | 有意差异（已记录理由） |

---

## A · 工具层（`src/tools/`）

TS 版默认注册表有 **13 个**工具，Python 版有 **9 个**。

| 工具 | TS 文件 | 行数 | Python | 状态 |
|---|---|---|---|---|
| `ask_user` | `ask-user.ts` | 28 | `tools/ask_user.py` | ✅ |
| `list_files` | `list-files.ts` | 31 | `tools/files.py` | ✅ |
| `read_file` | `read-file.ts` | 53 | `tools/files.py` | ✅ |
| `write_file` | `write-file.ts` | 28 | `tools/files.py` | ✅ |
| `grep_files` | `grep-files.ts` | 42 | `tools/grep_files.py` | ✅ |
| `run_command` | `run-command.ts` | 211 | `tools/run_command.py` | ✅ **已补齐**（见下） |
| `web_fetch` | `web-fetch.ts` | 58 | `tools/web.py` | ⚠️ 缺重试 |
| `web_search` | `web-search.ts` | 87 | `tools/web.py` | ⚠️ 缺域名过滤、搜狗源 |
| `load_skill` | `load-skill.ts` | 42 | `tools/load_skill.py` | ✅ |
| `update_plan` | `plan.ts` | 41 | `tools/goal.py` | ✅ |
| `update_goal` / `get_goal` | `goal.ts` | 33 | `tools/goal.py` | ✅ |
| `spawn_agent` 等 4 个 | `sub-agents.ts` | 122 | `tools/subagents.py` | ✅ |
| **`patch_file`** | `patch-file.ts` | **76** | — | ❌ **完全缺失** |
| **`edit_file`** | `edit-file.ts` | **45** | — | ❌ **完全缺失** |
| **`modify_file`** | `modify-file.ts` | **28** | — | ❌ **完全缺失** |

### `run_command` 的对齐细节

已按原版逐条镜像（`src/tools/run_command.ts` → `src/minicode/tools/run_command.py`）：

| 原版行为 | 对应实现 |
|---|---|
| `READONLY_COMMANDS`（17 个，免审批） | `READONLY_COMMANDS` |
| `DEVELOPMENT_COMMANDS`（8 个，需审批） | `DEVELOPMENT_COMMANDS` |
| `splitCommandLine` 引号/转义拆分 | `split_command_line` |
| `normalizeCommandInput`（有 args 则不拆分） | `normalize_command_input` |
| `looksLikeShellSnippet`（`\|&;<>()$\``） | `looks_like_shell_snippet` |
| `isBackgroundShellSnippet`（`&` 但不是 `&&`） | `is_background_shell_snippet` |
| `cwd` 参数经 `resolveToolPath(..., 'list')` | `resolve_tool_path(context, cwd, "list")` |
| 未知命令带 `forcePromptReason` | `force_prompt_reason` |
| 只读命令**完全免审批** | 同 |
| 后台执行 `spawn(detached)` + 登记任务 | `_spawn_background` + `background_tasks` |
| `execFile` maxBuffer 1MB 超限**失败** | 🔶 改为**截断并注明**（D31） |
| 硬编码 `bash -lc` | 🔶 解析真实 bash，**排除 WSL 转发器**（D32） |

### 为什么 `run_command` 缺失最严重

它是唯一让 agent **执行动作**的工具。缺了它，Python 版的 agent 只能读写
文件——不能跑测试、编译、git、构建。

用户观察到的「TS 版能自动切换所在目录」正源于此：TS 的 `run_command` 接受
`cwd` 参数（`run-command.ts:177-179`），命令可以在另一个目录里执行：

```typescript
const effectiveCwd = input.cwd
  ? await resolveToolPath(context, input.cwd, 'list')
  : context.cwd
```

Python 版连工具都没有，自然做不到。

---

## B · 命令层（`cli-commands.ts`）

TS 版 **29 条**用法，Python 版 **18 条**。

| 命令 | Python | 状态 |
|---|---|---|
| `/help` `/tools` `/status` `/plan` `/memory` `/skills` `/mcp` | ✅ | ✅ |
| `/permissions` `/resume` `/rename` `/new` `/fork` `/exit` | ✅ | ✅ |
| `/goal`（含 `status`/`pause`/`resume`/`clear`） | ✅ | ✅ |
| `/loop`（含 `stop`） | ✅ | ✅ |
| `/compact` `/collapse` `/snip` | ✅ | ✅ |
| **`/model`** | — | ❌ 查看/切换模型 |
| **`/config-paths`** | — | ❌ 显示配置文件路径 |
| **`/init`** | — | ❌ 初始化仓库 |
| **`/ls`** | — | ❌ 本地快捷方式 |
| **`/grep`** | — | ❌ 本地快捷方式 |
| **`/read`** | — | ❌ 本地快捷方式 |
| **`/write`** | — | ❌ 本地快捷方式 |
| **`/modify`** | — | ❌ 本地快捷方式 |
| **`/edit`** | — | ❌ 本地快捷方式 |
| **`/patch`** | — | ❌ 本地快捷方式 |
| **`/cmd`** | — | ❌ 本地快捷方式 |

> 注意：mock 适配器里的 `/ls` `/read` `/write` **不是**这套机制。那只是
> mock 用来跑测试的便利分支，真实模型走不到，也没有 `/grep` `/edit` `/cmd`。

---

## C · 模块层

| TS 模块 | 行数 | Python 现状 | 状态 |
|---|---|---|---|
| `local-tool-shortcuts.ts` | 123 | 完全没有 | ❌ |
| `init.ts` | 308 | 完全没有 | ❌ |
| `mcp-status.ts` | 33 | 完全没有 | ❌ |
| `background-tasks.ts` | 62 | 只有 `BackgroundTaskResult` 类型，无注册表、无 PID 存活检测 | ❌ |
| `history.ts` | 62 | `config.history_path()` 与 `HISTORY_PATH` **定义了但无人引用 = 死代码** | ❌ |
| `manage-cli.ts` | 260 | `skills.py` 覆盖了技能安装部分 | ⚠️ 需逐条核对 |
| `install.ts` | 111 | 同上 | ⚠️ 需逐条核对 |
| `utils/web.ts` | 443 | `utils/web.py`(217) + `tools/web.py`(130) | ⚠️ 缺搜狗源、域名过滤、重试 |
| `mock-model.ts` | 161 | `adapters/mock.py`(91) | ⚠️ 快捷方式少 |
| `utils/model-context.ts` | 144 | `context/model_context.py`(101) | ⚠️ 需核对窗口表 |
| `tty-app.ts` + `tui/*` | ~3,500 | 已删除 | 🔶 用户明确放弃全屏 TUI |

### 已被验证的「已对齐」模块

以下模块经逐行对照，行为对齐（差异记在 `docs/divergences.md`）：

`agent-loop.ts` · `session.ts` · `compact/*`（四级压缩）· `permissions.ts` ·
`anthropic-adapter.ts` · `config.ts` · `memory.ts` · `skills.ts` ·
`agents/*` · `goal/*` · `loop/*` · `plan/*` · `runtime/*` ·
`utils/token-estimator.ts` · `utils/context.ts` · `utils/tool-result-storage.ts` ·
`prompt.ts` · `file-review.ts` · `workspace.ts` · `tool.ts` · `types.ts` ·
`mcp.ts`（拆为 `mcp/` 包）

---

## D · 修复顺序（按用户可见影响排序）

| # | 项目 | 理由 |
|---|---|---|
| 1 | `run_command` | 没有它 agent 不能执行任何动作；也是「切换目录」的前提 |
| 2 | `background-tasks.ts` | 与 `run_command` 的后台执行配套 |
| 3 | `edit_file` | 改代码的主力工具（搜索替换） |
| 4 | `patch_file` | 批量替换 |
| 5 | `modify_file` | 整文件改写（与 `write_file` 差异需对照原版确认） |
| 6 | `local-tool-shortcuts.ts` + 8 条命令 | 命令层对等 |
| 7 | `/model` `/config-paths` `/init` | 命令层补全 |
| 8 | `history.ts` | REPL 输入历史（现在是死代码） |
| 9 | `mcp-status.ts` | MCP 汇总 |
| 10 | `utils/web.ts` 补全 | 搜狗源、域名过滤、重试 |
| 11 | `mock-model.ts` / `model-context.ts` 核对 | 收尾 |

---

## 维护规则

1. **新增或修改任何模块时，必须同步更新本表。**
2. 不许用「✅ 完成（列举几个）」的写法掩盖缺失——**缺什么就写什么**。
3. 每补齐一项，把状态改为 ✅ 并注明对照的 `file:line`。
