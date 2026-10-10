# MiniCode 会话持久化子系统 — 实现规格（Python 复刻版）

本文件描述 TypeScript 版 MiniCode 的会话持久化子系统（`src/session.ts` 及其调用点）的**精确语义**，
目标是让没有读过 TypeScript 的开发者能在 Python 中逐字节复刻其行为。

- 唯一实现文件：`D:\CoreCode\MiniCode\src\session.ts`（下称 `session.ts`，行号即该文件行号）。
- 相关文件：`src/config.ts`（磁盘位置、settings 合并）、`src/types.ts`（`ChatMessage` 联合与消息身份）、
  `src/tty-app.ts`（全部调用点）、`src/index.ts`（启动时的 fork/初始游标）、
  `src/compact/*`（压缩层，决定边界事件的内容）、`src/history.ts`、`src/permissions.ts`、`src/utils/tool-result-storage.ts`。
- 引用格式：`file:line` 或 `file:line-line`，均指 TypeScript 源文件。
- 建议的 Python 落点：`src/minicode/session.py`（纯函数 + `pathlib`/`json`），`ChatMessage` 沿用 `minicode.types` 的 dict 表示。

阅读顺序建议：§1 磁盘布局 → §2 事件模式 → §3 追加算法 → §5 恢复算法 → §6 函数规格 → §10 gotchas。
§4/§7/§8 是交叉约束，复刻时不能漏。

---

## 0. 术语

| 术语 | 含义 |
| --- | --- |
| sessionId | 8 个十六进制字符的会话标识，`crypto.randomUUID().slice(0,8)`（`index.ts:76`、`tty-app.ts:1317`、`session.ts:536`） |
| cwd | 进程工作目录，调用方传入；持久化层从不 `realpath`/规范化，只做字符串变换 |
| event | JSONL 文件里的一行，一个 JSON 对象 |
| message event | 带 `message` 字段的事件（可被 `unwrapMessage` 还原成 `ChatMessage`） |
| boundary event | 三个“侧信道”事件：`compact_boundary` / `snip_boundary` / `context_collapse`，它们 `parentUuid=null` 且带 `logicalParentUuid` |
| alreadySavedCount | 调用方持有的“已保存非系统消息条数”游标（**不是**事件数），只在无 `id` 消息的去重中起作用 |
| snake_case | 本规格中 Python 侧建议命名；磁盘上的字段名一律保持 TS 的 camelCase |

---

## 1. 磁盘布局

### 1.1 根目录与 env 覆盖

`config.ts:36-46`（模块加载时求值一次，之后不再变）：

| 常量 | 值 | 是否受 env 影响 |
| --- | --- | --- |
| `MINI_CODE_DIR` | `process.env.MINI_CODE_HOME ? path.resolve(MINI_CODE_HOME) : path.join(os.homedir(), '.mini-code')` | ✅ 受 `MINI_CODE_HOME` 覆盖（相对值按进程 CWD 解析） |
| `MINI_CODE_SETTINGS_PATH` | `<MINI_CODE_DIR>/settings.json` | ✅ |
| `MINI_CODE_HISTORY_PATH` | `<MINI_CODE_DIR>/history.jsonl` | ✅ |
| `MINI_CODE_PERMISSIONS_PATH` | `<MINI_CODE_DIR>/permissions.json` | ✅ |
| `MINI_CODE_MCP_PATH` | `<MINI_CODE_DIR>/mcp.json` | ✅ |
| `MINI_CODE_MCP_TOKENS_PATH` | `<MINI_CODE_DIR>/mcp-tokens.json` | ✅ |
| `MINI_CODE_PROJECTS_DIR` | `<MINI_CODE_DIR>/projects` | ✅ |
| `CLAUDE_SETTINGS_PATH` | `os.homedir()/.claude/settings.json` | ❌ 硬编码 home（**不**受 `MINI_CODE_HOME`） |
| `PROJECT_MCP_PATH` | `process.cwd()/.mcp.json` | ❌ 模块加载时的 CWD |
| MCP 协议缓存 | `os.homedir()/.mini-code/mcp-protocol-cache.json` | ❌ 硬编码 home（`mcp.ts:65-69`） |
| 托管 skills 根 | `os.homedir()/.mini-code/skills`（user scope） | ❌ 硬编码 home（`skills.ts:70-74`） |
| 工具结果落盘 | `<MINI_CODE_DIR>/tool-results/<随机 UUID>/<toolUseId>.txt` | ✅ 前缀受覆盖；目录名是**进程级随机 UUID**（`tool-result-storage.ts:46-60`） |

Python：`Path(os.environ["MINI_CODE_HOME"]).resolve() if "MINI_CODE_HOME" in os.environ else Path.home()/".mini-code"`。
注意 Node 的 `path.resolve()` 对相对路径按进程 CWD 解析，Python 的 `Path.resolve()` 等价；
但 `os.homedir()` 在 Windows 上取 `USERPROFILE`，与 `Path.home()` 一致。

### 1.2 项目目录命名规则（精确字符串变换）

`session.ts:49-51`：

```ts
function projectDirName(cwd: string): string {
  return cwd.replace(/[/\\:]+/g, '-').replace(/^-+/, '')
}
```

规则：

1. 把**一个或多个连续**的 `/`、`\`、`:` 字符整体替换成单个 `-`（正则字符类是 `{/, \, :}`，`+` 量词）。
2. 再删除开头的所有 `-`（只删开头；结尾的 `-` 保留）。
3. 其它字符（含空格、`.`、Unicode、大小写）原样保留。**不做小写化、不做路径规范化、不做 realpath**。

Python 等价：

```python
import re
def project_dir_name(cwd: str) -> str:
    return re.sub(r"^-+", "", re.sub(r"[/\\:]+", "-", cwd))
```

示例：

| 输入 cwd | 输出目录名 |
| --- | --- |
| `D:\CoreCode\MiniCode` | `D-CoreCode-MiniCode` |
| `C:\Users\bob\proj` | `C-Users-bob-proj` |
| `D:/CoreCode` | `D-CoreCode` |
| `/home/bob/proj` | `home-bob-proj`（前导 `/` 被吃掉） |
| `\\server\share\proj` | `server-share-proj`（前导 `\\` 被吃掉） |
| `D:\CoreCode\` | `D-CoreCode-`（尾部 `-` 保留） |
| `d:\corecode` | `d-corecode`（大小写敏感，与 `D:\CoreCode` 是不同目录） |
| `D:\a-b` 与 `D:\a\b` | 都是 `D-a-b`（**非单射，会串目录**） |
| `D:\My Code\x` | `D-My Code-x`（空格保留） |

`projectDir(cwd)`（`session.ts:53-55`）= `path.join(MINI_CODE_PROJECTS_DIR, projectDirName(cwd))`。

### 1.3 会话文件命名

`session.ts:57-59`：

```ts
function sessionFilePath(cwd, sessionId) {
  return path.join(projectDir(cwd), `${sessionId}.jsonl`)
}
```

- 文件名 = `<sessionId>.jsonl`，`sessionId` **不**做任何净化（`--resume ../../x` 会路径穿越，见 gotchas）。
- 一个项目目录下可能有多个会话文件；除此之外持久化层不在项目目录里写任何其它文件。
- 文件是纯追加的 JSONL：**永不重写、永不截断**（除 `clearSession`/`cleanupExpiredSessions` 的 unlink）。

### 1.4 home 目录下 MiniCode 写入的全部文件

| 路径 | 写入者 | 内容 |
| --- | --- | --- |
| `<MINI_CODE_DIR>/projects/<projectDirName(cwd)>/<sessionId>.jsonl` | `saveSession`/`append*`/`renameSession`（`session.ts:250,286,313,366,525`） | 会话事件流 |
| `<MINI_CODE_DIR>/settings.json` | `saveMiniCodeSettings`（`config.ts:190-201`） | 合并后的 settings（2 空格缩进 + 末尾换行） |
| `<MINI_CODE_DIR>/history.jsonl` | `saveHistoryEntries`（`history.ts:34-70`） | 每行 `{display,timestamp,project,sessionId}`，最多 500 行 |
| `<MINI_CODE_DIR>/permissions.json` | `permissions.ts:154-155` | 权限白/黑名单 |
| `<MINI_CODE_DIR>/mcp.json` | `saveScopedMcpServers`（`config.ts:125-137`） | `{"mcpServers": {...}}` |
| `<MINI_CODE_DIR>/mcp-tokens.json` | `saveMcpTokensFile`（`config.ts:64-70`） | token 映射 |
| `<MINI_CODE_DIR>/tool-results/<随机 uuid>/<toolUseId>.txt` | `persistToolResult`（`tool-result-storage.ts:94-118`，`flag:'wx'`） | 超大工具输出 |
| `~/.mini-code/mcp-protocol-cache.json` | `mcp.ts:381-382` | MCP 协议缓存（绕过 `MINI_CODE_HOME`） |
| `~/.mini-code/skills/<name>/SKILL.md` | `installSkill`（`skills.ts:190-194`） | 用户级 skill（绕过 `MINI_CODE_HOME`） |
| `<cwd>/.mini-code/skills/<name>/SKILL.md` | 同上（project scope） | 项目级 skill |
| `<cwd>/.mcp.json` | `saveScopedMcpServers`（`config.ts:125-137`） | 项目级 MCP 配置（不属于会话子系统） |
| `<cwd>/.mini-code/`、`<cwd>/.gitignore`、`<cwd>/MINI.md` | `initializeRepo`（`init.ts:302-325`） | `minicode init` 的项目脚手架（不属于会话子系统） |

### 1.5 settings 合并顺序（读取侧，供上下文参考）

`loadEffectiveSettings`（`config.ts:173-188`）等价于：

```
mergeSettings(mergeSettings(mergeSettings(claudeSettings, {mcpServers: globalMcp}), {mcpServers: projectMcp}), miniCodeSettings)
```

`mergeSettings(base, override)`（`config.ts:139-171`）：浅展开 `{...base, ...override}`；`env` 按键合并
（override 胜）；`mcpServers` 逐 server 合并，server 内的 `env`/`headers` 再各做一次按键合并。

因此优先级（高 → 低）：`<MINI_CODE_DIR>/settings.json` > `<cwd>/.mcp.json` > `<MINI_CODE_DIR>/mcp.json` > `~/.claude/settings.json`。

`loadRuntimeConfig`（`config.ts:203-251`）再取 `env = {...settings.env, ...process.env}`（进程 env 胜），
`model = process.env.MINI_CODE_MODEL || settings.model || env.ANTHROPIC_MODEL`，
`maxOutputTokens = process.env.MINI_CODE_MAX_OUTPUT_TOKENS ?? settings.maxOutputTokens ?? env.MINI_CODE_MAX_OUTPUT_TOKENS`。
会话持久化不使用这些值（只影响 `sessionId` 之外的运行时），列出仅供对照。

---

## 2. JSONL 事件模式

### 2.1 事件对象字段

TS 类型 `SessionEvent`（`session.ts:33-47`）：

| 字段 | 类型 | 出现于 | 说明 |
| --- | --- | --- | --- |
| `type` | `EventType` | 所有事件 | 见 §2.2 |
| `subtype` | `string?` | 仅三种 boundary 事件（`session.ts:268,303,334`） | 值与 `type` 相同；**全代码库从不读取** |
| `message` | `ChatMessage?` | 所有 message 事件；`snip_boundary`；compact 的摘要事件 | 还原的唯一来源 |
| `uuid` | `string` | 所有事件 | 事件身份；message 事件里等于 `message.id` |
| `timestamp` | `string` | 所有事件 | `new Date().toISOString()`，UTC 毫秒精度，形如 `2024-05-05T12:34:56.789Z` |
| `sessionId` | `string` | 所有事件 | 写入时调用方传入；**从不读取** |
| `cwd` | `string` | 所有事件 | 写入时调用方传入；**从不读取** |
| `parentUuid` | `string \| null` | 除 `rename` 外的所有事件 | `null` 表示无前驱；**从不读取** |
| `logicalParentUuid` | `string \| null?` | 仅三种 boundary 事件 | 写边界前的“文件最后一行 uuid”；**从不读取** |
| `title` | `string?` | 仅 `rename` | §7 |
| `compactMetadata` | `{trigger, preTokens, postTokens}` | 仅 `compact_boundary` | `trigger ∈ {'auto','manual'}` |
| `snipMetadata` | `SnipBoundaryMetadata` | 仅 `snip_boundary` | 见下 |
| `contextCollapseSpan` | `CollapseSpan` | 仅 `context_collapse` | 见 §4.3 |

`SnipBoundaryMetadata`（`session.ts:24-31`）：`{type:'snip_boundary', removedMessageIds: string[], removedCount: number, tokensFreed: number, timestamp: string, createdAt: string}`。
注意 `timestamp`/`createdAt` 都是字符串（= 事件时间），而 `message.timestamp` 是**数字毫秒**。

`rename` 事件（`session.ts:516-523`）的键只有 6 个且顺序固定：
`{"type":"rename","title":...,"uuid":...,"timestamp":...,"sessionId":...,"cwd":...}` ——
**没有** `parentUuid`、`subtype`、`message`。

### 2.2 `type` 取值全集（`session.ts:22`）

```
'system' | 'user' | 'assistant' | 'thinking' | 'progress' | 'tool_call'
| 'tool_result' | 'summary' | 'compact_boundary' | 'snip_boundary'
| 'context_collapse' | 'rename'
```

实际会出现的组合：

| type | 由谁写 | 有 `message` | 会被 `loadSession` 还原 |
| --- | --- | --- | --- |
| `system` | `saveSession`（仅当 system 消息位于数组下标 ≥1） | ✔ | ✔ |
| `user` | `saveSession`；`appendCompactBoundary` 的摘要 | ✔ | ✔ |
| `assistant` | `saveSession` | ✔ | ✔ |
| `thinking` | `saveSession`（`assistant_thinking`） | ✔ | ✔ |
| `progress` | `saveSession`（`assistant_progress`） | ✔ | ✔ |
| `tool_call` | `saveSession`（`assistant_tool_call`） | ✔ | ✔ |
| `tool_result` | `saveSession` | ✔ | ✔ |
| `summary` | `saveSession`（`context_summary`） | ✔ | ✔ |
| `snip_boundary` | `appendSnipBoundary`；`saveSession`（fork 回放时把 `snip_boundary` 消息写回） | ✔ | ✔（但常被 replay 丢弃，见 §5.2） |
| `compact_boundary` | `appendCompactBoundary` | ✘ | ✘（仅用于定位切分点） |
| `context_collapse` | `appendContextCollapseSpan` | ✘ | ✘（由 `loadContextCollapseState` 单独读） |
| `rename` | `renameSession` | ✘ | ✘（由标题提取读取） |

### 2.3 `role` → `type` 映射（`session.ts:61-74`）

| `ChatMessage.role` | event `type` |
| --- | --- |
| `system` | `system` |
| `user` | `user` |
| `assistant` | `assistant` |
| `assistant_thinking` | `thinking` |
| `assistant_progress` | `progress` |
| `assistant_tool_call` | `tool_call` |
| `tool_result` | `tool_result` |
| `context_summary` | `summary` |
| `snip_boundary` | `snip_boundary` |
| 其它 / 未知 | `user`（default 分支，静默降级） |

反向不存在：`loadSession` 用**消息自身的 `role`**（`event.message.role`），不用 `event.type`。

### 2.4 `uuid` / `parentUuid` / `logicalParentUuid` 赋值规则

**`uuid`**

1. message 事件（`wrapEvent`，`session.ts:82-104`）：`uuid = ensureMessageId(message)`，
   即 `message.id` 若存在则复用，否则 `randomUUID()` **原地写入 `message.id`**（`session.ts:76-80`，会改变内存中的对象）。
2. `compact_boundary`：`randomUUID()`（`session.ts:335`）。
3. compact 摘要事件：`randomUUID()`（`session.ts:348`）。
4. `snip_boundary`：`ensureMessageId(boundaryMessage)`（`session.ts:264`），
   正常等于 snipCompact 生成的 `snip-<Date.now()>-<removedMessageIds[0] ?? 'none'>`（`snipCompact.ts:380-382`）。
5. `context_collapse`：`span.id`（`session.ts:304`），形如 `collapse-<ms>-<pass>-<startMessageId>`（`context-collapse.ts:669`）。
6. `rename`：`randomUUID()`（`session.ts:519`）。

**`parentUuid`（线性链）**：`saveSession` 在写之前调用 `readLastEventUuid(filePath)`（`session.ts:242`）：
读整个文件，取**最后一个非空行**的 `uuid`；文件不存在/为空/最后一行 JSON 解析失败 → `null`。
批量内后续消息的 `parentUuid` = 上一条刚写出的 `uuid`（`session.ts:247`）。
注意链会**穿过** boundary 事件：boundary 之后写入的消息，其 `parentUuid` 就是该 boundary 的 `uuid`。

**boundary 事件**：三个写入函数都先取 `lastUuid = readLastEventUuid(filePath)`，然后写
`parentUuid: null` + `logicalParentUuid: lastUuid`（`session.ts:274-275, 308-309, 339-340`）。
这就是“用 `logicalParentUuid` 代替线性 parent”的全部含义：boundary 是侧信道事件，
不把自己接进消息链，但把“写之前链尾是谁”记在 `logicalParentUuid` 里。

`parentUuid` / `logicalParentUuid` / `subtype` / `sessionId` / `cwd` **在读取侧全部未被使用**（见 §10 gotchas）：
恢复顺序完全由文件行序 + §5.2 的 replay 决定，与 parent 链无关。

`compact_boundary` 的摘要事件例外：`parentUuid = boundary.uuid`（`session.ts:351`），随后 retained 消息
从 `summary.uuid` 继续线性串联（`session.ts:358-364`）。

### 2.5 样例行（键顺序与 TS 的插入顺序一致）

```json
{"type":"user","message":{"role":"user","content":"fix the bug","id":"9f1c...8a"},"uuid":"9f1c...8a","timestamp":"2024-05-05T12:34:56.789Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null}
{"type":"compact_boundary","subtype":"compact_boundary","uuid":"5c2e...10","timestamp":"2024-05-05T12:40:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"9f1c...8a","compactMetadata":{"trigger":"manual","preTokens":120000,"postTokens":8000}}
{"type":"user","message":{"role":"user","content":"<summary text>"},"uuid":"77aa...31","timestamp":"2024-05-05T12:40:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":"5c2e...10"}
{"type":"snip_boundary","subtype":"snip_boundary","message":{"id":"snip-1714900000000-9f1c...8a","role":"snip_boundary","content":"[Snipped earlier conversation segment]\n...","removedMessageIds":["9f1c...8a","..."],"removedCount":12,"tokensFreed":3400,"timestamp":1714900000000},"uuid":"snip-1714900000000-9f1c...8a","timestamp":"2024-05-05T12:45:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"77aa...31","snipMetadata":{"type":"snip_boundary","removedMessageIds":["9f1c...8a","..."],"removedCount":12,"tokensFreed":3400,"timestamp":"2024-05-05T12:45:00.000Z","createdAt":"2024-05-05T12:45:00.000Z"}}
{"type":"context_collapse","subtype":"context_collapse","uuid":"collapse-1714900100000-0-9f1c...8a","timestamp":"2024-05-05T12:48:20.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"snip-1714900000000-9f1c...8a","contextCollapseSpan":{"id":"collapse-1714900100000-0-9f1c...8a","startMessageId":"...","endMessageId":"...","messageIds":["..."],"summary":"...","tokensBefore":9000,"tokensAfter":1400,"status":"committed","createdAt":1714900100000,"reason":"manual"}}
{"type":"rename","title":"my session","uuid":"3f0d...99","timestamp":"2024-05-05T12:49:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj"}
```

`CollapseSpan`（`context-collapse.ts:17-28`）：
`{id, startMessageId, endMessageId, messageIds: string[], summary: string, tokensBefore, tokensAfter, status: 'staged'|'committed', createdAt: number(ms), reason: 'context_pressure'|'manual'|'overflow_recovery'}`。
实际只会写入 `status:'committed'`（`context-collapse.ts:580-583`）与 `reason ∈ {'manual','context_pressure'}`（`overflow_recovery` 从未产生）。

---

## 3. `saveSession` 的追加算法

### 3.1 签名

```ts
export async function saveSession(
  cwd: string,
  sessionId: string,
  messages: ChatMessage[],
  alreadySavedCount: number = 0,
): Promise<void>            // session.ts:219-224
```

Python 建议：`def save_session(cwd: str, session_id: str, messages: list[dict], already_saved_count: int = 0) -> None`。

### 3.2 步骤（严格按序，`session.ts:225-251`）

1. `dir = projectDir(cwd)`；`filePath = sessionFilePath(cwd, sessionId)`。
2. `await mkdir(dir, {recursive:true})` —— **先建目录，后判空**，因此“什么都不写”也会创建目录。
3. `existingIds = await readExistingEventUuids(filePath)`（`session.ts:203-217`）：
   读整个文件；按 `content.trim().split('\n').filter(Boolean)` 分行；逐行 `JSON.parse`（失败即该行返回 `null` 并跳过）；
   把每个 `event.uuid` 为真值的加入 `set`。任何异常（含 ENOENT）→ 返回**空 set**（把读错误当作“文件不存在”）。
4. `nonSystemMessages = messages.slice(1)` —— **无条件丢弃下标 0 的消息，不检查其 role 是否为 system**。
5. `toSave = nonSystemMessages.filter((message, index) => {...})`，`index` 是**在 `nonSystemMessages` 中的下标**：

   | 情况 | 是否保存 |
   | --- | --- |
   | `message.id` 存在 且 `existingIds.has(id)` | 不保存 |
   | `message.id` 存在 且 不在 `existingIds` | 保存 |
   | `message.id` 缺失/空串/`null` | 仅当 `index >= alreadySavedCount` 时保存 |

   注意这是 `filter`，**不是**从头扫描到第一个已保存就停：`index=2` 被跳过、`index=3` 仍可被保存。
6. `if (toSave.length === 0) return` —— 不触碰文件（但 `mkdir` 已发生）。
7. `parentUuid = await readLastEventUuid(filePath)`（`session.ts:191-201`）：最后一个非空行解析出的 `uuid ?? null`；
   读文件失败/空文件 → `null`。
8. 逐条：`line = wrapEvent(m, sessionId, cwd, parentUuid)` → 再 `JSON.parse(line)` 取回 `uuid` 作为下一条的 `parentUuid`
   （即：`parentUuid` 来自刚序列化的对象，等价于 `ensureMessageId(m)`）。
9. `appendFile(filePath, lines.join('\n') + '\n', 'utf8')` —— 单次追加，**末尾额外一个 `\n`**，
   批内用 `\n` 连接（没有前导换行、没有 fsync、没有锁、没有原子 rename）。

`wrapEvent`（`session.ts:82-104`）产出的键顺序固定：
`type, message, uuid, timestamp, sessionId, cwd, parentUuid`，若 `message.role === 'snip_boundary'`
再追加 `snipMetadata`（此时 `snipMetadata.timestamp = snipMetadata.createdAt = event.timestamp`，
而 `message.timestamp` 保持原数字值）。

`parseEvent`（`session.ts:106-112`）：`JSON.parse` 失败返回 `null`，成功则原样返回（不校验结构）。
`unwrapMessage`（`session.ts:114-122`）：`event.message` 存在时返回 `{...event.message, id: event.uuid}`
（**`id` 一律以事件 `uuid` 覆盖**），否则 `null`。

### 3.3 去重规则的本质与后果

- 去重主键是 `uuid`（= `message.id`），**与内容无关**：同一条消息改了内容再存，仍会被跳过（磁盘保留旧内容）。
- 没有 `id` 的消息只能靠位置游标：只要 `alreadySavedCount` 之后的下标区间覆盖它就会被写；
  写完后 `ensureMessageId` 给它补上 `id`，此后就走 uuid 去重。
- 因为 `filter` 不截断，混排（有 id / 无 id）时可能“跳着写”，文件里不保证同一批消息连续成链。
- `existingIds` 包含**所有**事件的 uuid：boundary、collapse、rename 的 uuid 也在集合里。
  若某条消息的 `id` 恰好等于某个 rename uuid（理论上不可能，除非人为构造），它会被静默跳过。

### 3.4 一次调用完全不写文件的充要条件

1. `toSave` 为空（文件名已存在且所有带 id 消息都已在文件中，且所有无 id 消息的 `index < alreadySavedCount`）。
2. 没有别的“空写”分支：`appendFile` 永远收到至少一行。目录的 `mkdir` 不属于“写入”。

### 3.5 `alreadySavedCount` 游标语义与全部赋值点

语义：**已经持久化过的非系统消息条数**（因为 `saveSession` 从 `slice(1)` 开始，所以调用方一律用 `messages.length - 1` 维护）。
它只影响“无 id 消息”的取舍；所有赋值点：

| 位置 | 赋值 | 触发时机 |
| --- | --- | --- |
| `index.ts:153` | `0` | 进程启动、全新会话 |
| `tty-app.ts:1026` | `loaded.length` | `resumeSession` 成功（`loaded` 是 `loadSession` 的返回，不含 system） |
| `tty-app.ts:1059` | `messages.length - 1` | 处理 `/new`、`/resume`、`/fork` 之前先补一次 `saveSession` |
| `tty-app.ts:1152` | `0` | 手动 `/snip` 成功后 |
| `tty-app.ts:1204` | `messages.length - 1` | 手动 `/compact` 成功后 |
| `tty-app.ts:1319` | `0` | `/new`（清空会话） |
| `tty-app.ts:1343` | `messages.length - 1` | `/fork` 成功后 |
| `tty-app.ts:1477` | `result.messages.length - 1` | 回合内 auto-compact 回调 |
| `tty-app.ts:1499` | `0` | 回合内 auto-snip 回调（**即使没有真的 snip 也会置 0**） |
| `tty-app.ts:1660` | `messages.length - 1` | 每个回合结束（`splice` 之后） |
| `tty-app.ts:1775` | `messages.length - 1` | `recordAnswer`（`ask_user` 的回答落盘后） |

推论：`alreadySavedCount = 0` 会让**所有当前无 id 的消息**（包括此前因位置被跳过的 `context_summary`）
在下一次 `saveSession` 时被写入；而 `messages.length - 1` 会跳过所有“早于游标且无 id”的消息。

---

## 4. 三种边界事件（+rename）的精确写入序列

### 4.1 `appendCompactBoundary` —— 三部分

```ts
appendCompactBoundary(cwd, sessionId, summaryText, trigger: 'auto'|'manual',
                      preTokens, postTokens, retainedMessages: ChatMessage[] = [])
// session.ts:316-324
```

步骤（`session.ts:325-366`）：

1. `mkdir(projectDir(cwd), {recursive:true})`。
2. `lastUuid = readLastEventUuid(filePath)`；`now = new Date().toISOString()`。
3. 第 1 部分 —— 边界事件（写入但**不带 `message`**）：
   `{type:'compact_boundary', subtype:'compact_boundary', uuid: randomUUID(), timestamp: now, sessionId, cwd, parentUuid: null, logicalParentUuid: lastUuid, compactMetadata:{trigger, preTokens, postTokens}}`。
4. 第 2 部分 —— 摘要事件（**伪装成普通 user 消息**）：
   `{type:'user', message:{role:'user', content: summaryText}, uuid: randomUUID(), timestamp: now, sessionId, cwd, parentUuid: boundary.uuid}`。
   注意：内存里的 `result.summary` 是 `context_summary` 角色对象，**不是**写进文件的这个对象；
   文件里这一行不携带 `compressedCount`/`timestamp`，也没有 `id`。
5. 第 3 部分 —— `retainedMessages` 逐条 `wrapEvent(message, sessionId, cwd, parentUuid)`，
   `parentUuid` 初始为 `summary.uuid`，之后依次串联。
   调用方传入的 retained 列表由 `retainedMessagesAfterCompact` 生成（`tty-app.ts:898-902`）：
   `result.messages.filter(m => m.role !== 'system' && m !== result.summary)`（**对象身份比较**）。
   这些消息大多**保留原有 uuid**（此前已写过一遍），所以文件里会出现**同一 uuid 两行**（边界前 + 边界后）。
6. 一次性 `appendFile(lines.join('\n') + '\n')`，顺序固定为：boundary → summary → retained…。

### 4.2 `appendSnipBoundary`

```ts
appendSnipBoundary(cwd, sessionId, boundaryMessage: Extract<ChatMessage,{role:'snip_boundary'}>)
// session.ts:253-257
```

步骤（`session.ts:258-287`）：`mkdir` → `lastUuid` → `now` → `uuid = ensureMessageId(boundaryMessage)`
（正常已有 `snip-<ms>-<firstRemovedId>`，**原地写回 `message.id`**）→ 写**单个**事件：

```json
{"type":"snip_boundary","subtype":"snip_boundary","message":<boundaryMessage>, "uuid":<同上>,
 "timestamp":now,"sessionId":..., "cwd":...,
 "parentUuid":null,"logicalParentUuid":lastUuid,
 "snipMetadata":{"type":"snip_boundary","removedMessageIds":[...],"removedCount":N,
                 "tokensFreed":T,"timestamp":now,"createdAt":now}}
```

`boundaryMessage` 由 `snipCompact.ts:374-393` 构造：`id = "snip-<Date.now()>-<removedMessageIds[0] ?? 'none'>"`，
`content = buildSnipBoundaryContent({removedCount, tokensFreed})`（`snipCompact.ts:348-363`，多行英文模板），
`tokensFreed` 是该消息的字段值 = `deletion.tokens - boundaryTokens`（`snipCompact.ts:456-479`），
与调用方 UI 用的 `result.tokensFreed`（真实测量差值）**不同**。
`removedMessageIds` 由 `messageId(message, index) = message.id ?? "message-<index>"` 生成（`snipCompact.ts:84-86, 452-455`），
所以**没有 id 的被移除消息贡献的是合成 id（`message-<下标>`），永远匹配不上磁盘 uuid**。

### 4.3 `appendContextCollapseSpan`

```ts
appendContextCollapseSpan(cwd, sessionId, span: CollapseSpan)  // session.ts:289-293
```

步骤（`session.ts:294-314`）：`mkdir` → `lastUuid` → `now` → 写单事件：

```json
{"type":"context_collapse","subtype":"context_collapse","uuid":<span.id>,
 "timestamp":now,"sessionId":...,"cwd":...,
 "parentUuid":null,"logicalParentUuid":lastUuid,"contextCollapseSpan":<span 原样>}
```

- `uuid` 直接复用 `span.id`；`timestamp` 是写入时刻，span 内的 `createdAt` 是生成时刻（数字毫秒）。
- 调用方 `persistContextCollapseResult`（`tty-app.ts:904-922`）会为 `result.spans`（非空时）或 `[result.span]`
  逐个调用，**按 spans 数组顺序**写入；返回 `Σ max(0, tokensBefore - tokensAfter)`。
- 该写入**不改变** `alreadySavedCount`，也不把投影摘要写进 JSONL：只有 span 落盘，
  model-visible 的 `context_summary` 投影消息仅存在于内存（`context-collapse.ts:143-153` 的 id 形如 `collapse-summary-<spanId>`）。

### 4.4 `rename` 事件

`renameSession`（`session.ts:505-527`）：先 `readFile` 探测存在性（失败 → 返回 `false`），
再 `mkdir`（防御目录被删）并追加一行。字段严格为
`type, title, uuid, timestamp, sessionId, cwd`（无 `parentUuid`/`message`/`subtype`）。
不修改任何内存状态；标题以**文件里最后一个 rename 事件**为准（§7）。

---

## 5. 恢复算法

### 5.1 `loadSession`（`session.ts:369-404`）

```
content = read(filePath)                      # 任何异常 → return None
lines   = content.trim().split('\n').filter(nonempty)

lastBoundaryIndex = 从 lines.length-1 向前扫描，第一个 parseEvent(line).type == 'compact_boundary' 的下标
startLine = lastBoundaryIndex >= 0 ? lastBoundaryIndex + 1 : 0
activeEvents = [parseEvent(lines[i]) for i in startLine..lines.length-1 if 解析成功]   # 保序，丢 null

messages = []
for event in reconstructSnippedEvents(activeEvents):
    msg = unwrapMessage(event)                # 无 message 字段则 None
    if msg: messages.append(msg)

return messages if messages else None
```

关键点：

- 只截取**最后一个** `compact_boundary` **之后**的行；boundary 行本身不进 `activeEvents`，
  但它后面那行“user 摘要”会进（摘要因此成为恢复后的第 1 条消息）。
- 前面的内容（含所有旧 boundary、rename、旧 collapse、被 snip 的原始消息）**不参与**恢复，
  但上下文压缩状态另有入口（§5.3）。
- `rename` / `context_collapse` 没有 `message` → 被 `unwrapMessage` 丢弃，不进消息数组。
- 返回值是消息数组（不含 system）或 `None`；空文件 → `None`。调用方约定“`None` 或空数组”= 会话不存在
  （`tty-app.ts:1292,1305`；`forkSession` 的 `session.ts:534`）。

### 5.2 `reconstructSnippedEvents` —— snip 回放的精确语义（`session.ts:124-167`）

```
snipEvents = [ e for e in events
               if e.type == 'snip_boundary' and e.snipMetadata
                  and len(e.snipMetadata.removedMessageIds) > 0 ]
if not snipEvents:
    return events                      # 原样返回：此时 removedMessageIds 为空的 snip 事件会保留

removedIdToSnips = {}                  # 按 snipEvents 的（=文件）顺序 append
for snip in snipEvents:
    for removedId in snip.snipMetadata.removedMessageIds:
        removedIdToSnips.setdefault(removedId, []).append(snip)

inserted = set(); result = []
for e in events:
    if e.type == 'snip_boundary':
        continue                       # 无条件丢弃**所有** snip 事件（含未被收录的）
    snips = removedIdToSnips.get(e.uuid) or []
    if snips:
        for s in snips:
            if s.uuid not in inserted:
                result.append(s); inserted.add(s.uuid)
        continue                       # 这条“被移除”的事件本身不进结果
    result.append(e)
return result
```

精确定序结论：

1. **所有** `snip_boundary` 事件先被从原位置删除。
2. 每个 snip 被重新插入到**文件中第一条 uuid 命中了它 `removedMessageIds` 的事件所在的位置之前**，
   而被命中的那条事件（以及同一 snip 命中的后续事件）被丢弃。
   - 一条 snip 命中多条事件时，只在**第一次**命中处插入一次（`inserted` 集合去重）。
   - 多个 snip 命中同一 uuid 时，按 `snipEvents` 顺序依次插入在该位置。
3. 因为被丢弃的原始消息在文件里仍存在，snip 之后写下的消息排在文件尾部，回放后顺序自然变成
   `[snip 之前保留的…, snip 边界, snip 之后写的…]`，与内存中 snip 后的数组一致。
4. 若某 snip 的 `removedMessageIds` **一条都匹配不上**当前 `events`（例如被 compact 切掉、
   或 fork 之后被移除的消息根本没被复制），该 snip 事件**被彻底丢弃**（不会出现在结果里）。
5. 若某 snip 的 `removedMessageIds` 为空数组，它不在 `snipEvents` 里；只要文件里存在**任意**一个非空 snip，
   它也会在循环里被 `continue` 丢掉（只有“全文件没有任何非空 snip”时它才保留）。
6. 匹配用 `event.uuid`（原始 JSON 里的字段），与 `unwrapMessage` 后写入的 `id` 同值。

`loadTranscript` 用的也是同一个函数，但传入的是**整个文件**的所有事件（不做 compact 切分），
因此 transcript 里被 snip 掉的历史同样被隐去，且 snip 边界被搬到第一条被移除消息的位置。

### 5.3 `loadContextCollapseState`（`session.ts:406-439`）

```
content = read(filePath)                     # 异常 → None
lines = ...
lastBoundaryIndex = 最后一个 type=='compact_boundary' 的下标（找不到为 -1）
state = createContextCollapseState()          # {spans: [], enabled: True, consecutiveFailures: 0}
for i in lastBoundaryIndex+1 .. lines.length-1:
    e = parseEvent(lines[i])
    if e is None or e.type != 'context_collapse' or not e.contextCollapseSpan: continue
    if e.contextCollapseSpan.status != 'committed': continue
    state.spans.append(e.contextCollapseSpan)
return state if state.spans else None
```

- 与 `loadSession` 使用**同一个**切分点（最后一个 compact boundary），但起点写成 `lastBoundaryIndex + 1`，
  未找到时 `-1 + 1 = 0`，等价。
- 只接受 `status == 'committed'`；`'staged'` 会被跳过。
- `enabled` / `consecutiveFailures` **不持久化**：恢复出来的状态永远是 `enabled=True, consecutiveFailures=0`
  （“连续失败后禁用 collapse”只活在进程内存里）。
- 不去重：同一 span 若被写两次，会出现两个副本（`projectCollapsedView` 会用 `occupiedIndices` 防重叠）。

### 5.4 调用方编排（`resumeSession`，`tty-app.ts:976-1031`）

1. `args.sessionId = sessionId`；`args.plan.reset(sessionId)`。
2. 记住当前 system 内容 → `refreshSystemPrompt()`（重新构建 prompt）→
   **丢弃刷新结果**，把 `args.messages` 重置为 `[旧的 system 内容, ...loaded]`（`tty-app.ts:984-989`）。
3. `state.transcript = []`；`loadTranscript(cwd, sessionId)`：
   - 非空 → 逐条 `pushTranscriptEntry`；
   - `null`/空 → **降级**用 `loaded` 消息渲染（`tty-app.ts:996-1021`，字符串与 `loadTranscript` 不同，见 §6.14）。
4. 追加一条 `Session {sessionId} resumed ({loaded.length} messages loaded).`。
5. `args.alreadySavedCount = loaded.length`。
6. `args.contextCollapseState = (await loadContextCollapseState(cwd, sessionId)) ?? createContextCollapseState()`。

---

## 6. 导出函数规格

所有函数都是 `async`，所有文件系统错误在读取路径上被吞掉（返回 `null`/空/0），
在写入路径上**不**吞（`appendFile`/`mkdir` 失败会 reject 到调用方）。

### 6.1 `saveSession`

见 §3。签名 `session.ts:219-224`。副作用：创建项目目录、**原地给消息补 `id`**、追加 1..n 行。

### 6.2 `appendSnipBoundary`

见 §4.2。签名 `session.ts:253-257`。副作用：`mkdir`、给 boundaryMessage 补 `id`、追加 1 行。
调用点：`tty-app.ts:1149`（手动 `/snip`）、`tty-app.ts:1497`（回合内 auto-snip）。

### 6.3 `appendContextCollapseSpan`

见 §4.3。签名 `session.ts:289-293`。副作用：`mkdir`、追加 1 行。
调用点：`tty-app.ts:915`（`persistContextCollapseResult`，手动 `/collapse` 与回合内 collapse 共用）。

### 6.4 `appendCompactBoundary`

见 §4.1。签名 `session.ts:316-324`。副作用：`mkdir`、追加 1+n 行。
调用点：`tty-app.ts:1193`（手动 `/compact`，trigger `'manual'`）、`tty-app.ts:1468`（回合内 auto-compact，trigger `'auto'`）。

### 6.5 `loadSession`

见 §5.1。签名 `session.ts:369-372` → `Promise<ChatMessage[] | null>`（不含 system 消息）。

### 6.6 `loadContextCollapseState`

见 §5.3。签名 `session.ts:406-409` → `Promise<ContextCollapseState | null>`。

### 6.7 `clearSession`

`session.ts:441-460`：

```
try: unlink(sessionFilePath(cwd, sessionId))     # 任何异常忽略
try:
    files = readdir(projectDir(cwd))
    if len(files) == 0: rm(projectDir(cwd), recursive=True, force=True)
except: pass
```

- 只删那一个文件；**不**判断被删的是不是当前会话；不清理内存状态。
- 目录空才删目录（`force: true` 让并发删除不报错）。
- 调用点：会话选择器里连按两次 `d`（`tty-app.ts:2171`）。

### 6.8 `listSessions`

```ts
type SessionMeta = { id: string; title: string | undefined; messageCount: number; updatedAt: number }
// session.ts:462-467
export async function listSessions(cwd: string): Promise<SessionMeta[]>   // session.ts:469
```

- `readdir(projectDir(cwd))` 失败 → `[]`。
- 只取文件名以 `.jsonl` 结尾的项；`id = name.slice(0, -6)`（去掉 `.jsonl`）。
- 对每个文件：`stat` 取 `mtime.getTime()`（毫秒）→ `updatedAt`；读全文 → `lines`；
  `title = extractTitleFromEvents(lines)`（§7）。
- **`messageCount = lines.length`，即“事件行数”，不是消息条数**（含 boundary/collapse/rename/retained 重复行）。
- 单个文件 `stat`/`readFile` 抛错（例如同名目录）→ 整体跳过该文件。
- 排序：`results.sort((a,b) => b.updatedAt - a.updatedAt)`，即按 mtime 降序；
  JS 的 `Array.sort` 稳定，等值时保持 `readdir` 顺序（Python `sorted(..., key=..., reverse=True)` 同样稳定，
  但 `readdir` 顺序本身依赖平台，不要依赖它）。

### 6.9 `renameSession`

```ts
export async function renameSession(cwd, sessionId, newTitle: string): Promise<boolean>  // session.ts:505-509
```

1. `await readFile(sessionFilePath(cwd, sessionId))`；抛错 → `return false`（**不创建**会话）。
2. 构造 §4.4 的 rename 事件（`uuid = randomUUID()`，`timestamp` 为当时 ISO 字符串）。
3. `mkdir(projectDir(cwd), {recursive:true})`（防御性；目录此前被 `clearSession` 删掉时重建）。
4. 追加该行 → `return true`。
5. 调用点：`/rename <name>`（`tty-app.ts:1241-1255`，空名字被拒绝）、`forkSession`（`session.ts:552`）。

### 6.10 `forkSession`

```ts
export async function forkSession(cwd, sessionId): Promise<string | null>   // session.ts:529-532
```

1. `loaded = await loadSession(cwd, sessionId)`；`null` 或空 → `return null`。
2. `newId = randomUUID().slice(0, 8)`。
3. `await saveSession(cwd, newId, [{role:'system', content:''}, ...loaded])` ——
   `alreadySavedCount` 取默认 `0`；因为新文件不存在，`existingIds` 为空，所有消息都满足
   “有 id 且不在集合中” → 全部写出，且**沿用原会话的 uuid**（`unwrapMessage` 把 `event.uuid` 写成了 `id`）。
   父链则重新线性生成（首条 `parentUuid = null`）。
4. `allSessions = await listSessions(cwd)`（**在写完新文件之后**，所以新会话也在结果里）。
5. `source = allSessions.find(s => s.id === sessionId)`（**源会话**）；`baseTitle = source?.title ?? 'session'`。
6. `forkPrefix = baseTitle + '_fork'`；
   `nums = allSessions.filter(s => s.title?.startsWith(forkPrefix)).map(s => parseInt(s.title.slice(forkPrefix.length), 10)).filter(n => !isNaN(n))`。
7. `nextNum = nums.length ? Math.max(...nums) + 1 : 1`。
8. `await renameSession(cwd, newId, `${baseTitle}_fork${nextNum}`)`。
9. `return newId`。

注意：`parseInt` 容忍尾随垃圾（`"2abc"` → 2、`"1_fork2"` → 1、`""` → `NaN` 被过滤），
且既有的 fork 标题本身也会参与“以 prefix 开头”的匹配（会产生 `x_fork1_fork1` 这类嵌套名）。
即便 rename 失败（返回 `false`），也仍然 `return newId`（fork 文件已存在但没标题）。

调用点：`/fork`（`tty-app.ts:1332-1351`，随后 `args.alreadySavedCount = messages.length - 1`）、
启动参数 `--fork <id>`（`index.ts:130-139`，把 `sessionId` 换成新 id 并把 `resumeTarget` 指向它）。

### 6.11 `cleanupExpiredSessions`

```ts
export async function cleanupExpiredSessions(cwd, maxAgeMs: number): Promise<number>  // session.ts:557-560
```

1. `readdir(projectDir(cwd))` 失败 → `0`。
2. 对每个 `.jsonl`：`stat`；若 `Date.now() - mtime > maxAgeMs` → `unlink` 并 `removed += 1`；
   `stat`/`unlink` 抛错则跳过（不计入）。
3. 再次 `readdir`；为空则 `rm(dir, {recursive:true, force:true})`。
4. 返回删除个数。调用点：`tty-app.ts:1825`，仅在**没有** `resumeTarget` 的启动路径执行，
   参数固定 `30 * 24 * 60 * 60 * 1000`（30 天），结果用于提示文案。
   判定依据是 **mtime**，不是事件里的 `timestamp`。

### 6.12 `listAllProjects`

```ts
type ProjectMeta = { dir: string; sessionCount: number; latestUpdatedAt: number }  // session.ts:597-601
export async function listAllProjects(): Promise<ProjectMeta[]>                    // session.ts:603
```

- `readdir(MINI_CODE_PROJECTS_DIR)` 失败 → `[]`（**无参数**，只扫默认 projects 根）。
- 只处理 `isDirectory()` 的项；`readdir` 后统计 `.jsonl` 个数，为 0 则跳过；
  `latestUpdatedAt = max(mtime)`；`dir = 目录项名字`（即 §1.2 的**变换后**名字，不是真实路径）。
- 按 `latestUpdatedAt` 降序排序。
- 调用点：会话选择器按 Tab 时的 all-projects 视图（`tty-app.ts:2194`），
  渲染成 `${p.dir}  ${p.sessionCount} sessions  ${ago}`（`tty-app.ts:801`），
  回车提示用户 `cd <project-path> && minicode --resume`（`tty-app.ts:2116`）——而 `dir` 并不是可用路径。

### 6.13 `loadTranscript`

```ts
export type PersistedTranscriptEntry =
  | { kind: 'user' | 'assistant' | 'progress'; body: string }
  | { kind: 'tool'; body: string; toolName: string; status: 'running' | 'success' | 'error' }
// session.ts:643-645
export async function loadTranscript(cwd, sessionId): Promise<PersistedTranscriptEntry[] | null>  // session.ts:647-650
```

算法（`session.ts:651-721`）：

1. 读全文 → `lines`；**不做 compact boundary 切分**（整份历史都要渲染）。
2. `events = reconstructSnippedEvents(所有成功解析的事件)`（保序，null 行丢弃）。
3. `toolEntries: dict[str, entry]`；遍历 `events`，`msg = event.message ?? {}`（不校验类型），按 `event.type` 分派：

   | type | 行为 |
   | --- | --- |
   | `user` | `msg.internal` 为真 → **跳过**；否则 push `{kind:'user', body: msg.content if str else ''}` |
   | `assistant` | push `{kind:'assistant', body: str(msg.content) or ''}` |
   | `progress` | push `{kind:'progress', body: str(msg.content) or ''}` |
   | `tool_call` | 先构造占位 entry `{kind:'tool', toolName: str(msg.toolName) or 'unknown', status:'error', body:'No saved result for this tool call.'}`，push 同一个对象；若 `isinstance(msg.toolUseId, str)` 则 `toolEntries[msg.toolUseId] = entry` |
   | `tool_result` | `entry = toolEntries.get(msg.toolUseId) if isinstance(msg.toolUseId, str) else None`；命中的话**原地改写** `entry.status = 'error' if msg.isError else 'success'`、`entry.body = str(msg.content) or ''`；未命中则**什么都不做**（孤儿结果被丢弃，不 push） |
   | `summary` | push `{kind:'assistant', body: f"[Context summary: {msg.compressedCount if not None else 0} messages compressed]"}` |
   | `compact_boundary` | push `{kind:'assistant', body: f"[Context compacted: {preTokens ?? '?'} → {postTokens ?? '?'} tokens]"}`（箭头是 U+2192，`?` 是字符串） |
   | `snip_boundary` | push `{kind:'assistant', body: f"[Snipped earlier context: removed {removedCount ?? '?'} messages, freed ~{tokensFreed ?? '?'} tokens]"}` |
   | `system` / `thinking` / `context_collapse` / `rename` | 无分支 → 忽略 |

4. 返回 `entries` 或 `None`（空/读取异常）。

必须保持的实现细节：

- **对象身份共享**：占位 entry 同时被 push 进 `entries` 并登记进 `toolEntries`，
  后续 `tool_result` 是**原地 mutate**（Python 用同一个 dict 引用即可）。
- 同一 `toolUseId` 出现两次 `tool_call` 时，后一个**覆盖** map，前一个 entry 永远停在占位文案
  `'No saved result for this tool call.'`（status `'error'`）。
- `tool_result` 必须出现在对应 `tool_call` **之后**才能回填（顺序由 `saveSession` 保证）。
- 空 `body`：`msg.content` 不是字符串时取 `''`（例如内容字段缺失）。
- `internal` 标记（`'goal'|'loop'|'continuation'`）的 user 消息既不渲染也不参与标题。
- 因为不做边界切分 + retained 消息被重复写入，**compact 之后的历史会在 transcript 里重复出现**（见 gotchas）。
- 摘要文案：`context_summary` 消息若被 `saveSession` 落盘（`type:'summary'`）就用 `compressedCount`；
  compact 写入的伪装 user 摘要则会渲染成一条普通 `user` 条目（紧跟 `[Context compacted: …]` 之后）。

### 6.14 调用方的降级渲染（与 6.13 文案不一致，必须知道）

当 `loadTranscript` 返回 `null`/空时，`resumeSession` 用 `loaded` 自己渲染（`tty-app.ts:996-1021`）：

| 消息角色 | 条件 | 渲染 |
| --- | --- | --- |
| `user` | `not msg.internal` | `{kind:'user', body: msg.content}` |
| `assistant` | — | `{kind:'assistant', body: msg.content}` |
| `assistant_tool_call` | — | `{kind:'tool', toolName, status:'success', body: summarizeToolInput(toolName, input)}`（**没有 tool_result 回填**） |
| `context_summary` | — | `[Context summary: {compressedCount} messages compressed]` |
| `snip_boundary` | — | `Snipped earlier context: removed {removedCount} messages, freed ~{Math.round(tokensFreed)} tokens.`（无方括号、四舍五入、句尾句点） |
| 其它 | — | 不渲染 |

---

## 7. 标题推导与 fork 命名

`extractTitleFromEvents(lines)`（`session.ts:169-189`）：

1. **第一遍**：顺序扫描全部行，记录**最后一个** `type === 'rename'` 且 `typeof title === 'string'` 的 `title`
   （空字符串也算命中，但因为 `if (renameTitle)` 判真，空串会被当作“没有 rename”而落到第二遍）。
   有结果则直接返回。
2. **第二遍**：顺序扫描，取第一条满足以下全部条件的 `user` 事件：
   - `event.type === 'user'`；
   - `event.message.internal` 为假（falsy）；
   - `typeof event.message.content === 'string'` 且 `content.trim()` 非空。
   取 `text = content.trim()`；若 `text.length > 60`（`MAX_TITLE_LENGTH = 60`，`session.ts:20`）
   返回 `text.slice(0,60) + '...'`（**总长最多 63**），否则原样返回 `text`。
3. 都不满足 → `undefined`。

要点：

- 标题**不落盘、不缓存**，每次 `listSessions` 现算（O(文件大小)）。
- 截断按 **UTF-16 code unit**（Python 的 `s[:60]` 是 code point，含 emoji/代理对时会与 TS 有 1 字符级差异；
  要完全一致需按 `len(s.encode('utf-16-le'))` 折算，实际可选）。
- 不清理换行：多行首条用户消息会产生多行标题。
- 因为 compact 摘要是 `type:'user'`，若某会话的第一条 `user` 事件恰是 compact 摘要，标题就会变成摘要正文。
- `rename` 与标题是**追加**关系：多次 `/rename` 以最后一次为准；无法“改回”自动标题。

fork 命名规则：`(`${baseTitle}_fork${nextNum}`)`，其中 `baseTitle` 取源会话当前的推导标题（含 rename），
失败时回退 `'session'`；`nextNum` 为“所有标题以 `<baseTitle>_fork` 开头的会话”的 `parseInt` 后缀最大值 + 1（无则 1）。
详见 §6.10。注意 `baseTitle` 本身可能是 `x_fork1`，于是产生 `x_fork1_fork1`。

---

## 8. 与压缩层的耦合

### 8.1 每种压缩 → 落盘产物 → 内存游标

| 触发 | 落盘调用 | 写入内容 | `alreadySavedCount` | 其它内存动作 |
| --- | --- | --- | --- | --- |
| 手动 `/compact`（`tty-app.ts:1169-1239`） | `appendCompactBoundary(..., 'manual', tokensBefore, tokensAfter, retained)`（`:1193-1201`） | boundary + 伪装 user 摘要 + retained 消息 | `args.messages.length - 1`（`:1204`） | `args.messages = result.messages`（`:1202-1203`）；`contextCollapseState = createContextCollapseState()`（`:1205`） |
| 回合内 auto-compact（`tty-app.ts:1459-1483`） | `appendCompactBoundary(..., 'auto', ...)`（`:1468-1476`） | 同上 | `result.messages.length - 1`（`:1477`） | 循环内部已替换局部 `messages`；`contextCollapseState` 在 `agent-loop.ts:263` 重置；回合末 `tty-app.ts:1658-1660` 再 splice+save 并**再次**改写游标 |
| 手动 `/snip`（`tty-app.ts:1132-1166`） | `appendSnipBoundary(cwd, sessionId, result.boundaryMessage)`（`:1149`） | 单个 `snip_boundary` 事件 | `0`（`:1152`） | `args.messages = result.messages`（`:1150-1151`）；`contextCollapseState = createContextCollapseState()`（`:1153`） |
| 回合内 auto-snip（`tty-app.ts:1495-1510`） | `appendSnipBoundary(...)`（`:1497`） | 同上 | `0`（`:1499`，**即使 `boundaryMessage` 不是 snip_boundary 也置 0**） | 循环内部已替换 `messages`（`agent-loop.ts:219`） |
| 手动 `/collapse`（`tty-app.ts:1067-1129`） | `persistContextCollapseResult` → `appendContextCollapseSpan`×N（`:1096`、`:904-922`） | N 个 `context_collapse` 事件 | 不变 | `args.contextCollapseState = result.state`；`result.messages` 只用于统计 |
| 回合内 collapse（`tty-app.ts:1484-1494`） | 同上（`:1486`） | 同上 | 不变 | 循环内部已 `replaceContextCollapseState`（`agent-loop.ts:241`） |

压缩层产出的具体对象：

- auto/manual compact：`CompressionResult`（`types.ts:115-121`）来自 `compactConversation`（`compact.ts:124-198`）：
  `messages = [...systemMessages, summaryMessage, ...messagesToKeep]`，`summaryMessage.role='context_summary'` 且**没有 `id`**，
  `removedCount = messagesToCompress.length`，`tokensBefore/After = tokenCountWithEstimation(...)`。
- snip：`SnipCompactResult`（`snipCompact.ts:23-32`）；`boundaryMessage` 是 `messagesAfterSnip[deletion.start]`（`:487`），
  `removedMessageIds = messageId(message, index)`（`:452-455`）；`tokensFreed = max(0, tokensBefore - tokensAfter)`（`:481`）。
- collapse span：`CollapseSpan`（`context-collapse.ts:17-28`），id 形如 `collapse-<ms>-<pass>-<startMessageId>`（`:669`），
  提交时 `status` 改成 `'committed'`（`:580-583`）。

### 8.2 为什么游标要这样改

- compact 之后：boundary + 摘要 + retained 已经落盘，而内存数组下标含义已变
  （`[system, context_summary(无 id), ...retained, ...之后新增]`），
  置 `messages.length - 1` 使得**摘要这条无 id 消息被跳过**（下标 0 < 游标），
  而之后新增的无 id 消息（下标 ≥ 游标）会被写入。
- snip 之后：内存数组被重建，置 `0` 让所有无 id 消息都有机会写入；
  已写入的（有 id）消息靠 uuid 去重不重复。
- collapse：不改变 `args.messages`（投影只用于 model-visible 视图），所以不需要动游标。

### 8.3 恢复侧的对接

- `loadSession` 只认最后一个 `compact_boundary`；snip 靠 §5.2 回放；collapse 靠 `loadContextCollapseState`。
- 恢复后 `args.messages` 的第一条摘要消息是**普通 `user` 消息**（compact 写入的伪装摘要），
  而不是 `context_summary`；`context_summary` 只有在 `saveSession` 恰好写过它时（`type:'summary'`）才存在。
- 恢复后 `contextCollapseState` 与内存消息的 `id` 强绑定（`span.messageIds` 是 uuid 列表），
  所以 fork/删除后 uuid 不变是 collapse 能继续生效的前提（fork 保留 uuid 是有意的）。
- 注意 `loadContextCollapseState` 不会因为 snip 而失效：snip 只改文件里被 snip 的消息存在性，
  span 的 `messageIds` 一旦对不上，`projectCollapsedView` 会返回 `null` 投影并整体跳过该 span
  （`context-collapse.ts:163-192`）。

---

## 9. Python 复刻骨架

模块建议 `src/minicode/session.py`，全部使用 `pathlib.Path` + `json`，`ChatMessage` 为 dict。
以下伪代码保持与 TS 完全一致的分支与顺序。

```python
MAX_TITLE_LENGTH = 60
EVENT_TYPES = {"system","user","assistant","thinking","progress","tool_call",
               "tool_result","summary","compact_boundary","snip_boundary",
               "context_collapse","rename"}
ROLE_TO_TYPE = {"system":"system","user":"user","assistant":"assistant",
                "assistant_thinking":"thinking","assistant_progress":"progress",
                "assistant_tool_call":"tool_call","tool_result":"tool_result",
                "context_summary":"summary","snip_boundary":"snip_boundary"}

def project_dir_name(cwd): return re.sub(r"^-+", "", re.sub(r"[/\\:]+", "-", cwd))
def project_dir(cwd):      return MINI_CODE_PROJECTS_DIR / project_dir_name(cwd)
def session_file_path(cwd, session_id): return project_dir(cwd) / f"{session_id}.jsonl"

def _now_iso():            # 等价 new Date().toISOString()
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def role_to_type(role):    return ROLE_TO_TYPE.get(role, "user")

def ensure_message_id(message):
    if message.get("id"): return message["id"]
    message["id"] = str(uuid.uuid4()); return message["id"]

def wrap_event(message, session_id, cwd, parent_uuid):        # -> str
    uid = ensure_message_id(message)
    event = {"type": role_to_type(message["role"]), "message": message, "uuid": uid,
             "timestamp": _now_iso(), "sessionId": session_id, "cwd": cwd,
             "parentUuid": parent_uuid}
    if message["role"] == "snip_boundary":
        event["snipMetadata"] = {"type":"snip_boundary",
            "removedMessageIds": message["removedMessageIds"],
            "removedCount": message["removedCount"], "tokensFreed": message["tokensFreed"],
            "timestamp": event["timestamp"], "createdAt": event["timestamp"]}
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))

def parse_event(line):
    try: return json.loads(line)
    except Exception: return None

def unwrap_message(event):
    if isinstance(event, dict) and event.get("message") is not None:
        return {**event["message"], "id": event["uuid"]}
    return None

def _lines(path):
    try: content = path.read_text(encoding="utf-8")
    except Exception: return None
    return [l for l in content.strip().split("\n") if l]     # JS trim 语义，见 §10
```

`save_session`（逐行对照 `session.ts:225-251`）：

```python
async def save_session(cwd, session_id, messages, already_saved_count=0):
    d = project_dir(cwd); path = session_file_path(cwd, session_id)
    d.mkdir(parents=True, exist_ok=True)
    existing_ids = read_existing_event_uuids(path)
    to_save = []
    for index, m in enumerate(messages[1:]):
        mid = m.get("id")
        if mid and mid in existing_ids: continue
        if mid and mid not in existing_ids: to_save.append(m); continue
        if index >= already_saved_count: to_save.append(m)
    if not to_save: return
    parent_uuid = read_last_event_uuid(path)
    lines = []
    for m in to_save:
        line = wrap_event(m, session_id, cwd, parent_uuid)
        parent_uuid = json.loads(line)["uuid"]
        lines.append(line)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
```

`read_existing_event_uuids` / `read_last_event_uuid`：

```python
def read_existing_event_uuids(path):
    ls = _lines(path)
    if ls is None: return set()
    out = set()
    for line in ls:
        e = parse_event(line)
        if isinstance(e, dict) and e.get("uuid"): out.add(e["uuid"])
    return out

def read_last_event_uuid(path):
    ls = _lines(path)
    if not ls: return None
    e = parse_event(ls[-1])
    return e.get("uuid") if isinstance(e, dict) else None
```

`reconstruct_snipped_events` 必须逐字翻译 §5.2（含 `inserted` 集合与“先无条件跳过所有 snip”）。
`load_session` / `load_context_collapse_state` / `load_transcript` 按 §5.1、§5.3、§6.13 直译；
`list_sessions` 的 `messageCount = len(lines)`；`fork_session` 的命名按 §6.10。
写入侧统一用 “打开 `a` 模式、`"\n".join(lines) + "\n"`”，不要加额外分隔符，也不要 flush/fsync（对齐 TS 行为）。

必须保持的行为清单（否则行为会漂移）：

1. `messages[1:]` 切片（丢下标 0，不判 role）。
2. `to_save` 用 `filter` 语义（不是 break）。
3. 文件内容用 `trim().split("\n")` 分行（末尾换行不会产生空行；`filter(Boolean)` 去空）。
4. uuid 去重优先于位置游标；无 id 消息只按位置。
5. `wrap_event` 原地写入 `message["id"]`。
6. 恢复时“最后一个 compact_boundary 之后 + snip 回放”两步，顺序不可交换。
7. `load_transcript` 不做边界切分、用共享 dict 回填 tool_result。

---

## 10. Gotchas（需要决定“是否复刻”的怪癖与不一致）

按“是否建议复刻”标注：**[必复刻]** 影响恢复正确性；**[建议复刻]** 影响输出一致性；**[可省]** 仅内部噪声。

1. **[必复刻] `subtype`、`parentUuid`、`logicalParentUuid`、`sessionId`、`cwd` 全部只写不读。**
   恢复顺序只由行序 + snip 回放决定（`session.ts:268,275,303,309,334,340` 写；全库无读取点）。
   `logicalParentUuid` 的实际含义仅是“写 boundary 前文件最后一行的 uuid”。
2. **[必复刻] 项目目录名是有损变换，且不是会话 id。**
   `projectDirName` 把 `[/\\:]+` 压成单个 `-` 并吃掉前导 `-`（`session.ts:49-51`），
   于是 `D:\a-b` 与 `D:\a\b` 冲突，`d:\x` 与 `D:\x` 是两个不同目录，`\\server\share` 丢掉 UNC 前缀，
   相对 cwd 会生成 `./x` 这类带点目录名。`listAllProjects` 返回的 `dir` 就是这个名字，
   但 UI 让用户 `cd` 到它（`tty-app.ts:2116`）——**不可用**。
3. **[必复刻] `messageCount` 是 JSONL 行数，不是消息数**（`session.ts:493`）。
   含 boundary / collapse / rename / retained 重复行，与 UI 上“N messages”的语义不符。
4. **[必复刻] compact 的 retained 消息被**再次**写入，uuid 与边界前完全相同**（`session.ts:359-364`）。
   后果：① 文件里存在重复 uuid；② `loadTranscript` 不做边界切分，于是 **compact 之后的历史在恢复的
   transcript 里重复显示一遍**；③ `listSessions` 的 `messageCount` 虚高。
   `loadSession` 因为只读边界之后的行，模型上下文不重复。
5. **[必复刻] compact 摘要写成 `type:'user'` 的普通消息**（`session.ts:344-352`），
   丢失 `context_summary` 角色、`compressedCount`、`timestamp` 与 `id`。
   后果：恢复后模型看到的是“用户说了一段摘要”；`loadTranscript` 也把它渲染成 user 条目，
   并且它可能成为 `extractTitleFromEvents` 的标题来源。
   而内存里的 `context_summary` 对象**永远不会被 `saveSession` 落盘**（它的下标 0 < 游标，且没有 id），
   只有在某次 `alreadySavedCount = 0`（snip/new）之后才可能被补写成 `type:'summary'`。
6. **[必复刻] “摘要 → 边界”的渲染顺序陷阱**：`loadTranscript` 先渲染
   `[Context compacted: A → B tokens]`（assistant 条目），紧接着渲染摘要的 user 条目（`session.ts:703-708, 667-671`）。
   降级渲染路径（§6.14）给出的 snip 文案与 `loadTranscript` 的**完全不同**（一个无方括号、四舍五入、带句点）。
   同一个会话在“有 transcript 文件可读”和“没有”时界面文本不一致。
7. **[必复刻] snip 回放的两条丢数据规则**（`session.ts:124-167`）：
   ① 一旦文件里存在**任意**非空 `removedMessageIds` 的 snip，**所有** `snip_boundary` 事件先被删除，
   只有能被重新定位的才回来；② 被移除的消息 uuid 一条都匹配不上时，边界事件永久消失。
   典型触发：fork 之后（被移除的消息没被复制到新文件）、snip 的区间落在 compact boundary 之前。
8. **[必复刻] `removedMessageIds` 可能包含合成 id。**
   `messageId(m, i) = m.id ?? f"message-{i}"`（`snipCompact.ts:84-86`）；这些合成 id 与磁盘 uuid 永不相等，
   如果被 snip 的区间里全是无 id 消息，回放会直接丢边界。
9. **[必复刻] `clearSession` 删除当前会话后会被下一次保存“复活”。**
   `clearSession` 只 unlink（`session.ts:441-460`）；内存 `args.messages` 未清空，
   之后任意 `saveSession` 因 `existingIds` 为空而把**整个历史**重新追加，文件重建、父链重排
   （uuid 保留，因为 `message.id` 已在内存里）。
10. **[必复刻] `alreadySavedCount = 0`（snip / new）会把此前被位置跳过的无 id 消息补写进文件**，
    包括内存中残留的 `context_summary`；文件里于是出现位置突兀的 `type:'summary'` 行。
11. **[必复刻] 无 id 消息的“跳着写”**：`filter` 不截断，可能出现“下标 2 跳过、下标 3 写入”，
    父链只按实际写出的顺序串（`session.ts:231-249`）。
12. **[建议复刻] `readExistingEventUuids` 每次保存都读整份文件**（O(n) 每回合，`session.ts:203-217`），
    并且把**所有**异常（含权限错误）当作“文件不存在”，从而在不可读时把全部消息重写一遍（产生重复 uuid 行）。
13. **[建议复刻] 无 `\n` 分隔保护**：追加时直接 `lines.join('\n') + '\n'`（`session.ts:250`），
    若历史文件末尾缺少换行，新行会拼接到旧行尾部，制造无法解析的一行（解析失败即被静默跳过）。
14. **[建议复刻] 工具结果目录名是进程级随机 UUID，与会话 id 无关**（`tool-result-storage.ts:46`）。
    重启后旧目录无法被定位；它也是 `MINI_CODE_HOME` 下除 `projects/` 之外唯一按会话划分的目录。
15. **[建议复刻] `sessionFilePath` 不做净化**：`sessionId` 里带 `..\` 或 `/` 会读写项目目录之外
    （`session.ts:57-59`）；8 位十六进制前缀的碰撞会导致两个会话共用同一文件。
16. **[建议复刻] `--fork` 的编号扫描含嵌套名**：`baseTitle='x'` 时 `x_fork1_fork2` 也会被 `startsWith('x_fork')`
    匹配，`parseInt('1_fork2') = 1`；对 fork 再 fork 得到 `x_fork1_fork1`（`session.ts:539-552`）。
    `parseInt` 的“容忍尾随垃圾”行为需用 `re.match(r'\d+', ...)` 复刻。
17. **[建议复刻] `/resume` 丢弃刷新后的 system prompt**：先算 `systemContent`、再 `refreshSystemPrompt()`，
    随后把 `messages[0]` 覆盖回**旧**内容（`tty-app.ts:984-989`）——刷新是白做的。
18. **[建议复刻] `loadContextCollapseState` 不恢复 `enabled` / `consecutiveFailures`**
    （`session.ts:423`），也不恢复 `'staged'` span；失败计数只活在内存。
19. **[可省] JSON `trim` 语义差异**：JS 的 `String.trim()` 会去掉 BOM（`\uFEFF`）与 NBSP（`\u00A0`），
    Python `str.strip()` 不会。若要用 Python 完全对齐，需显式指定 strip 字符集为
    `" \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"`。
    另外 JSONL 若是 CRLF，行尾的 `\r` 会被 `json.loads` 当作空白容忍（与 `JSON.parse` 一致）。
20. **[可省] `parseEvent` 不校验结构**：`JSON.parse("123")` 返回数字也会被当成事件（各消费点访问
    `.type`/`.message` 得到 undefined 而自然跳过）。Python 中建议对非 dict 直接跳过（等价行为）。
21. **[可省] 文件 `id` 与 `event.uuid` 的覆盖关系**：`unwrapMessage` 总是用 `event.uuid` 覆盖 `message.id`
    （`session.ts:114-122`），所以磁盘上 `message.id` 与 `uuid` 不一致时，以 `uuid` 为准。

---

## 附：写入口清单（file:line）

| 位置 | 调用 |
| --- | --- |
| `tty-app.ts:1058` | `/new` `/resume` `/fork` 之前补一次 `saveSession`，随后 `alreadySavedCount = messages.length - 1`（`:1059`） |
| `tty-app.ts:1149` | 手动 `/snip` → `appendSnipBoundary`；`:1152` 游标置 0 |
| `tty-app.ts:1193` | 手动 `/compact` → `appendCompactBoundary('manual')`；`:1204` 游标置 `messages.length - 1` |
| `tty-app.ts:1468` | 回合内 auto-compact → `appendCompactBoundary('auto')`；`:1477` 游标更新 |
| `tty-app.ts:1497` | 回合内 auto-snip → `appendSnipBoundary`；`:1499` 游标置 0 |
| `tty-app.ts:915` | `persistContextCollapseResult` → `appendContextCollapseSpan`（手动 `/collapse` 与回合内共用） |
| `tty-app.ts:1659` | 每个回合结束 `saveSession`；`:1660` 游标置 `messages.length - 1` |
| `tty-app.ts:1774` | `recordAnswer`（`ask_user` 回答）→ `saveSession`；`:1775` 游标更新 |
| `tty-app.ts:1250` | `/rename` → `renameSession` |
| `tty-app.ts:1291,1304` | `/resume` → `loadSession` + `loadTranscript` + `loadContextCollapseState` |
| `tty-app.ts:1333` | `/fork` → `forkSession` |
| `tty-app.ts:1825` | 启动（非 resume）→ `cleanupExpiredSessions(cwd, 30d)` |
| `tty-app.ts:2171` | 选择器删除 → `clearSession`；`:2172` 重新 `listSessions` |
| `tty-app.ts:2194` | 选择器 Tab → `listAllProjects` |
| `index.ts:131` | `--fork <id>` → `forkSession`，成功后 `sessionId`/`resumeTarget` 指向新会话 |
| `index.ts:153` | 启动游标 `alreadySavedCount: 0` |

非交互模式（`index.ts` 的 readline 分支，`:159-314`）**完全不落盘**：只有交互式 TTY 路径会写会话文件。
