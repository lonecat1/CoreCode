# 与 TypeScript 原版的功能对等清单

> 对照仓库（只读）：`D:\CoreCode\MiniCode`
> 本清单由**逐文件核对**得出，不是印象。核对方法：
> 1. 列出 `MiniCode/src/**/*.ts` 全部 77 个文件与行数；
> 2. 列出 `minicode-py/src/**/*.py` 全部文件与行数；
> 3. 对每个 TS 文件，在 Python 版找出对应实现；
> 4. 对工具与命令，直接读**运行时注册表**与**命令表**，而不是看有没有同名文件。

## 现状（收尾时）

| 维度 | 状态 |
|---|---|
| 工具层（默认注册表） | **13 / 13** ✅ |
| 工具层（含 runtime 追加的 goal 工具） | **15 / 15** ✅ |
| 命令层 | **29 / 29** ✅ |
| `src/` 各模块 | **全部覆盖**，或记为有意差异 ✅ |
| 完全缺失（❌） | **0** |
| 能力缩水（⚠️） | **0** |
| 有意差异（🔶） | 38 条，逐条记在 `docs/divergences.md` |

**没有「待核对」的中间态**——每一项要么核对出结论，要么核对出缺口；
缺口已全部补齐。核对的副产品是修掉了 6 个既有缺陷（见「顺带修掉的既有缺陷」），
它们的共同特征是**静默**：不报错，只是行为不对。

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

**口径要先说清楚，否则数字会看起来对不上**（原版 `tools/index.ts` 的实际结构）：

| 注册表 | 数量 | 说明 |
|---|---|---|
| 默认注册表（**无** plan） | **12** | |
| 默认注册表（**有** plan） | **13** | `update_plan` 是条件加入的：`...(args.plan ? [createUpdatePlanTool(args.plan)] : [])` |
| 根注册表（runtime 再追加 4 个子 Agent 工具） | **17** | 横幅打印的就是这个数 |
| 子 Agent 可见（`SUB_AGENT_TOOL_NAMES`） | **6** | 只读子集 |

Python 侧四个数字分别是 **12 / 13 / 17 / 6**，与原版逐个集合比对一致
（实测，不是看文件是否存在）。

| 工具 | TS 文件 | 行数 | Python | 状态 |
|---|---|---|---|---|
| `ask_user` | `ask-user.ts` | 28 | `tools/ask_user.py` | ✅ |
| `list_files` | `list-files.ts` | 31 | `tools/files.py` | ✅ |
| `read_file` | `read-file.ts` | 53 | `tools/files.py` | ✅ |
| `write_file` | `write-file.ts` | 28 | `tools/files.py` | ✅ |
| `grep_files` | `grep-files.ts` | 42 | `tools/grep_files.py` | ✅ |
| `run_command` | `run-command.ts` | 211 | `tools/run_command.py` | ✅ **已补齐** |
| `edit_file` | `edit-file.ts` | 45 | `tools/edit.py` | ✅ **已补齐** |
| `patch_file` | `patch-file.ts` | 76 | `tools/edit.py` | ✅ **已补齐** |
| `modify_file` | `modify-file.ts` | 28 | `tools/edit.py` | ✅ **已补齐** |
| `update_plan` | `plan.ts` | 41 | `tools/goal.py` | ✅ **已接线**（见下） |
| `web_fetch` | `web-fetch.ts` | 58 | `tools/web.py` | ✅ **已补齐** |
| `web_search` | `web-search.ts` | 87 | `tools/web.py` | ✅ **已补齐**（含域名过滤） |
| `load_skill` | `load-skill.ts` | 42 | `tools/load_skill.py` | ✅ |
| `update_goal` / `get_goal` | `goal.ts` | 33 | `tools/goal.py` | ✅ |
| `spawn_agent` 等 4 个 | `sub-agents.ts` | 122 | `tools/subagents.py` | ✅ |

**运行时工具集已完全对等**——逐个集合比对确认，无缺无多。

### 三处「基础设施写好了但没接线」的缺口

这三个缺口的共同点很值得记下来：**实现都在、测试也过，但生产代码从来不调用它。**
纯函数测试发现不了这类问题——只有把「工具集 / 命令集 / 回调」当成**数据**去核对
才会暴露。

| 缺口 | 症状 |
|---|---|
| `permissions.ensure_command` | 只有测试调用；`run_command` 不存在，权限系统对它形同虚设 |
| `create_update_plan_tool` | 定义了但从未调用 → **Plan 功能实际不可用**，而系统提示每个回合都在让模型「用 update_plan 跟踪多步工作」，模型一调就是 `Unknown tool` |
| `history_path()` / `HISTORY_PATH` | 定义了但无人引用 → REPL 输入历史没有实现 |

### 三个编辑工具的对齐细节

实现都在 `tools/edit.py`（三者共享落盘路径与替换语义）。

| 原版行为 | 对应实现 |
|---|---|
| 默认**只替换第一处**（JS `String.replace` 字符串模式） | `apply_replacement(..., replace_all=False)` |
| `replaceAll` 替换全部 | 同 |
| `search` 非空（zod `.min(1)`） | `_require_text` |
| `edit_file` 读不到文件 → 抛 ENOENT | 明确失败提示（行为等价，文本更清楚） |
| `patch_file` 任一替换找不到 → **整体放弃** | 同（不写盘，不留半成品） |
| `patch_file` 输出 `#1 replaceOnce, #2 replaceAll` | 同格式 |
| `modify_file` 与 `write_file` **实现完全相同** | 同（只是 description 不同；因此可创建文件） |
| 替换串里的 `$&`/`$1` 被 JS 特殊展开 | 🔶 **不做展开**，`str.replace` 是字面替换（D33） |
| `readFile`/`writeFile` 字节级读写 | 🔶 必须显式 `newline=""`，否则 Windows 上翻转换行（D34） |


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

**原版有 37 条用法，但只有 29 个不同的命令**——因为 `/loop`、`/goal`、`/model`
各有多条用法变体（`/loop`、`/loop stop`、`/goal pause` …）。

因此**「用法条数」不是对等指标**（多一条少一条可能只是措辞不同），
**不同的命令名**才是。我一开始按用法条数比，得出了错误的结论。

**Python 版 29 / 29，命令层已完全对等。**

| 命令 | Python | 状态 |
|---|---|---|
| `/help` `/tools` `/status` `/plan` `/memory` `/skills` `/mcp` | ✅ | ✅ |
| `/permissions` `/resume` `/rename` `/new` `/fork` `/exit` | ✅ | ✅ |
| `/goal`（含 `status`/`pause`/`resume`/`clear`） | ✅ | ✅ |
| `/loop`（含 `stop`） | ✅ | ✅ |
| `/compact` `/collapse` `/snip` | ✅ | ✅ |
| `/ls` `/grep` `/read` `/write` `/modify` `/edit` `/patch` `/cmd` | ✅ | ✅ 本地工具快捷方式 |
| `/model [model-name]` | ✅ | ✅ |
| `/config-paths` | ✅ | ✅ |
| `/init` | ✅ | ✅ **已补齐** |

> 这几条测试把上面的清单变成**可执行的**：
> `test_no_commands_are_missing` 断言没有缺口，
> `test_all_29_commands_are_present` 断言数量，
> `test_no_extra_commands` 断言没有多出原版没有的命令，
> `test_every_listed_command_has_a_handler_or_is_a_shortcut` 断言每条都能被处理。


### 本地工具快捷方式的实现

`local-tool-shortcuts.ts` → `src/minicode/local_tool_shortcuts.py`。

| 原版行为 | 对应实现 |
|---|---|
| 8 个快捷方式的解析 | `parse_local_tool_shortcut` |
| `/ls` 靠空格避免吃掉 `/lsof` | 同（`text == "/ls" or text.startswith("/ls ")`） |
| `/cmd [cwd::]<command>` 的 cwd 在 `::` **之前** | 同 |
| `/cmd` 用朴素空白切分（不解析引号） | 同 |
| `/write` `/modify` 用 `indexOf('::')` 切一刀 | 同（内容里的 `::` 完整保留） |
| `/patch` 奇数片段 → 拒绝 | 同 |
| 只在 TUI 里接（`tty-app.ts:1385`） | 🔶 接在行模式 REPL 的分派链里（TUI 已移除），位置相同 |
| `/edit` `/grep` 用 `split('::')` 解构，多余片段**被静默丢掉** | 🔶 改为「最后一段收下剩余」（D35） |

分派顺序在两条路径上**必须一致**（`_dispatch` 与 `_run_once`）：

    运行时命令 → 本地命令 → 工具快捷方式 → 未知命令提示 → 普通回合

**快捷方式必须排在「未知命令提示」之前**：`/edit a::b::c` 的头部 `/edit`
现在是已知命令，若先做未知命令判断就会被拦下，永远走不到解析器。
我在这条上踩过两次——两次都是只改了一条路径。


---

## C · 模块层

| TS 模块 | 行数 | Python 现状 | 状态 |
|---|---|---|---|
| `local-tool-shortcuts.ts` | 123 | `local_tool_shortcuts.py` | ✅ **已补齐** |
| `background-tasks.ts` | 62 | `background_tasks.py` | ✅ **已补齐**（注册表 + PID 存活探测） |
| `mcp-status.ts` | 33 | `mcp/status.py` | ✅ **已补齐** |
| `init.ts` | 308 | `repo_init.py` | ✅ **已补齐** |
| `history.ts` | 62 | `history.py` | ✅ **已补齐**（JSONL 输入历史；见下） |
| `manage-cli.ts` | 260 | `manage_cli.py` | ✅ **已补齐**（`minicode mcp` / `minicode skills`） |
| `install.ts` | 126 | `install.py` | ✅ **已补齐**（`python -m minicode.install`；见 D39） |
| `utils/web.ts` | 506 | `utils/web.py` | ✅ **已补齐**（见下） |
| `mock-model.ts` | 161 | `adapters/mock.py`(91) | 🔶 **有意差异**（见下） |
| `utils/model-context.ts` | 144 | `context/model_context.py`(101) | ✅ **已核对一致**（见下） |
| `config.ts` 的 Claude 回退 | — | `load_effective_settings()` | ✅ **已补齐** |
| `tty-app.ts` + `tui/*` | ~3,500 | 已删除 | 🔶 用户明确放弃全屏 TUI |

### 逐文件核对的结果（三处）

**`utils/model-context.ts` · ✅ 完全一致**

用脚本逐条比对（不是目测）：两侧都是 **22 条规则**，每条的**模式列表、
上下文窗口、输出预留**以及**顺序**全部相同，未知模型回退也都是
`128000 / 8000`。行数差异（144 vs 101）纯粹来自格式——TS 用多行对象，
Python 用元组。

**`mock-model.ts` · 🔶 有意差异（已核对，不打算改）**

两侧的 mock 都有斜杠命令分支，但**这些分支在 CLI 路径下都不可达**——
本地工具快捷方式解析器先拦截了（`/ls` `/read` `/write` `/edit` `/cmd`
`/grep` 都在快捷方式表里）。原版也一样（`tty-app.ts` 在把输入交给模型之前
先解析快捷方式）。

所以差异是：
- TS 的 mock 多 `/grep` `/cmd` `/edit` `/tools` 四个分支（**不可达**）
- Python 的 mock 多 `/ask` `/boom` `/empty` `/progress`（**不可达**，
  但 Python 的测试**直接调适配器**时会用它们来驱动特定路径）

两边都是**测试辅助**，各自被自己的测试套件塑形。强行对齐等于：
往 Python 里加四个死分支、再从 Python 拿走测试依赖的钩子。**没有收益，
只有风险。** 因此明确记为有意差异，而不是悄悄留着。

**`utils/web.ts` · ✅ 已补齐**

| 原版能力 | 本实现 |
|---|---|
| `fetchWithRetry`（429/5xx 与网络错误重试，退避 300ms × 2^n，上限 2 次） | ✅ |
| 超时可配（`DEFAULT_TIMEOUT_MS = 12000`） | ✅ |
| `allowed_domains` / `blocked_domains` 过滤（子域匹配、黑名单优先、互斥校验） | ✅ |
| 搜狗搜索源（`parseSogouSearch`） | ✅ |
| `formatWebErrorMessage` 结构化错误 | ✅ |
| DuckDuckGo **Lite** 端点（`lite.duckduckgo.com`）与对应解析器 | ✅ |
| HTML 跳转跟进（`window.location` / meta refresh） | ✅ |
| DDG 的 `uddg=` 跳转链接还原 | ✅ |
| 正文抽取 | ✅（D36：保留段落结构） |

三处**有意差异**：D36（正文保留段落，原版压成一行）、D37（修掉搜狗协议相对
链接的死分支）、D38（「无结果」报成错误——与原版一致，但记下它是可争议的设计）。

一处**端点修正**：原版用 `lite.duckduckgo.com/lite/`，本实现此前用
`html.duckduckgo.com/html/`。两者页面结构完全不同（`result-link` vs
`result__a`），**换端点必须同时换解析器**——否则会得到「永远没有结果」，
而且看起来像「确实没搜到」。现在端点与解析器都对齐到 Lite。


### `history.ts` 的对齐说明

原版的输入历史是给 **TUI 的上下键补全**用的（`tty-app.ts` 的 `historyUp` /
`historyDown`）。行模式 REPL 里，上下键由**终端自己**负责，本模块不参与那个。

它仍然有价值的地方是**跨会话**：终端历史不区分项目（在 A 项目敲的命令，
在 B 项目的终端里按上键也会冒出来），而这份历史按项目记录，并带时间戳与会话 id。

数据层逐条对齐：JSONL、字段 `display`/`timestamp`/`project`/`sessionId`、
上限 500 条、**只追加不重复**（没有新条目时完全不碰文件）、超限保留最近的。
同样的字段名意味着两种实现可以共用同一个历史文件。

🔶 **有意差异**：TUI 的上下键导航随 TUI 一并移除（用户明确放弃全屏 TUI）。

### 顺带修掉的既有缺陷（不在缺口清单里，是核对时发现的）

| 缺陷 | 症状 |
|---|---|
| `grep_files` 的 `path` 指向**文件**时永远没有结果 | 回退扫描用 `os.walk(target)`，而它对文件路径静默产出空序列。于是 `/grep <pattern>::<file>`（文档化用法）永远返回 `(no matches)`，看起来像「确实没有匹配」而不是「我根本没搜」。`grep_files` 此前**没有任何专属测试**。 |
| **用导入期快照做 I/O**（四处） | `config.SETTINGS_PATH` 这类常量在**导入期**求值。`MINICODE_HOME` 在导入后变化时它们指向错误位置。受影响：`read_settings_file` / `save_settings` / `read_permission_store` / `write_permission_store` / `discover_memory_files`。症状全是**静默的**：`/model` 写到别处、授权规则全部失效、全局指令不生效。README 早就写了这条规则，但只有展示用途的地方遵守了。 |
| `/config-paths` 的长路径被截断 | 这条命令的全部意义是让用户看到**完整**路径去复制，而 `render_key_value` 默认截断成 `C:\Users\...\se...`，等于白做。 |

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

**全部完成。** 下表保留下来是为了记录**为什么这样排序**——它同时是一份
「先修什么最值」的判断记录。

| # | 项目 | 理由 | 状态 |
|---|---|---|---|
| 1 | `run_command` | 没有它 agent 不能执行任何动作；也是「切换目录」的前提 | ✅ |
| 2 | `background-tasks.ts` | 与 `run_command` 的后台执行配套 | ✅ |
| 3 | `edit_file` | 改代码的主力工具（搜索替换） | ✅ |
| 4 | `patch_file` | 批量替换 | ✅ |
| 5 | `modify_file` | 整文件改写（与 `write_file` 差异需对照原版确认） | ✅ |
| 6 | `local-tool-shortcuts.ts` + 8 条命令 | 命令层对等 | ✅ |
| 7 | `/model` `/config-paths` `/init` | 命令层补全 | ✅ |
| 8 | `history.ts` | REPL 输入历史（曾是死代码） | ✅ |
| 9 | `mcp-status.ts` | MCP 汇总 | ✅ |
| 10 | `utils/web.ts` 补全 | 搜狗源、域名过滤、重试 | ✅ |
| 11 | `mock-model.ts` / `model-context.ts` 核对 | 收尾 | ✅ 已核对 |

---

## 维护规则

1. **新增或修改任何模块时，必须同步更新本表。**
2. 不许用「✅ 完成（列举几个）」的写法掩盖缺失——**缺什么就写什么**。
3. 每补齐一项，把状态改为 ✅ 并注明对照的 `file:line`。
