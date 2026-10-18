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
| 工具层（默认注册表，无/有 plan） | **12 / 13** ✅ 与原版逐个集合一致 |
| 工具层（根注册表，含子 Agent 工具） | **17** ✅ |
| 子 Agent 可见 | **6** ✅ |
| 命令层 | **29 / 29** ✅ |
| `src/` 各模块 | **全部有对应实现** ✅ |
| 完全缺失（❌） | **0** |
| 能力缩水（⚠️） | **0** |
| 有意差异（🔶） | 39 条，逐条记在 `docs/divergences.md` |

### 但「有实现」不等于「已验证」

这句话必须写清楚，因为**我在这上面犯过一次错**：`utils/context.ts` 曾被列为
「已验证已对齐」，而它的规则表其实是自拟的，并且因此静默截断用户配置。

**做过数据级比对（逐条拉出常量表/集合比对）的模块**：

| TS 模块 | 比对内容 |
|---|---|
| `utils/model-context.ts` | 22 条规则（模式/窗口/预留/顺序） |
| `utils/context.ts` | 18 条输出上限规则 + 兜底值 |
| `tools/index.ts` | 4 个注册表集合（12/13/17/6） |
| `cli-commands.ts` | 29 个命令名 |
| `utils/web.ts` | 逐函数 + 端点/解析器配对 |
| `compact/constants.ts` | 22 个值 + 零引用常量核实 |
| `utils/token-estimator.ts` | warningLevel 阈值 0.95/0.85/0.50 + `CLEAR_MARKER` + `CHARS_PER_TOKEN` 9 项逐项 + 各 role 的长度取法 |
| `compact/snipCompact.ts` | `PROTECTED_TOOL_NAMES` / `ERROR_MARKERS` / 子串保护分支 / 边界角色（15 个用例） |
| `compact/context-collapse.ts` | 汇总开销比 0.15 与下限 128 |
| `utils/tool-result-storage.ts` | 6 个常量 + 预览的「最后一个换行超过 50% 才切」规则 |
| `session.ts` | 12 个事件类型（JSONL 磁盘契约）+ `MAX_TITLE_LENGTH` |
| `agents/types.ts` | `MAX_SUB_AGENTS=3` / `MAX_SUB_AGENT_STEPS=20` / 4 个状态 |
| `goal/manager.ts` | `goalUpdateSchema` 的三支判别联合与各自必填项 |
| `loop/scheduler.ts` | `DEFAULT_LOOP_INTERVAL=600000` / `MAX_LOOP_INTERVAL=2147483647` |
| `mcp.ts` | 初始化探测超时 1200ms / 初始化超时 10000ms |
| `memory.ts` | 候选清单 6 项与顺序、两个字符上限、`@` 引用语法 |
| `permissions.ts` | 无内置名单（只读/开发命令表在 `run-command.ts`，已核对） |
| `runtime/session-runtime.ts` | `submit` 的四级优先级、`command` 的 `/loop` `/goal` 分发、`assertIdle` 的三条检查 |
| `listener/scheduler.ts`（即 `loop/scheduler.ts`） | 间隔文法两条规则 + 默认/最小/最大间隔 |
| `agent-loop.ts` | 无模块级常量与数值阈值（纯逻辑）；压缩阶段顺序逐条对照 |
| `plan/types.ts` + `manager.ts` + `tools/plan.ts` | `updatePlanSchema` 的 3 状态枚举、`<=1 in_progress`、id 校验两条、工具输出形状 |
| `goal/manager.ts` + `tools/goal.ts` | `goalUpdateSchema` 三支联合、`get_goal` 返回 goal+plan、`completion` 嵌套形状 |
| `skills.ts` | 4 个技能根目录与顺序、名称校验（D19 加固） |
| `prompt.ts` | MCP 行格式、`MCP_NAMING_HINT` 逐字一致 |
| `types.ts` | 9 个消息角色 + 6 个回合结局 |
| `agents/manager.ts` | `MAX_SUB_AGENTS=3` / `MAX_SUB_AGENT_STEPS=20`、id 格式 `agent_N`、`wait` 的 30 秒默认超时、close 的 abort 文案 |
| `mcp.ts` 的桥接层 | `sanitizeToolSegment` 三条细则、`normalizeInputSchema`、`endpoint_key` 格式、三种 status、失败时的协议上报 |
| `runtime/turn-runner.ts` | 单飞闸门、`awaitingUser` 由 outcome 推导、`stop` 的默认原因文案 |
| `tools/run-command.ts` | `READONLY_COMMANDS` **17/17**、`DEVELOPMENT_COMMANDS` **8/8**、`MAX_OUTPUT_BYTES = 1048576` |
| `cli-commands.ts` | `SLASH_COMMANDS` **37 条逐条同序**；`completeSlashCommand` 的两条规则（前缀命中 / 无命中列全部） |
| `compact/snipCompact.ts` 的边界文本 | `buildAnthropicSnipBoundaryText()` **逐字一致**（4 行，含空行） |
| `plan/context.ts` 的注入头 | `PLAN_CONTEXT_HEADER` **5 行逐字一致** |
| **全仓 267 个模块级常量** | `tools/check_constants.py`，零孤儿 |
| **全仓 589 个模块级函数/类** | `tools/check_orphans.py`，零孤儿 |

**只做过逐行阅读、尚未做数据级比对的模块**（**已知剩余工作**）：
`utils/*` 里少数工具函数的边界值 · `runtime/*` 的算法内部阈值

> 注：`agents/manager.ts`、`mcp/bridge.py`、`runtime/turn-runner.ts`、
> `runtime/session-runtime.ts`、`utils/token-estimator.ts` 已在本轮完成
> 数据级比对，从上面的清单移出。

### 横切契约：非法 UTF-8（D45）

这条不归属某一个模块，但影响 10 个读取点，因此单列：

| 类别 | 做法 | 测试 |
|---|---|---|
| 只读的状态文件 | `errors="replace"`（与 Node `readFile('utf8')` 一致） | `tests/test_text_decoding.py` |
| 会被写回的文件 | 抛 `FileReviewError`，本次修改取消 | 同上 |

判断依据是「会不会写回」：有损读 + 写回 = **用户文件被真的改坏**。
这些模块的**行为**经过对照与测试，但它们内部的**常量表与阈值**没有逐条验证过
——而 `utils/context.ts` 恰恰就是在这类地方出问题的。

### 测试覆盖映射

用「模块 → 测试是否点名」扫过全部 66 个生产模块，**唯一完全未覆盖的是
`mcp/http_client.py`（228 行）**，已补上 52 项测试。

做法值得保留：**没有测试的地方就是差异藏身处**。`memory.py` 就是这样藏了
两处（`@` 引用行的空白判定、`visited` 的 DFS 语义），而它在补测试之前
一处都没被发现。

### 三个可复用的核查动作

已经固化成脚本，不是一次性劳动：

| 脚本 | 查什么 | 当前结果 |
|---|---|---|
| `tools/check_imports.py` | 相对导入是否存在、名字是否真的定义、有没有从未被导入的模块 | 78 个模块全通过 |
| `tools/check_constants.py` | 模块级常量的「谁在用」：零引用 / 仅测试引用 / **伪引用**（只在注释里出现） | 267 个常量，零孤儿 |
| `tools/check_orphans.py` | 模块级**函数与类**的引用情况：定义了却没人调 | 596 个函数/类，零孤儿 |
| `tools/e2e_compare.py` | **把两边真的跑起来比**：同一场景分别跑 TS 与 Python，diff 输出 **+ 比留下的文件** | 20/20 通过（5 个逐字一致） |
| `tools/mutation_check.py` | 测试是否真的会变红（并审计变异锚点是否有歧义） | 247 条，0 歧义 0 无效 |

**「伪引用」那一类是这套检查的主要价值**：常量只在注释或 docstring 里被
提到时，扫一眼会觉得「有引用」，而实际没有任何代码读它。我自己就被骗过一次
——把 `MIN_KEEP_TOKENS` 的注释行当成了「有人在用」。

### 为什么又加了一个「函数孤儿」检查器

`check_constants.py` 只扫**模块级赋值**（常量），函数不在它的视野里。于是这类
缺陷完全逃得掉：

> `find_matching_slash_commands()` 被定义、有 docstring、看起来完全正常，
> 但**没有任何调用点**。用户感受到的是「Tab 补全没反应」。

常量孤儿通常是「写多了」；**函数孤儿往往是「该接线但没接」**——后者更严重，
因为它描述的是一个缺失的功能，而代码读起来像功能已经有了。

`check_orphans.py` 上线后的第一批产出就是三个真缺陷：那个补全函数、它自己扫出
的 `NullContextPipeline`（与 `IdentityContextPipeline` 功能完全相同、从未使用），
以及四条无人调用的死函数。

**白名单是双向的**：已不再零引用的条目也会被报出来。写这个工具时我在白名单里
放了两条入口函数，工具当场把它们报了出来——`main` 其实被 `__main__.py` 引用着。

### 端到端对照：为什么它无可替代

逐模块核对是**局部证据**。每个常量、每张表、每个函数都对上了，也不等于「它们
组合起来的行为」一致。

`tools/e2e_compare.py` 把两边真的跑起来、diff 输出。它第一轮就抓到四件事：

1. **环境变量拼写**：原版读 `MINI_CODE_HOME`，本实现读 `MINICODE_HOME`——
   一个从原版过来的用户设了环境变量，程序照旧读写默认目录，**不报错**（D47）。
2. **启动横幅是另一套字段**，而我还给它写了个「信息量一致」的理由（D48）。
3. 原版**没有** `-p/--prompt`，第一版对照脚本用它驱动，等于在比两个不同的东西。
4. `skills add` 缺参数时原版打完整 Node 栈——本实现打一行干净错误，**有意保留**。

三种期望让它可以当回归闸门用：

| 期望 | 含义 |
|---|---|
| `identical` | 归一化后必须逐字相同。**失败就是 bug** |
| `banner_only` | 只比框体行（框体必须一致，周边有行模式必需的额外输出） |
| `intentional` | 已登记理由的有意差异，只报告 |
| `py_only` | 原版在这条路径上**没有这个功能**，改为断言本实现自己的文件结果 |

`intentional` 的每一条都必须在 `INTENTIONAL` 字典里写清「差什么、为什么有意」
——不写的会被判为失败。否则这一档就变成「懒得管」。

### 「比文件」比「比 stdout」强得多

**全部 20 个场景都会额外比对两侧工作目录里的文件内容**（`.mini-code/` 之类
的状态文件除外，它们天然含时间戳）。

这条规矩是被一次**空断言**逼出来的：第一版把工具场景标成 `intentional`、理由
写着「工具行为一致，只是渲染不同」，跑完打印文件快照才发现 `a.txt` **一个字节
都没变**——管道 stdin 让权限提示遇到 EOF，两边都**拒绝**了。于是「文件一致」
只证明「两边都没做事」。

修法分两步：给 `py_only` 场景加 `--yes` 让批准通过；把断言从「两侧文件相同」
改成「**文件等于期望值**」。现在能证明的是：`/write` 真的写入、`/edit` 真的
替换、`/patch` 有一处不中时**整体放弃**（文件一个字节不动）。

### 顺带发现的第四件事：这些命令原版只在 TUI 里接

`index.ts`（普通 readline 路径）**完全不** import `parseLocalToolShortcut`，
只有 `tty-app.ts:1385`（TUI）用它。所以 `/ls` `/read` `/write` `/edit`
`/patch` `/cmd` 在原版的非 TUI 路径下一律回「未识别命令」。

本实现放弃了 TUI，把它们接进了行模式（已登记的有意差异），因此这些场景
**不可比**——一边不认识、一边真的执行。这就是 `py_only` 存在的理由。

### 一条操作纪律：变异检查运行期间**不得编辑源码**

`mutation_check.py` 会**改写源码**再跑测试，跑完从 `.mutation-backup` 还原。
因此它运行期间：

- 改源码 → 你的改动可能被「还原」覆盖，或者被当成变异的一部分；
- 跑测试 → 会得到假失败（源码此刻是被改坏的）。

我违反过一次：在变异检查运行时给 `config.py` 加 `errors="replace"`，而它当时
正好有一个变异作用在同一文件上，把 `_write_json_pretty` 的 `indent=2` 改掉了。
症状是另一个模块的测试报「期望 pretty JSON 却得到紧凑 JSON」——**完全看不出
和「我在改 config.py」有关系**。

`heal_leftovers()` 从备份恢复成功（备份里含我的改动，两者都保住了）。教训：
**要改源码，先停作业**；输出去文件（`tests/_out.txt`）而不是靠作业输出，
因为作业会跨轮次丢失。

（原版有 4 个零引用常量，本实现删掉 3 个、接线 1 个。另外把整套**导入期路径
快照**（8 个）也删了——它们的注释写着「仅供展示」，而实际零引用，且那句注释
会诱导后来者在需要展示路径时选中它们、重新引入「显示了一个并不生效的路径」
这个 bug。）

核对的副产品是修掉了 7 个既有缺陷（见「顺带修掉的既有缺陷」），
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

### ⚠️ 这张表只说明「命令表里有」，不说明「行模式下能用」

原版有相当一部分功能**只接在 TUI 里**。`index.ts`（普通 readline 路径）压根
没有 import 它们：

| 功能 | 原版接线位置 | `index.ts` 有吗 |
|---|---|---|
| 本地工具快捷方式（`/ls` `/read` `/write` `/edit` `/patch` `/cmd`） | `tty-app.ts:1385`（`parseLocalToolShortcut`） | **没有** |
| 会话持久化（`saveSession`） | `tty-app.ts` 三处 | **没有** |
| 输入历史（`saveHistoryEntries`） | `tty-app.ts:1355` | **没有** |
| 会话清理 / 项目列表 | `tty-app.ts:1825` / `:2194` | **没有** |

所以在**原版的非 TUI 路径**下，`/resume` `/fork` `/ls` … 一律回「未识别命令」。

本实现放弃了 TUI，把这些能力接进了行模式——这是**能力上的加法**（有意差异，
见 `docs/divergences.md`）。上表里这些行的 ✅✅ 应当理解为「本实现有，且比
原版的**行模式**多」；`tools/e2e_compare.py` 里它们被标成 `py_only`，正因为
**两边不可比**。

这一节是补写的。此前那张表让人以为「原版行模式也能用」，与它自己要避免的
夸大是同一类问题。


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
| `tty-app.ts` + `tui/*`（9 个文件） | 4,169 | 已删除 | 🔶 用户明确放弃全屏 TUI |

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
| **启动横幅右边框每行错开一列** | 内容行宽 75、边框行宽 76（可用宽度比正确值少 1）。启动横幅是用户看到的第一样东西，错位很显眼。之前没被发现，一部分原因是核对时按**字符数**看而不是**显示宽度**——横幅里有中文与全角括号。 |
| **输出 token 上限的规则表是自拟的粗粒度子串** | 原版是 18 条精确模式；本实现是 10 条粗粒度子串。这不是「数字略有出入」而是**静默截断用户配置**：用户设 `maxOutputTokens: 16000`、模型 `deepseek-v4-pro` 时，原版保留 16000，粗粒度 `deepseek` 规则砍成 8192 且不报错。症状是「长回答被莫名截断」，而用户对照自己的配置看不出原因。 |
| **`load_runtime_config` 的参数遮蔽同名函数** | 参数名 `settings_path` 与模块级 `settings_path()` 同名，函数体内调用它会得到 `'WindowsPath' object is not callable`。它是**修「凭据报错不指明位置」时才暴露的**——原来的报错文案是静态字符串，不调用该函数。 |
| **整套导入期路径快照零引用（8 个）** | `SETTINGS_PATH` / `HISTORY_PATH` / `PERMISSIONS_PATH` / `MCP_PATH` / `MCP_TOKENS_PATH` / `PROJECTS_DIR` / `TOOL_RESULTS_DIR` / `MINICODE_HOME` 注释写着「仅供展示（如 /status 打印路径）」，实际**没有任何地方展示它们**。错注释比没有注释更糟：它让人在需要展示路径时正好选中这些快照。已整块删除。 |
| **`/permissions` 与两处报错用快照而非动态访问器** | `/permissions` 显示的存储路径来自导入期快照，`MINICODE_HOME` 变化后指向**并不生效**的位置——用户按它去找自己的授权规则会找不到。`load_runtime_config` 的报错与摘要同样退到快照。三处都改用动态访问器。 |
| **`GLYPH_OK` / `GLYPH_FAIL` 有定义却被硬编码绕过** | theme.py 写着「哪些颜色被用到是可以被检查的」，而字形常量在 `cli.py` 与 `cli_commands.py` 里被写成字面量 `'✓'`/`'✗'`。已接线并补上导出。顺带清掉来自已移除全屏 TUI 的 8 个零引用色号。 |
| **`memory` 的 `@` 引用行只判字面空格** | 原版是 `/^@([^\s]+)\s*$/`，`\s` 涵盖制表符。早先用 `" " in stripped`，于是 `@foo\tbar` 被当成引用，产出 `include skipped: not found`——看起来像「文件不存在」，而实际是「这行根本不是引用」。 |
| **`memory` 的 `visited` 从不移除** | 原版在递归返回后 `visited.delete(...)`（DFS 栈语义），同一文件可从不同分支各包含一次。本实现从不移除，第二次包含同一个文件会报 `cycle detected`——**原因说错了**，且**内容静默缺失**。 |
| **`mcp/http_client.py` 完全未覆盖（228 行）** | 全部 66 个生产模块里唯一一个测试从未点名的。已补 52 项测试。 |
| **`/loop` 的间隔文法自拟** | 原版 `/^(\d+(?:\.\d+)?)(m\|h)$/` 支持小数并拒绝「像数字但格式不对」的前缀；本实现是另一套文法。于是 `/loop 1.5h do it` 从「90 分钟、prompt 是 do it」变成「**10 分钟**、prompt 是 1.5h do it」——用户要 90 分钟得到 10 分钟，不报错。 |
| **auto-compact 之后替换引用而非原地清空** | 原版 `replaceContextCollapseState()` 会同步调用方持有的对象；本实现只替换内部引用，于是 `host` 继续拿着过期 span 写进会话文件——与同文件「必须原地更新」的不变量矛盾。 |
| **auto-compact 的进程级熔断器在测试间泄漏** | `compact._state` 是模块级可变状态，`AutoCompactTests` 专测「连续失败熔断」会把它置为 disabled 且**从不复位**（`reset_auto_compact_state` 被导入却从未调用）。后果不是测试报错，而是**测试因为错误的原因通过**。 |

### 关于「已对齐」这份清单的可信度

**它出过一次错，值得记下来。**

`utils/context.ts` 曾列在下面这份清单里，被当作「已验证已对齐」。但后来逐条比对
它的 `MODEL_MAX_OUTPUT_TOKEN_RULES` 时发现：**规则表整个是自拟的**，与原版的
18 条精确模式不一致，并且因此静默截断用户配置。

原因是我当初核对时看的是「这个模块**存在**且看起来做了同一件事」，而不是
「把它的**数据表**逐条拉出来比对」。同一个错误在工具层与命令层也犯过
（见文档开头「为什么要有这份文档」）。

**因此下面这份清单现在只收录做过「数据级比对」的模块**，且注明比对方式：

| 模块 | 比对方式 |
|---|---|
| `utils/model-context.ts` | 脚本逐条比对 22 条规则（模式/窗口/预留/顺序）→ 完全一致 |
| `utils/context.ts` | 脚本逐条比对 18 条规则（模式/默认值/上限/顺序）+ 兜底值 → 完全一致 |
| `tools/index.ts` | 实测四个注册表的集合（12 / 13 / 17 / 6）→ 与原版逐个集合一致 |
| `cli-commands.ts` | 命令名集合（29 个）→ 与原版一致 |
| `utils/web.ts` | 逐函数核对 + 端点/解析器配对检查 |
| `manage-cli.ts` / `install.ts` / `init.ts` / `history.ts` / `mcp-status.ts` | 逐行核对并补齐 |

其余模块（`agent-loop.ts`、`compact/*`、`permissions.ts`、`mcp.ts` 等）此前做过
逐行对照，但**没有做过数据级比对**——它们的规则表/常量表尚未逐条验证。
这是一项**已知的剩余工作**，不应被读成「已全部验证」。

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
