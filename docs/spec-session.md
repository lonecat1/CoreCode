# CoreCode 会话持久化子系统 — 实现规格（Python 复刻版）

阅读顺序建议：§1 磁盘布局 → §2 事件模式 → §3 追加算法 → §5 恢复算法 → §6 函数规格 → §10 gotchas。
§4/§7/§8 是交叉约束，复刻时不能漏。

---

## 0. 术语

---

## 1. 磁盘布局

### 1.1 根目录与 env 覆盖

Python：`Path(os.environ["CORECODE_HOME"]).resolve() if "CORECODE_HOME" in os.environ else Path.home()/".corecode"`。
注意 Node 的 `path.resolve()` 对相对路径按进程 CWD 解析，Python 的 `Path.resolve()` 等价；
但 `os.homedir()` 在 Windows 上取 `USERPROFILE`，与 `Path.home()` 一致。

### 1.2 项目目录命名规则（精确字符串变换）

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
| `D:\CoreCode\CoreCode` | `D-CoreCode-CoreCode` |
| `C:\Users\bob\proj` | `C-Users-bob-proj` |
| `D:/CoreCode` | `D-CoreCode` |
| `/home/bob/proj` | `home-bob-proj`（前导 `/` 被吃掉） |
| `\\server\share\proj` | `server-share-proj`（前导 `\\` 被吃掉） |
| `D:\CoreCode\` | `D-CoreCode-`（尾部 `-` 保留） |
| `d:\corecode` | `d-corecode`（大小写敏感，与 `D:\CoreCode` 是不同目录） |
| `D:\a-b` 与 `D:\a\b` | 都是 `D-a-b`（**非单射，会串目录**） |
| `D:\My Code\x` | `D-My Code-x`（空格保留） |

### 1.3 会话文件命名

```ts
function sessionFilePath(cwd, sessionId) {
  return path.join(projectDir(cwd), `${sessionId}.jsonl`)
}
```

- 文件名 = `<sessionId>.jsonl`，`sessionId` **不**做任何净化（`--resume ../../x` 会路径穿越，见 gotchas）。
- 一个项目目录下可能有多个会话文件；除此之外持久化层不在项目目录里写任何其它文件。
- 文件是纯追加的 JSONL：**永不重写、永不截断**（除 `clearSession`/`cleanupExpiredSessions` 的 unlink）。

### 1.4 home 目录下 CoreCode 写入的全部文件

### 1.5 settings 合并顺序（读取侧，供上下文参考）

```
mergeSettings(mergeSettings(mergeSettings(claudeSettings, {mcpServers: globalMcp}), {mcpServers: projectMcp}), coreCodeSettings)
```

因此优先级（高 → 低）：`<CORECODE_DIR>/settings.json` > `<cwd>/.mcp.json` > `<CORECODE_DIR>/mcp.json` > `~/.claude/settings.json`。

---

## 2. JSONL 事件模式

### 2.1 事件对象字段

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

`parentUuid` / `logicalParentUuid` / `subtype` / `sessionId` / `cwd` **在读取侧全部未被使用**（见 §10 gotchas）：
恢复顺序完全由文件行序 + §5.2 的 replay 决定，与 parent 链无关。

### 2.5 样例行（键顺序与 TS 的插入顺序一致）

```json
{"type":"user","message":{"role":"user","content":"fix the bug","id":"9f1c...8a"},"uuid":"9f1c...8a","timestamp":"2024-05-05T12:34:56.789Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null}
{"type":"compact_boundary","subtype":"compact_boundary","uuid":"5c2e...10","timestamp":"2024-05-05T12:40:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"9f1c...8a","compactMetadata":{"trigger":"manual","preTokens":120000,"postTokens":8000}}
{"type":"user","message":{"role":"user","content":"<summary text>"},"uuid":"77aa...31","timestamp":"2024-05-05T12:40:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":"5c2e...10"}
{"type":"snip_boundary","subtype":"snip_boundary","message":{"id":"snip-1714900000000-9f1c...8a","role":"snip_boundary","content":"[Snipped earlier conversation segment]\n...","removedMessageIds":["9f1c...8a","..."],"removedCount":12,"tokensFreed":3400,"timestamp":1714900000000},"uuid":"snip-1714900000000-9f1c...8a","timestamp":"2024-05-05T12:45:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"77aa...31","snipMetadata":{"type":"snip_boundary","removedMessageIds":["9f1c...8a","..."],"removedCount":12,"tokensFreed":3400,"timestamp":"2024-05-05T12:45:00.000Z","createdAt":"2024-05-05T12:45:00.000Z"}}
{"type":"context_collapse","subtype":"context_collapse","uuid":"collapse-1714900100000-0-9f1c...8a","timestamp":"2024-05-05T12:48:20.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj","parentUuid":null,"logicalParentUuid":"snip-1714900000000-9f1c...8a","contextCollapseSpan":{"id":"collapse-1714900100000-0-9f1c...8a","startMessageId":"...","endMessageId":"...","messageIds":["..."],"summary":"...","tokensBefore":9000,"tokensAfter":1400,"status":"committed","createdAt":1714900100000,"reason":"manual"}}
{"type":"rename","title":"my session","uuid":"3f0d...99","timestamp":"2024-05-05T12:49:00.000Z","sessionId":"ab12cd34","cwd":"D:\\CoreCode\\proj"}
```

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

   | 情况 | 是否保存 |
   | --- | --- |
   | `message.id` 存在 且 `existingIds.has(id)` | 不保存 |
   | `message.id` 存在 且 不在 `existingIds` | 保存 |
   | `message.id` 缺失/空串/`null` | 仅当 `index >= alreadySavedCount` 时保存 |

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

### 4.2 `appendSnipBoundary`

```ts
appendSnipBoundary(cwd, sessionId, boundaryMessage: Extract<ChatMessage,{role:'snip_boundary'}>)
// session.ts:253-257
```

```json
{"type":"snip_boundary","subtype":"snip_boundary","message":<boundaryMessage>, "uuid":<同上>,
 "timestamp":now,"sessionId":..., "cwd":...,
 "parentUuid":null,"logicalParentUuid":lastUuid,
 "snipMetadata":{"type":"snip_boundary","removedMessageIds":[...],"removedCount":N,
                 "tokensFreed":T,"timestamp":now,"createdAt":now}}
```

### 4.3 `appendContextCollapseSpan`

```ts
appendContextCollapseSpan(cwd, sessionId, span: CollapseSpan)  // session.ts:289-293
```

```json
{"type":"context_collapse","subtype":"context_collapse","uuid":<span.id>,
 "timestamp":now,"sessionId":...,"cwd":...,
 "parentUuid":null,"logicalParentUuid":lastUuid,"contextCollapseSpan":<span 原样>}
```

### 4.4 `rename` 事件

---

## 5. 恢复算法

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

---

## 6. 导出函数规格

所有函数都是 `async`，所有文件系统错误在读取路径上被吞掉（返回 `null`/空/0），
在写入路径上**不**吞（`appendFile`/`mkdir` 失败会 reject 到调用方）。

### 6.1 `saveSession`

### 6.2 `appendSnipBoundary`

### 6.3 `appendContextCollapseSpan`

### 6.4 `appendCompactBoundary`

### 6.5 `loadSession`

### 6.6 `loadContextCollapseState`

### 6.7 `clearSession`

```
try: unlink(sessionFilePath(cwd, sessionId))     # 任何异常忽略
try:
    files = readdir(projectDir(cwd))
    if len(files) == 0: rm(projectDir(cwd), recursive=True, force=True)
except: pass
```

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

### 6.11 `cleanupExpiredSessions`

```ts
export async function cleanupExpiredSessions(cwd, maxAgeMs: number): Promise<number>  // session.ts:557-560
```

### 6.12 `listAllProjects`

```ts
type ProjectMeta = { dir: string; sessionCount: number; latestUpdatedAt: number }  // session.ts:597-601
export async function listAllProjects(): Promise<ProjectMeta[]>                    // session.ts:603
```

### 6.13 `loadTranscript`

```ts
export type PersistedTranscriptEntry =
  | { kind: 'user' | 'assistant' | 'progress'; body: string }
  | { kind: 'tool'; body: string; toolName: string; status: 'running' | 'success' | 'error' }
// session.ts:643-645
export async function loadTranscript(cwd, sessionId): Promise<PersistedTranscriptEntry[] | null>  // session.ts:647-650
```

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

压缩层产出的具体对象：

### 8.2 为什么游标要这样改

- compact 之后：boundary + 摘要 + retained 已经落盘，而内存数组下标含义已变
  （`[system, context_summary(无 id), ...retained, ...之后新增]`），
  置 `messages.length - 1` 使得**摘要这条无 id 消息被跳过**（下标 0 < 游标），
  而之后新增的无 id 消息（下标 ≥ 游标）会被写入。
- snip 之后：内存数组被重建，置 `0` 让所有无 id 消息都有机会写入；
  已写入的（有 id）消息靠 uuid 去重不重复。
- collapse：不改变 `args.messages`（投影只用于 model-visible 视图），所以不需要动游标。

### 8.3 恢复侧的对接

---

## 9. Python 复刻骨架

模块建议 `src/corecode/session.py`，全部使用 `pathlib.Path` + `json`，`ChatMessage` 为 dict。
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
def project_dir(cwd):      return CORECODE_PROJECTS_DIR / project_dir_name(cwd)
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

---

## 附：写入口清单（file:line）

非交互模式（`index.ts` 的 readline 分支，`:159-314`）**完全不落盘**：只有交互式 TTY 路径会写会话文件。
