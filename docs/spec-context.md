# MiniCode 上下文管理子系统 — 实现规范（Python 复刻版）

> 来源仓库（只读）：`D:\CoreCode\MiniCode`。本规范基于以下文件的逐行阅读：`src/compact/constants.ts`、`src/compact/microcompact.ts`、`src/compact/snipCompact.ts`、`src/compact/context-collapse.ts`、`src/compact/auto-compact.ts`、`src/compact/compact.ts`、`src/compact/prompt.ts`、`src/utils/token-estimator.ts`、`src/utils/model-context.ts`、`src/utils/context.ts`、`src/utils/tool-result-storage.ts`，并交叉验证调用点 `src/agent-loop.ts`。文中所有规则都给出 `file:line` 引用。
>
> 本文只描述语义，不修改任何被描述文件。所有常量、模板字符串、排序规则均按源码原样给出；JS 与 Python 的数值/字符串差异集中列在 §9「Gotchas」。

---

## 0. 总览

### 0.1 消息模型（`ChatMessage` 联合类型，`src/types.ts:23-55`）

Python 侧建议用一个 `dict`（或 dataclass + `role` 判别器）表示。字段表：

| role | 字段（除 `role`、可选 `id`） | 备注 |
|---|---|---|
| `system` | `content: str` | 系统提示，可被 plan/runtime 注入原地改写 |
| `user` | `content: str`；可选 `internal: 'goal'\|'loop'\|'continuation'` | `internal` 仅用于标注，token 估算不区分 |
| `assistant_thinking` | `blocks: list[dict]` | **无 `content`**；token 估算走 `blocks` 的 JSON |
| `assistant` | `content: str`；可选 `providerUsage`、`usageStale`、`usageStaleReason` | |
| `assistant_progress` | `content: str`；可选 `providerUsage`、`usageStale`、`usageStaleReason` | |
| `assistant_tool_call` | `toolUseId: str`、`toolName: str`、`input: Any`；可选 `providerUsage`、`usageStale`、`usageStaleReason` | |
| `tool_result` | `toolUseId: str`、`toolName: str`、`content: str`、`isError: bool` | 注意 `isError` 是必填布尔 |
| `context_summary` | `content: str`、`compressedCount: int`、`timestamp: int` | 由 compact / collapse 插入 |
| `snip_boundary` | `content: str`、`removedMessageIds: list[str]`、`removedCount: int`、`tokensFreed: int`、`timestamp: int` | 由 snip 插入 |

公共可选字段：`id: str`（`src/types.ts:19-21`）。**大量逻辑依赖 `id`，缺失时用位置伪 id `message-{index}`**（`snipCompact.ts:84-86`、`context-collapse.ts:117-119`）。`ProviderUsage` 形状：`{inputTokens, outputTokens, totalTokens, source}`（`src/types.ts:1-6`）。

### 0.2 四个机制的定位对比

| 机制 | 触发阈值（对 `effectiveInput` 的利用率） | 调用模型 | 是否改写真实 transcript | 跨调用状态 | 运行频率 |
|---|---|---|---|---|---|
| `microcompact` | `>= 0.50`（`THRESHOLDS.MICROCOMPACT_UTILIZATION`） | 否 | **是**（就地清空旧 tool_result 内容） | 无 | 每一步 |
| `snipCompact` | `>= 0.70`（`SNIP_COMPACT_THRESHOLD`） | 否 | **是**（删除中间区段 + 插入 `snip_boundary`） | 仅 `snippedThisTurn`（每回合局部布尔） | 每一步尝试，每回合最多成功一次 |
| `contextCollapse` | `>= 0.75`（`CONTEXT_COLLAPSE_UTILIZATION`） | 是（逐 span 生成摘要） | **否**，只改模型可见投影 | `ContextCollapseState`（span 列表 + enabled + 失败计数，可持久化到会话文件） | 每一步 |
| `autoCompact`（全量压缩） | `>= 0.85`（`THRESHOLDS.AUTOCOMPACT_UTILIZATION`），且 `step === 0` | 是（生成一段摘要） | **是**（用 `context_summary` 替换前缀） | 模块级单例 `{consecutiveFailures, disabled}`（进程级） | 每回合仅 step 0，且仅 `critical`/`blocked` |
| `tool-result-storage` | 单条 `> 50_000` 字符；批内可见总量 `> 200_000` 字符 | 否 | **是**（把 tool_result 内容换成持久化占位文本） | `ContentReplacementState`（`seenIds` + `replacements`，进程级、每会话一份） | 每次工具执行 / 每个工具批次 |

**数据流（一个 agent step 内）**：真实 transcript `messages` →（snip 改真实）→（microcompact 改真实）→（collapse 只产出投影 `modelMessages`）→（step 0 时 autocompact 改真实并重置 collapse 状态）→（plan/runtime 注入改写 system）→ 发给模型。

---

## 1. `microcompact`（确定性清空旧工具输出）

文件：`src/compact/microcompact.ts`（全文 50 行）。依赖：`COMPACTABLE_TOOLS`（`src/utils/context.ts:118-124`）、`CLEAR_MARKER`（`src/utils/token-estimator.ts:46`）、`THRESHOLDS`/`RETENTION`（`src/compact/constants.ts:1-25`）。

### 1.1 触发条件与全部数值

| 常量 | 值 | 定义位置 | 作用 |
|---|---|---|---|
| `THRESHOLDS.MICROCOMPACT_UTILIZATION` | `0.50` | `constants.ts:2` | 触发线 |
| `RETENTION.KEEP_RECENT_TOOL_RESULTS` | `3` | `constants.ts:21` | 保留最近 N 条可压缩工具结果 |
| `CLEAR_MARKER` | `'[Output cleared for context space]'` | `token-estimator.ts:46` | 替换文本 |
| `COMPACTABLE_TOOLS` | `{read_file, run_command, search_files, list_files, web_fetch}` | `context.ts:118-124` | 白名单（**精确匹配 `toolName`，大小写敏感，无 trim**） |

触发判定（`microcompact.ts:10-13`）：`stats = computeContextStats(messages, model)`；若 `stats.utilization < 0.50` → **返回原 `messages` 同一引用**（`strictEqual` 语义，测试 `test/microcompact.test.ts:31`）。

### 1.2 精确算法

1. `stats = computeContextStats(messages, model)`（§7，注意其 `utilization` 被 `min(1, …)` 截顶）。
2. 若 `utilization < 0.50` → 原样返回（同引用）。
3. 顺序扫描 `i = 0..len-1`，收集 `toolResultIndices = [i | messages[i].role == 'tool_result' and messages[i].toolName in COMPACTABLE_TOOLS]`（`microcompact.ts:15-21`）。
4. 若 `len(toolResultIndices) <= 3` → 原样返回（**`==3` 也不处理**，`microcompact.ts:23-25`）。
5. `keepFrom = len(toolResultIndices) - 3`；`indicesToClear = set(toolResultIndices[:keepFrom])`（`microcompact.ts:27-28`）。即：**清空"除了最后 3 条之外的所有可压缩 tool_result"**，按消息数组顺序（不是按时间戳）。
6. 遍历构造新数组（`microcompact.ts:30-47`）：
   - 若 `i in indicesToClear` 且 `role == 'tool_result'`：若 `content != CLEAR_MARKER` → 复制 `{...msg, content: CLEAR_MARKER}`（保留 `id/toolUseId/toolName/isError` 等全部其它字段），置 `changed = True`；否则原对象放入。
   - 其它消息原对象放入。
7. 返回 `changed ? 新数组 : messages`（无改动则**返回原引用**，`microcompact.ts:49`）。

### 1.3 保护规则

- 只有 `toolName` 在 `COMPACTABLE_TOOLS` 白名单内的 `tool_result` 会被清空：`edit_file`/`write_file`/`apply_patch` 等**不在**白名单（`context.ts:118-124`；测试 `microcompact.test.ts:64-81`）。
- 不看 `isError`：失败的工具结果同样会被清空。
- 不识别 `snip_boundary` / `context_summary`（它们不是 `tool_result`），不会被清空（测试 `snip-compact.test.ts:440-471`）。
- 已经被清空过的（`content == CLEAR_MARKER`）不再计入 `changed`。

### 1.4 突变范围

- **不改写传入数组**（测试 `microcompact.test.ts:83-108` 断言原数组不变），返回新数组。
- 但调用点 `agent-loop.ts:228` 执行 `messages = microcompact(messages, modelName)`，因此**真实 transcript 被永久替换**（本进程内），且后续 collapse/autocompact/stats 都基于清空后的内容。
- 不插入任何占位消息，**消息条数不变**（`microcompact.test.ts:61`）。
- 无回调通知、不重算 `alreadySavedCount`：被清空的旧消息在 `saveSession` 中位于 `alreadySavedCount` 之前且没有 `id`，因此**不会回写到会话 JSONL**（`src/tty-app.ts:1658-1660`、`src/session.ts:230-239`）——清空只存在于内存 transcript。属于跨文件副作用，复刻时需决策。

### 1.5 状态与失败行为

无状态、无失败计数、无熔断；纯函数。

---

## 2. `snipCompact`（删除中间安全区段 + 插入边界消息）

文件：`src/compact/snipCompact.ts`（全文 499 行）。入口：`snipCompactConversation(params)`（`snipCompact.ts:402-499`）。

### 2.1 触发条件与全部数值

| 常量 | 值 | 定义位置 | 作用 |
|---|---|---|---|
| `SNIP_COMPACT_THRESHOLD` | `0.70` | `constants.ts:7` | 触发线 |
| `SNIP_TARGET_USAGE` | `0.60` | `constants.ts:8` | 目标利用率（算"需要释放多少 token"） |
| `SNIP_MIN_MESSAGES_TO_REMOVE` | `6` | `constants.ts:9` | 最小删除条数 |
| `SNIP_KEEP_RECENT_MESSAGES` | `12` | `constants.ts:10` | 尾部保留窗口 |
| `SNIP_MIN_TOKENS_TO_FREE` | `2000` | `constants.ts:11` | 最小净释放 token |
| `PROTECTED_TOOL_NAMES` | `{edit_file, modify_file, patch_file, write_file, apply_patch}` | `snipCompact.ts:51-57` | 文件编辑工具保护集 |
| `ERROR_MARKERS` | `['error','failed','failure','exception','traceback','permission denied']` | `snipCompact.ts:59-66` | 错误保护子串 |

触发计算（`snipCompact.ts:408-418`）：

```
triggerTokens  = contextStats.totalTokens                 # provider-aware 总量
tokensBefore   = estimateMessagesTokens(params.messages)  # 纯本地估算
effectiveInput = contextStats.effectiveInput > 0 ? contextStats.effectiveInput : params.modelContextWindow
utilization    = effectiveInput > 0 ? triggerTokens / effectiveInput : contextStats.utilization
if utilization < 0.70: 返回 noSnip('below_threshold')
```

**注意**：这里的 `utilization` **不截顶**（与 `ContextStats.utilization` 的 `min(1, …)` 不同）；调用点 `agent-loop.ts:216` 传 `modelContextWindow = latestStats.effectiveInput`。

### 2.2 候选区段 `findCandidateRange`（`snipCompact.ts:237-275`）

设 `n = len(messages)`：

1. 若 `n <= 12 + 6 = 18` → `reason='too_few_messages'`，返回 `{start:0,end:0}`。
2. `keepRecentStart = max(0, n - 12)`。
3. `lastUserIndex` = **从尾部向前**第一个 `role == 'user'` 的下标；不存在为 `-1`。
4. `end = min(keepRecentStart, lastUserIndex if lastUserIndex >= 0 else n)`。
5. 若 `end <= 0` → `reason='no_middle_range'`。
6. `start = 0`；对 `i in [0, end)`：若 `isBoundaryMessage(messages[i])` 则 `start = i + 1`。即 **`start` = 最后一个位于 `end` 之前的边界消息的下一位**。
   - `isBoundaryMessage`（`snipCompact.ts:88-94`）= `role in {'system','context_summary','snip_boundary'}`。
7. 若 `end - start < 6` → `reason='candidate_range_too_small'`（即使 `start` 已有意义）。
8. 否则返回 `{start, end}`（无 `reason`）。

**候选区段是"半开区间 `[start, end)`"，以真实消息下标表示，且 `end` 之外（含最后一条 user 及其之后）全部不可删。**

### 2.3 消息分组 `buildMessageGroups`（`snipCompact.ts:148-195`）

顺序遍历，产出 `MessageGroup{start,end,messages,tokens,protected,reasons}`；`tokens = sum(estimateMessageTokens(m) for m in group.messages)`：

| 当前消息 | 分组规则 | `protected` | `reasons` |
|---|---|---|---|
| `assistant_tool_call` | 若 `messages[i+1]` 存在、`role == 'tool_result'` 且 `toolUseId` 相等 → 组 = `[call, result]`（长度 2）；否则组 = `[call]` | 长度 1 时 `True`，长度 2 时 `False` | 长度 1 → `['unclosed_tool_call']`；长度 2 → `[]` |
| `tool_result`（未被前一条 call 吞掉） | `[result]` | 恒 `True` | `['orphan_tool_result']` |
| 其它（含 `system`/`user`/`assistant`/`assistant_thinking`/`context_summary`/`snip_boundary`） | 单条 | `False` | `[]` |

游标推进：`i += len(group) - 1`（`snipCompact.ts:168`），即闭合的 call/result 对被跳过，不重复成组。

**检测语义**：
- 「未闭合 tool_call」= 该 `assistant_tool_call` 的**紧邻下一条**不是 `toolUseId` 相同的 `tool_result`。
- 「孤儿 tool_result」= 任何没有被其紧邻前一条同 id `assistant_tool_call` 消费掉的 `tool_result`。
- **并行调用批次（连续多条 `assistant_tool_call` 后再跟多条 `tool_result`）在本函数下全部被判为未闭合 + 孤儿，因此整批受保护**（这是与 collapse 的 `toolGroupIsClosed` 的关键差异，见 §9）。

### 2.4 保护规则 `markProtectedGroups`（`snipCompact.ts:204-235`）

第一阶段（`snipCompact.ts:215-224`），对**每个** group：

- 若 `group.start < candidateStart or group.end > candidateEnd` → 加 `'outside_candidate_range'` 并 `continue`（完全落在候选区间**内**的组才有机会被删）。
- 否则若 `any(isBoundaryMessage(m) for m in group.messages)` → 加 `'boundary_message'`。

第二阶段（`snipCompact.ts:226-234`）再次遍历**全部** group（包括刚被标保护的）：

- `groupHasProtectedTool(group)` → `protectNearbyGroups(groups, i, 'near_file_edit')`。
- `groupHasImportantError(group)` → `protectNearbyGroups(groups, i, 'near_important_error')`。

`protectNearbyGroups(groups, i, reason)`（`snipCompact.ts:204-208`）：对 `j in [max(0,i-1), min(len-1,i+1)]` 调 `addProtectedReason(groups[j], reason)` → **保护该组本身、前一组合后一组**（`protected=True` 且去重追加 reason）。

`groupHasProtectedTool`（`snipCompact.ts:129-139`）：组内任一 `assistant_tool_call` 或 `tool_result` 的 `toolName` 满足 `isProtectedToolName`。

`isProtectedToolName(toolName)`（`snipCompact.ts:96-105`）：

```
normalized = toolName.strip().lower()
return (normalized in PROTECTED_TOOL_NAMES
        or 'patch' in normalized or 'write' in normalized
        or 'edit' in normalized or 'modify' in normalized)
```

（子串匹配意味着 `patch_anything`、`overwrite_file`、`credit_report`、`modify_*` 等也会被保护——`edit` 是 `credit` 的子串。）

`groupHasImportantError`（`snipCompact.ts:141-146`）：

```
messageTextLooksImportantError(m) or (m.role == 'tool_result' and toolResultLooksImportantError(m))
```

- `toolResultLooksImportantError`（`snipCompact.ts:107-113`）：`m.isError == True` **或** `m.content.lower()` 包含任一 `ERROR_MARKERS`。
- `messageTextLooksImportantError`（`snipCompact.ts:115-127`）：仅当 `role in {'user','assistant','assistant_progress','context_summary','snip_boundary'}`（**不含 `tool_result`、`system`、`assistant_tool_call`、`assistant_thinking`**）且 `content.lower()` 含任一 marker。

### 2.5 安全段与选择排序（`snipCompact.ts:277-346`）

`findSafeRuns(groups)`（`snipCompact.ts:277-305`）：把**连续未受保护**的 group 合并为 `SafeRun{groups,start,end,messagesCount,tokens}`，其中 `start = first.start`、`end = last.end`、`messagesCount = last.end - first.start`、`tokens = sum(group.tokens)`。受保护组负责 flush。

过滤（`snipCompact.ts:430-435`）：仅保留 `messagesCount >= 6` **且** `tokens >= 2000` 的 run。

排序 `compareRuns`（`snipCompact.ts:307-313`），字典序：

1. `tokens` **降序**
2. `messagesCount` **降序**
3. `start` **升序**（兜底稳定）

取 `bestRun = safeRuns[0]`；为空 → `noSnip('no_safe_interval')`。

### 2.6 删除量选择 `selectDeletionFromRun`（`snipCompact.ts:315-346`）

```
desiredTokensToFree = max(2000, triggerTokens - floor(effectiveInput * 0.60))   # snipCompact.ts:442-446
tokens = 0; endGroupIndex = -1; messagesCount = 0
for i, group in enumerate(run.groups):          # 从前向后累积
    tokens += group.tokens
    messagesCount = group.end - run.start        # 从 run 起点算起的原始消息条数
    endGroupIndex = i
    if tokens >= desiredTokensToFree and messagesCount >= 6: break
end = run.groups[max(0, endGroupIndex)].end
return {start: run.start, end, tokens, messagesCount}
```

若循环未 break（run 全量仍不够），返回整个 run 的 `tokens/messagesCount`。若 `messagesCount < 6` → `noSnip('below_min_messages')`（`snipCompact.ts:448-450`）。

### 2.7 边界消息与净收益校验（`snipCompact.ts:452-498`）

1. `removedMessages = messages[deletion.start:deletion.end]`。
2. `removedMessageIds[j] = removedMessages[j].id ?? f'message-{deletion.start + j}'`（`snipCompact.ts:84-86,453-455`）。
3. `boundaryMessage`（`buildBoundaryMessage`，`snipCompact.ts:374-393`）：

| 字段 | 值 |
|---|---|
| `id` | `f'snip-{timestamp}-{removedMessageIds[0] or "none"}'`，`timestamp = Date.now()`（毫秒） |
| `role` | `'snip_boundary'` |
| `content` | `buildSnipBoundaryContent({removedCount: len(removedMessages), tokensFreed: deletion.tokens})` |
| `removedMessageIds` | 上述 id 数组 |
| `removedCount` | `len(removedMessages)` |
| `tokensFreed` | `deletion.tokens`（此时还是"删除总量"，未扣边界开销） |
| `timestamp` | 同上 `Date.now()` |

`buildSnipBoundaryContent`（`snipCompact.ts:348-363`）精确模板（`\n` 为换行）：

```
[Snipped earlier conversation segment]

A middle portion of the earlier conversation was removed to preserve context space.

Removed range:
- messages: {removedCount}
- approximate tokens freed: {max(0, round(tokensFreed))}

The recent conversation and active task context are preserved.
```

4. `boundaryTokens = estimateMessagesTokens([boundaryMessage])`（`snip_boundary` 比率 3.5）。
5. `estimatedTokensFreed = max(0, deletion.tokens - boundaryTokens)`；若 `< 2000` → `noSnip('below_min_tokens')`。
6. 组装（`snipCompact.ts:468-479`）：

```
messagesAfterSnip = markRetainedUsageStale(
    messages[:deletion.start]
  + [{...boundaryMessage,
      content: buildSnipBoundaryContent({removedCount, tokensFreed: estimatedTokensFreed}),
      tokensFreed: estimatedTokensFreed}]
  + messages[deletion.end:]
)
```

`markRetainedUsageStale`（`snipCompact.ts:395-400`）对**整个数组**逐条调用 `markProviderUsageStale(m, 'conversation was snip-compacted after this provider usage was recorded')`；后者仅对 `role in {assistant, assistant_progress, assistant_tool_call}` 且存在 `providerUsage` 的消息返回**新对象**并置 `usageStale=True, usageStaleReason=<reason>`（`token-estimator.ts:158-175`）。

7. `tokensAfter = tokenCountWithEstimation(messagesAfterSnip).totalTokens`；`tokensFreed = max(0, tokensBefore - tokensAfter)`。
   - 因为刚把所有 provider usage 标记为 stale，`tokenCountWithEstimation` 必然走 `estimate_only` 分支 → `tokensAfter` 实际等于 `estimateMessagesTokens(messagesAfterSnip)`。
8. 若 `tokensAfter >= tokensBefore` → `noSnip('no_token_reduction')`（**放弃全部改动，返回原数组**）。
9. 成功返回（`snipCompact.ts:489-498`）：

| 字段 | 值 |
|---|---|
| `messages` | `messagesAfterSnip`（最终数组，含内容/`tokensFreed` 已修正的边界消息） |
| `didSnip` | `True` |
| `tokensBefore` | 纯估算的原始总量 |
| `tokensAfter` | `tokenCountWithEstimation` 结果（实际为全量估算） |
| `tokensFreed` | `max(0, tokensBefore - tokensAfter)` |
| `removedMessageIds` | 被删消息 id 列表 |
| `boundaryMessage` | `messagesAfterSnip[deletion.start]`（数组内的那一个对象，非草稿） |
| `reason` | `'snipped_safe_middle_interval'` |

`noSnipResult`（`snipCompact.ts:68-82`）返回 `{messages: 原数组, didSnip: False, tokensBefore, tokensAfter: tokensBefore, tokensFreed: 0, removedMessageIds: [], reason}`；**失败时绝不改动 transcript**。

### 2.8 突变范围、状态、回滚

- **改写真实 transcript**：调用点 `agent-loop.ts:218-224` 执行 `messages = snipResult.messages`，并置 `snippedThisTurn = true`。
- 保护"全局"：`system`、既有 `context_summary`、既有 `snip_boundary` 永不删除（边界保护 + `start` 起点推导）。
- 状态：`snippedThisTurn`（`agent-loop.ts:166`）是**每回合局部布尔**；只在 `didSnip == True` 时为真 → 每回合最多成功 snip 一次，但在未成功前**每个 step 都会重试**（`agent-loop.ts:212`）。
- 无失败计数/熔断；任何失败都是 no-op（返回原引用）
- 副作用：snip 成功后**整个数组的 provider usage 全部变 stale** ⇒ 之后 stats 退化为 `estimate_only`，直到模型返回新的 usage。
- 调用点**不重置** `contextCollapseState`（`/snip` 命令路径会重置：`tty-app.ts:1153`）——见 §9 gotcha。

---

## 3. `contextCollapse`（投影式摘要折叠）

文件：`src/compact/context-collapse.ts`（全文 701 行）。入口：`applyContextCollapseIfNeeded(messages, model, adapter, state, options?)`（`context-collapse.ts:599-701`）。

### 3.1 触发条件与全部数值

| 常量 | 值 | 定义位置 | 作用 |
|---|---|---|---|
| `CONTEXT_COLLAPSE_UTILIZATION` | `0.75` | `constants.ts:13` | 触发线 |
| `CONTEXT_COLLAPSE_TARGET_USAGE` | `0.65` | `constants.ts:14` | 多 span 循环的停止目标 |
| `CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES` | `12` | `constants.ts:15` | 尾部保护窗口 |
| `CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE` | `2000` | `constants.ts:16` | 最小净省 token（也是 `desiredTokensToSave` 下限） |
| `CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS` | `2` | `constants.ts:17` | 单次调用最多折叠 span 数 |
| `CONTEXT_COLLAPSE_MAX_FAILURES` | `3` | `constants.ts:18` | 连续失败熔断阈值 |

默认选项合并（`withDefaultOptions`，`context-collapse.ts:97-115`）：`utilizationThreshold=0.75`、`targetUsage=0.65`、`keepRecentMessages=12`、`minTokensToSave=2000`、`currentTokens=undefined`、`effectiveInput=undefined`、`maxSpansPerPass=2`、`maxFailures=3`、`reason='context_pressure'`。agent-loop 调用时不传 options（`agent-loop.ts:234-239`），故全用默认值。

触发（`context-collapse.ts:606-616`）：

```
currentState  = normalize(state)                 # 浅拷贝 spans
if not currentState.enabled: return unchanged(messages, currentState)
currentProjected = projectCollapsedView(messages, currentState)
stats = computeContextStats(currentProjected, model)
if stats.utilization < options.utilizationThreshold: return unchanged(currentProjected, currentState)
```

**注意返回的是 `currentProjected`**：即使本次不折叠，已提交 span 的投影依然生效。

### 3.2 主循环（`context-collapse.ts:618-700`）

```
plannedSpans = []
maxSpans = max(1, floor(options.maxSpansPerPass))            # =2
for pass in range(maxSpans):
    selectionState = {**currentState, spans: currentState.spans + plannedSpans}
    projected = projectCollapsedView(messages, selectionState)
    stats = computeContextStats(projected, model)
    if plannedSpans and stats.utilization <= options.targetUsage: break
    candidate = findCollapseCandidate(messages, selectionState,
                    {**options, currentTokens: stats.totalTokens, effectiveInput: stats.effectiveInput})
    if not candidate: break
    summaryPrompt = buildContextCollapseSummaryPrompt(messagesToCollapseText(candidate.messages))
    summaryRequestMessages = [
        {role: 'system', content: 'You are a precise assistant that summarizes older coding-session context without inventing details.'},
        {role: 'user',   content: summaryPrompt},
    ]
    try:
        response = adapter.next(summaryRequestMessages)
        if response.type != 'assistant' or not response.content.strip():
            return failedCollapseResult(currentProjected, currentState, options)
        summary = parseSummaryFromResponse(response.content)
        if not summary:
            return failedCollapseResult(currentProjected, currentState, options)
        now = Date.now()
        draftSpan = {id: f'collapse-{now}-{pass}-{candidate.startMessageId}',
                     startMessageId, endMessageId, messageIds, summary,
                     tokensBefore: candidate.tokensBefore, tokensAfter: 0,
                     status: 'staged', createdAt: now, reason: options.reason}
        summaryTokens = estimateMessagesTokens([buildCollapsedSummaryMessage(draftSpan)])
        tokensToSave = max(0, candidate.tokensBefore - summaryTokens)
        if tokensToSave < options.minTokensToSave:
            if plannedSpans: break                 # 保留已规划的 span
            return failedCollapseResult(currentProjected, currentState, options)   # 计入失败
        plannedSpans.append({**draftSpan, tokensAfter: summaryTokens})
    except:
        return failedCollapseResult(currentProjected, currentState, options)       # 丢弃全部 plannedSpans
if not plannedSpans: return unchangedCollapseResult(currentProjected, currentState)
return committedCollapseResult(messages, currentState, plannedSpans)
```

关键点：
- **第二个及以后的 pass 只在 `plannedSpans` 非空且利用率仍 `> targetUsage` 时继续**（`context-collapse.ts:629-631`）。
- 候选搜索始终基于**原始 `messages` 索引空间**，用 `selectionState.spans`（含 staged）把已覆盖消息排除；而 stats 基于**投影后**的数组（索引空间不一致，见 §9）。
- 摘要请求固定为 system + user 两条，**不传 max_tokens**（尽管 `LIMITS.SUMMARY_MAX_OUTPUT_TOKENS` 存在但未使用）。

### 3.3 候选 span 选择 `findCollapseCandidate`（`context-collapse.ts:428-489`）

1. `options = withDefaultOptions(rawOptions)`；若 `len(messages) == 0` → `None`。
2. `lastUserIndex` = 从尾向前第一个 `role == 'user'`；无则 `-1`。
3. `keepRecentStart = max(0, len(messages) - options.keepRecentMessages)`（默认 `len-12`）。
4. `protectedStart = min(keepRecentStart, lastUserIndex if lastUserIndex >= 0 else len(messages))`。
5. 若 `protectedStart <= 0` → `None`。
6. `collapsedIds` = **所有 `status in {'committed','staged'}`** 的 span 中 `messageIds` 的并集（`committedCollapsedMessageIds`，`context-collapse.ts:355-364`——名字叫 committed，实际含 staged）。
7. `groups = buildMessageGroups(messages)`（见 3.4）。
8. 顺序切分安全段；每组判保护（`context-collapse.ts:463-479`）：

```
protectedGroup = group.protected
              or group.start < 0                       # 恒 False
              or group.end > protectedStart
              or any(isCollapseBoundary(m) for m in group.messages)
              or any(collapsedIds.has(messageId(m, group.start + offset))
                     for offset, m in enumerate(group.messages))
```

   受保护 → flush；否则并入当前 run。`isCollapseBoundary`（`context-collapse.ts:121-127`）= `role in {'system','context_summary','snip_boundary'}`。
9. 按 run 的**自然顺序**（不是按大小排序）逐个 `buildCandidateFromGroups`，**返回第一个非空候选**（`context-collapse.ts:481-486`）。

`buildCandidateFromGroups`（`context-collapse.ts:380-426`）：

```
desired = desiredTokensToSave(options)
# = max(minTokensToSave, ceil(currentTokens - effectiveInput * targetUsage))
#   仅当 currentTokens 与 effectiveInput 均非 None 且 effectiveInput > 0；
#   否则 = minTokensToSave                                    (context-collapse.ts:366-378)
tokens = 0; endGroupIndex = -1
for i, g in enumerate(run):
    tokens += g.tokens
    estimatedTokensAfter    = max(128, ceil(tokens * 0.15))   # estimateCollapseSummaryTokens
    estimatedTokensToSave   = max(0, tokens - estimatedTokensAfter)
    endGroupIndex = i
    if estimatedTokensToSave >= desired: break
if endGroupIndex < 0: return None
selectedGroups = run[:endGroupIndex+1]
first, last = selectedGroups[0], selectedGroups[-1]
selectedMessages = messages[first.start:last.end]
messageIds = [m.id ?? f'message-{first.start + offset}' for offset, m in enumerate(selectedMessages)]
# 注意：estimatedTokensAfter / estimatedTokensToSave 用循环结束时的 tokens 重算一次
if estimatedTokensToSave < options.minTokensToSave: return None
return {startIndex: first.start, endIndex: last.end,
        startMessageId: messageIds[0], endMessageId: messageIds[-1],
        messageIds, messages: selectedMessages,
        tokensBefore: tokens, estimatedTokensAfter, estimatedTokensToSave}
```

摘要开销估算函数（`context-collapse.ts:129-131`）：`max(128, ceil(tokensBefore * 0.15))`。

### 3.4 分组 `buildMessageGroups`（`context-collapse.ts:252-326`）

与 snip 的分组**不同**：按"API 轮次"聚合，且用闭包检测保护：

| 起始消息 | 组内容 | `protected` |
|---|---|---|
| `assistant_thinking` | 该 thinking + 紧随的所有连续 `assistant_tool_call` + 紧随的所有连续 `tool_result` | `hasToolCall and not toolGroupIsClosed(group)` |
| `assistant_tool_call` | 紧随的所有连续 `assistant_tool_call` + 紧随的所有连续 `tool_result` | `not toolGroupIsClosed(group)` |
| `tool_result` | 单条 | `True` |
| 其它 | 单条 | `False` |

`toolGroupIsClosed(messages)`（`context-collapse.ts:328-353`）：

```
calls   = {m.toolUseId for m in messages if m.role == 'assistant_tool_call'}
results = {m.toolUseId for m in messages if m.role == 'tool_result'}
if not calls and not results: return True
if not calls or not results:  return False
return all(cid in results for cid in calls) and all(rid in calls for rid in results)
```

即：**双向包含检查**（call 集合与 result 集合必须完全相等）；空组视为闭合；只要一侧为空即不闭合。**并行调用批次在此判定下是闭合的**（与 snip 相反）。

### 3.5 投影 `projectCollapsedView`（`context-collapse.ts:201-250`）

```
if not state.enabled or not state.spans: return messages        # 同引用
projections = [p for p in map(projectSpan, state.spans) if p]
projections.sort(key=lambda p: p.start)                        # 升序；同 start 保持 spans 顺序（JS sort 稳定）
if not projections: return messages                            # 同引用
result = []; occupied = set(); cursor = 0
for p in projections:
    if any(i in occupied for i in range(p.start, p.end)): continue   # 跳过重叠 span，occupied 不更新
    while cursor < p.start:
        result.append(markProviderUsageStale(messages[cursor], STALE_REASON)); cursor += 1
    result.append(p.message)                     # context_summary 投影消息
    occupied.update(range(p.start, p.end))
    cursor = p.end
while cursor < len(messages):
    result.append(markProviderUsageStale(messages[cursor], STALE_REASON)); cursor += 1
return result
```

`STALE_REASON = 'conversation was context-collapsed in the model-visible projection after this provider usage was recorded'`（`context-collapse.ts:78-79`）。

**副作用（极重要）**：投影输出的每一条非摘要消息都过了一遍 `markProviderUsageStale` ⇒ 只要存在任何已提交 span，**模型可见投影中所有 provider usage 都是 stale**，`tokenCountWithEstimation` 恒为 `estimate_only`。

`projectSpan(messages, span)`（`context-collapse.ts:155-199`）——span 合法性校验（任一不满足返回 `None`，该 span 静默失效、原文照常输出）：

1. `span.status != 'committed'` 或 `len(span.messageIds) == 0` → `None`。
2. 建立 `indexById: {messageId(m, i): i}`（`messageId = m.id ?? f'message-{i}'`）。
3. `indices = [indexById[id] for id in span.messageIds]`；任一缺失 → `None`。
4. `indices` 必须**严格连续**（`indices[i] == indices[i-1] + 1`），否则 `None`。
5. `start = indices[0]`、`end = indices[-1] + 1`；必须 `messageId(messages[start], start) == span.startMessageId` 且 `messageId(messages[end-1], end-1) == span.endMessageId`，否则 `None`。
6. 返回 `{start, end, message: buildCollapsedSummaryMessage(span)}`。

`buildCollapsedSummaryMessage`（`context-collapse.ts:143-153`）：

| 字段 | 值 |
|---|---|
| `id` | `f'collapse-summary-{span.id}'` |
| `role` | `'context_summary'` |
| `content` | `buildCollapsedSummaryContent(span)` |
| `compressedCount` | `len(span.messageIds)` |
| `timestamp` | `span.createdAt` |

`buildCollapsedSummaryContent`（`context-collapse.ts:133-141`）精确模板：

```
[Collapsed context summary]
This summary replaces messages {span.startMessageId} through {span.endMessageId} in the model-visible context only.
The original transcript is preserved in the session/UI.

{span.summary}
```

### 3.6 摘要请求文本

`messagesToCollapseText`（`context-collapse.ts:491-515`）逐条前缀（`\n\n` 连接）：

| role | 模板 |
|---|---|
| `user` | `[User]: {content}` |
| `assistant` / `assistant_progress` | `[Assistant]: {content}` |
| `assistant_thinking` | `[Assistant Thinking]: preserved provider reasoning block`（**内容被丢弃**） |
| `assistant_tool_call` | `[Tool Call: {toolName} {toolUseId}]: {json(input)}` |
| `tool_result` | `[Tool Result: {toolName} {toolUseId}{' ERROR' if isError}]: {content}`（**不截断**） |
| `context_summary` | `[Context Summary]: {content}` |
| `snip_boundary` | `[Snip Boundary]: {content}` |
| `system` | `[System]: protected system message` |

`buildContextCollapseSummaryPrompt(conversationText)`（`context-collapse.ts:517-542`）精确文本：

```
You are creating a local context-collapse summary for an AI coding session.
The summary will replace only this older message span in the model-visible context.
The original transcript remains preserved outside the model-visible projection.

Produce the final summary in <summary> tags.

Preserve:
- User intent and active goals
- Completed tasks and current state
- Important decisions and constraints
- Tool calls and tool results that still matter
- File reads/writes and code changes, with paths, function names, config names, and commands
- Errors, failures, warnings, and exact messages when relevant
- TODOs, uncertainty, follow-up constraints, and anything still relevant later

Rules:
- Do not invent facts or outcomes
- Do not omit critical paths, function names, configuration keys, file paths, or error text
- Keep it concise, but prefer specificity over vague compression
- This is not a full conversation compact; summarize only the provided span

Messages to summarize:

{conversationText}
```

摘要解析复用 `parseSummaryFromResponse`（§4.4）。

### 3.7 三种收尾（`context-collapse.ts:544-597`）

| 结果构造器 | 触发时机 | `messages` | `state` | `collapsed` | `span(s)` |
|---|---|---|---|---|---|
| `unchangedCollapseResult` | `!enabled`；利用率低于阈值；无候选；plans 为空 | 传入值（通常是 `currentProjected`） | **原对象引用**（未归一化） | `False` | `spans: []` |
| `failedCollapseResult` | 适配器异常；响应非 assistant/空；摘要解析失败；单 pass 且净省不足 | `currentProjected` | 新对象：`consecutiveFailures = state.consecutiveFailures + 1`；`enabled = False if consecutiveFailures >= maxFailures else state.enabled`；`spans = [...state.spans]` | `False` | `spans: []` |
| `committedCollapseResult` | 至少一个 planned span | `projectCollapsedView(messages, nextState)` | 新对象：`spans = [...state.spans, ...spans(status='committed')]`、`consecutiveFailures = 0` | `True` | `span = committed[0]`，`spans = 全部新提交 span` |

### 3.8 突变范围、状态、回滚

- **只改模型可见投影**：调用点 `agent-loop.ts:242` `modelMessages = collapseResult.messages`，真实 `messages` 不动（测试 `test/context-collapse.test.ts:207-221` 断言原数组 `deepEqual` 不变）。
- 投影里插入的占位是 `context_summary`（形状见 3.5），**替代**被折叠区段（消息条数减少）。
- 状态 `ContextCollapseState{spans, enabled, consecutiveFailures}`：
  - 进程内由 `agent-loop.ts:169-179` 持有；若调用方传了 `args.contextCollapseState`，`replaceContextCollapseState` **就地改写该对象的三个字段**（`spans` 浅拷贝替换）。
  - `tty-app` 在会话级持有该对象，并在 `/resume` 时从会话 JSONL 通过 `loadContextCollapseState` 恢复（`tty-app.ts:1027-1029`、`src/session.ts:406-439`：只加载 `compact_boundary` 事件之后的 `context_collapse` 事件中的 `committed` span）。提交时逐 span 持久化为 `context_collapse` 事件（`tty-app.ts:904-922`、`session.ts:289-314`）。
  - 状态在以下时机被重置为全新状态：autoCompact 成功后（`agent-loop.ts:263`）、`/snip`（`tty-app.ts:1153`）、`/compact`（`tty-app.ts:1205`）、`/new` 与 `/fork`（`tty-app.ts:1320,1344`）。
- 回滚/熔断：`consecutiveFailures` 连续累加，`>= maxFailures`（默认 3）→ `enabled = False`，此后 `applyContextCollapseIfNeeded` 立即返回 unchanged（测试 `test/context-collapse.test.ts:508-542`）；任一次成功提交把计数清零；中途异常会丢弃本 pass 已规划的全部 span（`test/context-collapse.test.ts:470-506`）。
- 注意：`failedCollapseResult` 返回的 `messages` 是 `currentProjected`（不含本 pass 的 plannedSpans），且 `state.spans` 也不含它们。

---

## 4. `autoCompact` + `compactConversation`（全量 LLM 压缩）

文件：`src/compact/auto-compact.ts`（82 行）、`src/compact/compact.ts`（200 行）、`src/compact/prompt.ts`（38 行）、`src/compact/manual-compact.ts`（15 行）。

### 4.1 门控与常量

| 常量 | 值 | 定义位置 |
|---|---|---|
| `THRESHOLDS.AUTOCOMPACT_UTILIZATION` | `0.85` | `constants.ts:3` |
| `LIMITS.MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT` | `20000` | `constants.ts:31` |
| `LIMITS.MAX_AUTOCOMPACT_FAILURES` | `3` | `constants.ts:28` |
| `RETENTION.MIN_KEEP_MESSAGES` | `6` | `constants.ts:22` |
| `RETENTION.MAX_KEEP_TOKENS` | `40000` | `constants.ts:24` |
| `RETENTION.MIN_KEEP_TOKENS` | `10000`（**未被任何代码使用**） | `constants.ts:23` |

`autoCompact(messages, model, modelAdapter)`（`auto-compact.ts:45-82`）顺序：

1. `if state.disabled: return None`。
2. `window = getModelContextWindow(model)`；`if window.effectiveInput < 20000: return None`。
3. `if not shouldAutoCompact(messages, model): return None`——即 `computeContextStats(messages, model).utilization >= 0.85`（`auto-compact.ts:33-43`）。
4. `try: result = await compactConversation(messages, modelAdapter)`
   - 非空 → `state.consecutiveFailures = 0`，返回 result。
   - 空（`None`）→ `consecutiveFailures += 1`；`>= 3` 时 `state.disabled = True`；返回 `None`。
   - 抛异常 → 同样的计数与禁用逻辑；返回 `None`。

调试输出（`auto-compact.ts:18-22,36-41`）：仅当 `MINI_CODE_DEBUG_AUTOCOMPACT == '1'` 时向 **stderr** 打印：

```
[auto-compact] source={accounting.source} total={accounting.totalTokens} provider={accounting.providerUsageTokens} estimate={accounting.estimatedTokens} utilization={utilization:.3f} threshold=0.85 should={true|false}
```

### 4.2 状态与持久性

- 模块级单例 `state = {consecutiveFailures: 0, disabled: false}`（`auto-compact.ts:13-16`）——**进程级、跨会话共享、不持久化**。
- `resetAutoCompactState()`（`auto-compact.ts:24-27`）在**手动 `/compact` 成功**后调用（`manual-compact.ts:10-13`）。
- `getAutoCompactState()` 返回浅拷贝（`auto-compact.ts:29-31`）。
- agent-loop 的门控不是直接调 `shouldAutoCompact`，而是看 `warningLevel`（`agent-loop.ts:257`）：`critical` 或 `blocked`，即 `utilization >= 0.85`（`blocked` 是 `>= 0.95` 的子集）。

### 4.3 `compactConversation`（`compact.ts:124-198`）

1. `if len(messages) <= 2: return None`。
2. `tokensBefore = tokenCountWithEstimation(messages).totalTokens`（provider-aware）。
3. `systemMessages = [m for m in messages if m.role == 'system']`；`nonSystemMessages = 其余`；`if len(nonSystemMessages) <= 6: return None`。
4. `boundary = findRetentionBoundary(messages)`（见 4.3.1）。
5. `messagesToCompress = messages[1:boundary]`（**硬编码从下标 1 开始**）。
6. `messagesToKeep = [markProviderUsageStale(m, 'conversation was compacted after this provider usage was recorded') for m in messages[boundary:]]`。
7. `if len(messagesToCompress) == 0: return None`。
8. `conversationText = messagesToText(messagesToCompress)`（4.3.2）；`summaryPrompt = buildCompactSummaryPrompt(conversationText)`（4.4）。
9. 请求 = `[{role:'system', content:'You are a helpful assistant that summarizes conversations concisely.'}, {role:'user', content:summaryPrompt}]`。
10. `response = adapter.next(...)`；`if response.type != 'assistant' or not response.content.strip(): return None`。
11. `summaryContent = parseSummaryFromResponse(response.content)`；`if not summaryContent: return None`。
12. `summaryMessage = {role:'context_summary', content:summaryContent, compressedCount: len(messagesToCompress), timestamp: Date.now()}`（**无 `id`**）。
13. `newMessages = [*systemMessages, summaryMessage, *messagesToKeep]`。
14. `tokensAfter = tokenCountWithEstimation(newMessages).totalTokens`。
15. 返回 `{messages: newMessages, summary: summaryMessage, removedCount: len(messagesToCompress), tokensBefore, tokensAfter}`。
16. 任何异常 → `None`（`compact.ts:195-197`）。

#### 4.3.1 `findRetentionBoundary`（`compact.ts:58-86`）

```
tokenSum = 0
boundary = len(messages)
for i in range(len(messages) - 1, 0, -1):          # i 从末尾到 1（含），永不取 0
    t = estimateMessagesTokens([messages[i]])
    if tokenSum + t > 40000: break                 # MAX_KEEP_TOKENS
    tokenSum += t
    boundary = i
minBoundary = max(1, len(messages) - 6)            # MIN_KEEP_MESSAGES
boundary = min(boundary, minBoundary)
if boundary <= 1 and len(messages) > 7:
    boundary = max(1, len(messages) - 6)
return alignBoundaryToApiRound(messages, boundary)
```

注意三件事：(a) 下标 0 永远不参与累积；(b) `boundary = min(boundary, minBoundary)` 使**保留窗口不小于 6 条**（`messages.length - boundary >= 6`）；(c) 尾部 token 累积一旦超过 40000 就停，所以保留窗口通常 ≤ 40K 估算 token。

`alignBoundaryToApiRound(messages, boundary)`（`compact.ts:46-56`）：用 `groupMessagesByApiRound` 顺序切分，累计 `start`；若 `start < boundary < end`（严格不等）→ 返回 `start`（把边界左移到该轮次开头），否则返回 `boundary`。若返回 `0` → `messagesToCompress` 为空 → `compactConversation` 返回 `None`。

`groupMessagesByApiRound`（`compact.ts:11-44`）：

```
i = 0
while i < len(messages):
    group = []; cursor = i
    if messages[cursor].role == 'assistant_thinking': group.append(messages[cursor]); cursor += 1
    while cursor < len and messages[cursor].role == 'assistant_tool_call': group.append(...); cursor += 1
    while cursor < len and messages[cursor].role == 'tool_result':       group.append(...); cursor += 1
    if any(m.role in ('assistant_tool_call','tool_result') for m in group):
        groups.append(group); i = cursor; continue
    groups.append([messages[i]]); i += 1
```

即：`assistant_thinking` 只在与后续 tool_call/tool_result 同组时才成组，否则退化为单条；孤儿 `tool_result` 会被合并成"连续 result"组。

#### 4.3.2 `messagesToText`（`compact.ts:88-122`）

`\n\n` 连接；逐条模板：

| role | 模板 |
|---|---|
| `user` | `[User]: {content}` |
| `assistant` / `assistant_progress` | `[Assistant]: {content}` |
| `assistant_thinking` | `[Assistant Thinking]: preserved provider reasoning block` |
| `assistant_tool_call` | `[Tool Call: {toolName}]: {json(input)}`（**不含 toolUseId**） |
| `tool_result` | `[Tool Result: {toolName}{' ERROR' if isError}]: {content[:500] + '... (truncated)' if len(content) > 500 else content}` |
| `context_summary` | `[Previous Summary]: {content}` |
| `snip_boundary` | `[Snipped Context Boundary]: {content}` |
| 其它（含 `system`） | **直接跳过**（`default: break`） |

（`system` 消息若出现在 `messages[1:boundary]` 中会被跳过，但它们通过 `systemMessages` 全部重新出现在结果头部。）

### 4.4 提示词与摘要解析（`prompt.ts`）

`buildCompactSummaryPrompt(conversationText)`（`prompt.ts:1-21`）精确文本：

```
You are summarizing a conversation for context compression.
Produce a structured summary in <summary> tags.

Sections:
1. Primary Request — What the user asked for
2. Key Decisions — Important choices made
3. Files Modified — Which files were changed and why
4. Errors Encountered — Problems hit and how they were resolved
5. Current State — Where things stand right now
6. Pending Tasks — What still needs to be done

Rules:
- Be concise but preserve actionable details (file paths, command outputs, error messages)
- Use <analysis> tags as scratchpad, then <summary> tags for final output
- The summary will replace all messages before the recent tail

Conversation to summarize:

{conversationText}
```

`parseSummaryFromResponse(response)`（`prompt.ts:23-38`），精确顺序：

1. `m = regex_search(r'<summary>([\s\S]*?)</summary>', response)`；若匹配且捕获组**真值（非空字符串）** → 返回 `group.strip()`。
2. 否则 `a = regex_search(r'<analysis>([\s\S]*?)</analysis>', response)`；若**没有** `<analysis>` → `t = response.strip()`；若 `len(t) > 0` → 返回 `t`；否则 `None`。
3. 若存在 `<analysis>` 但没有 `<summary>` → 返回 `None`。

（`<summary>   </summary>`：捕获组 `'   '` 为真 → 返回 `''`，调用方的 `if not summary` 会把它当失败。）

### 4.5 突变范围与失败行为

- **改写真实 transcript**：`agent-loop.ts:261-263` `messages = result.messages; modelMessages = messages; replaceContextCollapseState(createContextCollapseState())`——即 **compact 成功后 collapse 状态被整体丢弃**（因为索引/消息全变了）。
- 失败不修改任何东西；连续 3 次失败后模块级 `disabled=True`，本进程内不再尝试 autocompact（直到 `resetAutoCompactState`）。

---

## 5. `token-estimator`

文件：`src/utils/token-estimator.ts`（208 行）。

### 5.1 `CHARS_PER_TOKEN` 表（`token-estimator.ts:34-44`）

| role | chars/token |
|---|---|
| `system` | 3.5 |
| `user` | 3.0 |
| `assistant_thinking` | 3.0 |
| `assistant` | 3.5 |
| `assistant_progress` | 3.5 |
| `assistant_tool_call` | 2.5 |
| `tool_result` | 2.0 |
| `context_summary` | 3.5 |
| `snip_boundary` | 3.5 |
| 其它（fallback `?? 3.0`） | 3.0 |

`estimateMessageTokens(m) = ceil(contentLength(m) / CHARS_PER_TOKEN[m.role])`（`token-estimator.ts:78-82`）。

`contentLength`（`token-estimator.ts:48-76`）：

| role | 长度定义 |
|---|---|
| `system`/`user`/`assistant`/`assistant_progress` | `len(content)`（**UTF-16 码元数**） |
| `assistant_thinking` | `len(JSON.stringify(blocks))`；异常 → 0 |
| `assistant_tool_call` | `len(JSON.stringify(input))`；异常 → 0 |
| `tool_result`/`context_summary`/`snip_boundary` | `len(content)` |
| 其它 | 0 |

`estimateMessagesTokens(messages) = sum(estimateMessageTokens(m))`（`token-estimator.ts:84-90`）。

### 5.2 provider usage 与本地估算的合并（`tokenCountWithEstimation`，`token-estimator.ts:125-156`）

可用 usage 的判定 `messageProviderUsage(m)`（`token-estimator.ts:92-103`）：`role in {assistant, assistant_progress, assistant_tool_call}` **且** `providerUsage` 存在 **且** `usageStale` 不为真。

算法：

```
for i in range(len(messages) - 1, -1, -1):          # 从尾部向前找
    usage = messageProviderUsage(messages[i])
    if usage is None: continue
    tail = messages[i+1:]                            # 严格在其之后的消息
    estimatedTokens = estimateMessagesTokens(tail)
    return {
        totalTokens:        usage.totalTokens + estimatedTokens,
        providerUsageTokens: usage.totalTokens,
        estimatedTokens:     estimatedTokens,
        source:              'provider_usage_plus_estimate' if estimatedTokens > 0 else 'provider_usage',
        isExact:             estimatedTokens == 0,
        usageBoundary:       {messageIndex: i,
                              messageId: (messages[i].toolUseId if role == 'assistant_tool_call' else None)},
    }
# 没有可用 usage：
reason = staleUsageReason(messages)   # 正序第一条 role∈三者 且 有 providerUsage 且 usageStale 的 usageStaleReason ?? 'provider usage was marked stale'
estimatedTokens = estimateMessagesTokens(messages)     # 全量重估
return {totalTokens: estimatedTokens, providerUsageTokens: 0, estimatedTokens,
        source: 'estimate_only', isExact: False,
        stale: bool(reason), reason: reason or 'no provider usage available'}
```

边界语义：**只有 "usage 边界消息之后" 的尾部会被本地估算；边界及其之前完全信任 provider 报的 `totalTokens`（不重估、不加成）**。`usageBoundary.messageId` 只在边界消息是 `assistant_tool_call` 时有值（`messageBoundaryId`，`token-estimator.ts:120-123`）。

`staleUsageReason`（`token-estimator.ts:105-118`）按**正序**返回第一条 stale usage 的 `usageStaleReason`。

`markProviderUsageStale(message, reason)`（`token-estimator.ts:158-175`）：仅当 `role in {assistant, assistant_progress, assistant_tool_call}` 且 `providerUsage` 存在时返回 `{...message, usageStale: True, usageStaleReason: reason}` 新对象；否则**返回同一对象引用**。

### 5.3 `ContextStats` 字段（`token-estimator.ts:23-32, 177-206`）

| 字段 | 计算 |
|---|---|
| `estimatedTokens` | `accounting.estimatedTokens` |
| `totalTokens` | `accounting.totalTokens` |
| `providerUsageTokens` | `accounting.providerUsageTokens` |
| `contextWindow` | `window.contextWindow`（**完整窗口，不是 effectiveInput**） |
| `effectiveInput` | `window.effectiveInput = contextWindow - outputReserve` |
| `utilization` | `min(1, accounting.totalTokens / window.effectiveInput)` |
| `warningLevel` | 见下 |
| `accounting` | 上面的 `TokenAccountingResult`（含 `source`/`isExact`/`usageBoundary`/`stale`/`reason`） |

`warningLevel` 四级阈值（`token-estimator.ts:185-194`，**硬编码字面量，未引用 `THRESHOLDS`**）：

| 条件 | 级别 |
|---|---|
| `utilization >= 0.95` | `'blocked'` |
| `0.85 <= utilization < 0.95` | `'critical'` |
| `0.50 <= utilization < 0.85` | `'warning'` |
| `utilization < 0.50` | `'normal'` |

`utilization` 恒被截顶到 1（测试 `test/token-estimator.test.ts:130-136`）。

---

## 6. `model-context`

文件：`src/utils/model-context.ts`（149 行）。

`getModelContextWindow(model)`（`model-context.ts:132-149`）：

```
normalized = model.strip().lower()
for rule in MODEL_CONTEXT_RULES:                # 按数组顺序，首个命中即返回
    if any(pattern in normalized for pattern in rule.patterns):
        return {contextWindow: rule.contextWindow,
                outputReserve: rule.outputReserve,
                effectiveInput: rule.contextWindow - rule.outputReserve}
return {contextWindow: 128000, outputReserve: 8000, effectiveInput: 120000}   # UNKNOWN_MODEL_CONTEXT
```

匹配是**子串包含**（`in`），不是前缀/正则；大小写不敏感（先 `lower()`）；`trim()` 后匹配。

### 规则表（`model-context.ts:19-130`，顺序敏感，必须按此顺序线性查找）

| # | patterns | contextWindow | outputReserve | effectiveInput |
|---|---|---|---|---|
| 1 | `claude-opus-4-6`, `claude opus 4.6`, `opus-4-6` | 200000 | 16000 | 184000 |
| 2 | `claude-sonnet-4-6`, `claude sonnet 4.6`, `sonnet-4-6` | 200000 | 16000 | 184000 |
| 3 | `claude-haiku-4-5`, `claude haiku 4.5`, `haiku-4-5` | 200000 | 16000 | 184000 |
| 4 | `claude-opus-4-1`, `claude opus 4.1`, `opus-4-1`, `claude-opus-4`, `claude opus 4`, `opus-4` | 200000 | 16000 | 184000 |
| 5 | `claude-sonnet-4`, `claude sonnet 4`, `sonnet-4` | 200000 | 16000 | 184000 |
| 6 | `claude-3-7-sonnet`, `claude 3.7 sonnet`, `3-7-sonnet` | 200000 | 8192 | 191808 |
| 7 | `claude-3-5-sonnet`, `claude 3.5 sonnet`, `3-5-sonnet`, `claude-3-sonnet` | 200000 | 8192 | 191808 |
| 8 | `claude-3-5-haiku`, `claude 3.5 haiku`, `3-5-haiku` | 200000 | 8192 | 191808 |
| 9 | `claude-3-opus`, `claude 3 opus` | 200000 | 4096 | 195904 |
| 10 | `claude-3-haiku`, `claude 3 haiku` | 200000 | 4096 | 195904 |
| 11 | `gpt-5-codex`, `gpt-5.4`, `gpt-5.2`, `gpt-5.1`, `gpt-5` | 128000 | 16000 | 112000 |
| 12 | `o4-mini`, `o3`, `o1-pro`, `o1` | 200000 | 16000 | 184000 |
| 13 | `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4.1` | 1047576 | 16000 | 1031576 |
| 14 | `gpt-4o-mini`, `gpt-4o` | 128000 | 16384 | 111616 |
| 15 | `gpt-4` | 128000 | 8192 | 119808 |
| 16 | `gemini-2.5-pro`, `gemini 2.5 pro` | 1048576 | 16000 | 1032576 |
| 17 | `gemini-2.5-flash-lite`, `gemini 2.5 flash-lite` | 1048576 | 16000 | 1032576 |
| 18 | `gemini-2.5-flash`, `gemini 2.5 flash` | 1048576 | 16000 | 1032576 |
| 19 | `deepseek-reasoner` | 128000 | 16000 | 112000 |
| 20 | `deepseek-chat` | 128000 | 4000 | 124000 |
| 21 | `minimax-m3` | 1000000 | 16000 | 984000 |
| 22 | `minimax-m2.7` | 204800 | 16000 | 188800 |
| — | 未命中 | 128000 | 8000 | 120000 |

顺序陷阱示例：`claude-opus-4-6` 必须先于 `opus-4`；`gemini-2.5-pro`/`flash-lite`/`flash` 三者窗口相同所以顺序无影响；`gpt-4.1-*` 必须先于 `gpt-4`（因为 `'gpt-4'` 是 `'gpt-4.1'` 的子串）；`deepseek-reasoner` 先于 `deepseek-chat`。

另有一个**独立**的表 `getModelMaxOutputTokens`（`src/utils/context.ts:1-100`），与本节窗口表无关，不参与上下文管理四机制（仅用于 API 的 max_tokens 解析，`resolveMaxOutputTokens`，`context.ts:102-116`）。

---

## 7. `tool-result-storage`（工具结果持久化 + 预算）

文件：`src/utils/tool-result-storage.ts`（292 行）。

### 7.1 常量

| 常量 | 值 | 定义位置 |
|---|---|---|
| `TOOL_RESULTS_SUBDIR` | `'tool-results'` | `tool-result-storage.ts:7` |
| `PERSISTED_OUTPUT_TAG` | `'<persisted-output>'` | `:8` |
| `PERSISTED_OUTPUT_CLOSING_TAG` | `'</persisted-output>'` | `:9` |
| `DEFAULT_MAX_RESULT_SIZE_CHARS` | `50000` | `:11` |
| `MAX_TOOL_RESULTS_PER_BATCH_CHARS` | `200000` | `:12` |
| `PREVIEW_SIZE_CHARS` | `2000` | `:13` |

### 7.2 写入位置与路径安全

- 根目录 `MINI_CODE_DIR`（`src/config.ts:36-38`）= `os.environ['MINI_CODE_HOME']` 解析后的绝对路径，否则 `~/.mini-code`。
- 目录：`{MINI_CODE_DIR}/tool-results/{sessionId}`，其中 `sessionId = sanitizePathSegment(uuid4())`，**在模块加载时生成一次**（`tool-result-storage.ts:46`）→ 每个进程一个目录。
- 文件名：`{sanitizePathSegment(toolUseId)}.txt`（`tool-result-storage.ts:52-60`）。
- `sanitizePathSegment(v)`：`re.sub(r'[^a-zA-Z0-9._-]', '_', v)`；若结果为空 → 新 `uuid4()`（`tool-result-storage.ts:41-44`）。
- 额外包含性校验：`relative = posix-relative(dir, filepath)`；若以 `..` 开头或为绝对路径 → 改用 `{dir}/{uuid4()}.txt`（`tool-result-storage.ts:55-59`）。
- 写文件：`mkdir(dirname, recursive=True)` + `writeFile(filepath, content, encoding='utf8', flag='wx')`（`tool-result-storage.ts:99-109`）——**`wx` 不覆盖已存在文件**；`EEXIST` 被容忍并继续，其它错误 → `None`（放弃替换）。

### 7.3 预览截断 `generatePreview`（`tool-result-storage.ts:66-81`）

```
if len(content) <= 2000: return {preview: content, hasMore: False}
truncated = content[:2000]
lastNewline = truncated.rfind('\n')                 # JS lastIndexOf，未找到为 -1
cutPoint = lastNewline if lastNewline > 2000 * 0.5 else 2000     # 即 > 1000
return {preview: content[:cutPoint], hasMore: True}
```

换行边界特例：只有当**最后一个换行的位置严格大于 1000** 时才在换行处切（`preview` 不含该换行符本身），否则硬切 2000。

`formatChars(chars)`（`tool-result-storage.ts:83-87`）：

| 条件 | 输出 |
|---|---|
| `chars >= 1_000_000` | `f'{(chars / 1_000_000):.1f}M chars'` |
| `chars >= 1_000` | `f'{round(chars / 1_000)}K chars'` |
| 其它 | `f'{chars} chars'` |

### 7.4 替换文本模板 `buildPersistedToolResultMessage`（`tool-result-storage.ts:120-140`）

以 `\n` 连接的分片序列（`parts`）：

```
<persisted-output>
Output too large ({formatChars(originalSize)}). Full output saved to: {filepath}

Preview (first {formatChars(2000)}):        ← 实际渲染为 "Preview (first 2K chars):"
{preview}
[如果 hasMore: '...']                        ← 单独一行
</persisted-output>
```

即：

```
<persisted-output>\nOutput too large (50K chars). Full output saved to: C:\Users\x\.mini-code\tool-results\<sid>\<id>.txt\n\nPreview (first 2K chars):\n{preview}[\n...]\n</persisted-output>
```

空输出替换文本（`tool-result-storage.ts:165-171`）：`f'({toolName} completed with no output)'`。

### 7.5 单条结果 `replaceLargeToolResult`（`tool-result-storage.ts:142-196`）

签名等价于 `replace_large_tool_result(result, state_or_threshold=None, maybe_threshold=50000)`；`state` 只在第一个参数是对象时取用，`threshold` 在第一个参数是数字时取用，否则取 `maybe_threshold`。agent-loop 用 `(result, contentReplacementState)`（`agent-loop.ts:452-458`），因此 `state` 有效、阈值默认 50000。

严格顺序：

1. `content = normalizeToolResultContent(result.content)`：`None → ''`；非字符串 → `str(content)`（`tool-result-storage.ts:89-92`）。
2. 若 `state.replacements` 中存在该 `toolUseId` → **直接返回历史替换文本**（字节级重放，不重新落盘）。**此检查先于空内容检查**。
3. 若 `content.strip() == ''` → `state.seenIds.add(id)`，返回 `content = f'({toolName} completed with no output)'`（**不写入 `replacements`**）。
4. 若 `content.startswith('<persisted-output>')` → `seenIds.add`、`replacements[id] = content`，原样返回（避免二次持久化）。
5. 若 `len(content) <= threshold` → 返回规范化后的原文（**`== 50000` 不触发**，测试 `test/tool-result-storage.test.ts:61-70`）。
6. `persisted = await persistToolResult(content, toolUseId)`；失败（非 EEXIST 错误）→ 返回原文。
7. `replacement = buildPersistedToolResultMessage(persisted)`；`seenIds.add(id)`、`replacements[id] = replacement`；返回 `{...result, content: replacement}`。

### 7.6 批预算 `applyToolResultBudget`（`tool-result-storage.ts:198-292`）

输入 `(results, state, limit=200000)`；输出 `{results, newlyReplaced}`。`results` 为**同一工具批次**的结果（agent-loop 每次工具批次调用一次）。

1. `len(results) == 0` → `{results, newlyReplaced: []}`。
2. 顺序遍历，累计 `visibleSize` 并挑出 `freshCandidates`：

| 情况 | `visibleSize` | 是否候选 | 副作用 |
|---|---|---|---|
| `state.replacements` 有该 id | `+= len(previousReplacement)` | 否 | — |
| `state.seenIds` 有该 id | `+= len(content)` | 否 | — |
| `content.strip() == ''` | **不累加** | 否 | `seenIds.add(id)` |
| 已以 `PERSISTED_OUTPUT_TAG` 开头 | `+= len(content)` | 否 | `seenIds.add`、`replacements[id]=content`、`replacementMap[id]=content` |
| 其它 | `+= len(content)` | 是（`{toolUseId, content, size}`） | — |

3. 候选排序（`tool-result-storage.ts:250-253`）：`size` **降序**，同 size 用 `toolUseId.localeCompare` **升序**抖平。
4. 循环（`tool-result-storage.ts:255-273`）：`if visibleSize <= limit: break`（**`<=` 即停，`== 200000` 不替换**）；对候选：`persistToolResult` → **先** `seenIds.add(id)` → 若持久化失败 `continue`（无替换、无记录，但已标 seen）；否则 `replacementMap[id]=replacement`、`replacements[id]=replacement`、`visibleSize = visibleSize - size + len(replacement)`、追加 `newlyReplaced` 记录 `{kind:'tool-result', toolUseId, replacement}`。
5. 循环结束后把**所有** fresh 候选（包括未被替换的）加入 `seenIds`。
6. 若 `replacementMap` 为空 → **返回原 `results` 数组引用**（注意此时不做 `normalizeToolResultContent`，`None` 内容原样返回）。
7. 否则返回 `[ {**r, content: replacementMap[id]} if id in replacementMap else r for r in results ]`。

agent-loop 用法（`agent-loop.ts:472-482`）：对批次执行预算后，用 `toolUseId → result` 映射**回写真实 `messages` 中对应的 tool_result**。因此单条阈值与批阈值是**串联**的两道闸门。

### 7.7 `ContentReplacementState` 的角色（`tool-result-storage.ts:15-18,34-39`）

```
{seenIds: set[str], replacements: dict[str, str]}
```

- 由 `createContentReplacementState()` 创建；agent-loop 缺省时每回合新建（`agent-loop.ts:167-168`），而 `tty-app`/`index.ts` 在会话级创建并传入（`tty-app.ts:1754-1755`、`index.ts:112`）→ **进程级、会话级内存状态，不持久化**。
- 作用 1（幂等重放）：同一 `toolUseId` 永远得到**字节相同**的替换文本，不会因为重放而改写文件（`flag:'wx'` 进一步保证不覆盖）。
- 作用 2（避免重复计数/重复持久化）：`seenIds` 让已处理过的结果在后续批次的 `visibleSize` 统计与候选筛选中走"已见"分支。
- 注意 7.5 步骤 3（空输出）与 7.6 步骤 2（空内容）**只写 `seenIds`、不写 `replacements`**，因此空输出不会像大输出那样被记忆为可重放替换。

---

## 8. `agent-loop` 单步执行顺序（阈值与重算点）

主循环 `for step in 0..maxSteps-1`（`agent-loop.ts:204`）。每一步内的精确顺序：

| 序 | 动作 | 行号 | 频率 |
|---|---|---|---|
| 1 | `throwIfAborted(signal)` | `205` | 每步 |
| 2 | `latestStats = None`；`modelMessages = messages` | `206-207` | 每步 |
| 3 | 若 `modelName` 非空：`latestStats = computeContextStats(messages, modelName)` | `209-210` | 每步 |
| 4 | **snip**：`if not snippedThisTurn:` → `snipCompactConversation({messages, contextStats: latestStats, modelContextWindow: latestStats.effectiveInput})`；若 `didSnip` → `messages = result.messages`；`snippedThisTurn = True`；`onSnipCompact(result)`；`latestStats = computeContextStats(messages)`；`onContextStats(latestStats)` | `212-225` | 每步尝试，**每回合最多成功一次** |
| 5 | **microcompact**：`before = messages`；`messages = microcompact(messages, modelName)`；若引用变化 → `latestStats = computeContextStats(messages)`；`onContextStats` | `227-232` | 每步（≥0.50 时才可能变） |
| 6 | **context collapse**：`applyContextCollapseIfNeeded(messages, modelName, model, contextCollapseState)`；`throwIfAborted`；`replaceContextCollapseState(result.state)`；`modelMessages = result.messages`；若 `collapsed` → `onContextCollapse(result)`、`latestStats = computeContextStats(modelMessages)`、`onContextStats`；否则若 `modelMessages is not messages` → 同样重算 | `234-250` | 每步（≥0.75 时才可能新建 span） |
| 7 | **autoCompact**：仅 `step == 0 and modelName` 非空。`latestStats = latestStats or computeContextStats(modelMessages)`；`onContextStats(latestStats)`；若 `warningLevel in {'critical','blocked'}` → `autoCompact(modelMessages, modelName, model)`；`throwIfAborted`；若返回非空 → `messages = result.messages`、`modelMessages = messages`、`replaceContextCollapseState(createContextCollapseState())`（**重置 collapse 状态**）、`onAutoCompact(result)`、重算 stats | `253-269` | **仅每回合 step 0** |
| 8 | plan 注入：`modelMessages = withPlanContext(modelMessages, plan)`；若 `modelName` → `onContextStats(computeContextStats(modelMessages))`（只上报，**不写入 `latestStats`**） | `271-274` | 每步（有 plan 时） |
| 9 | runtime 注入：`modelMessages = withRuntimeContext(modelMessages, runtimeContext())` | `276` | 每步（有回调时） |
| 10 | `model.next(modelMessages, {tools, signal})` | `278-281` | 每步 |
| 11 | 工具执行：追加 `assistant_tool_call` + 占位 `tool_result('Cancelled before execution.', isError=True)` 到 **真实 `messages`** | `427-433` | 有 tool calls 时 |
| 12 | 逐个工具：执行 → 用结果改写 `messages` 里对应 `tool_result`（`448-450`）→ `replaceLargeToolResult(result, contentReplacementState)`（**单条 50K 阈值**）→ 再改写 `messages`（`452-460`） | `435-470` | 每个工具调用 |
| 13 | 批次预算：`applyToolResultBudget(batchResults, contentReplacementState)`（**批 200K 阈值**）→ 按 `toolUseId` 回写真实 `messages` | `472-482` | 每个工具批次 |
| 14 | 处理 `awaitUser` / abort / `batchOutcome` 收尾；否则进入下一个 step | `484-500` | — |

要点归纳：

- **每步运行**：snip（尝试）、microcompact、context collapse。
- **每回合仅 step 0 运行**：autoCompact。
- **每回合最多一次**：成功的 snip（`snippedThisTurn`）。
- **每个工具/批次运行**：单条结果替换、批预算。
- **stats 重算点**（共 6 处）：step 开头（`210`）、snip 成功后（`222`）、microcompact 有变化后（`230`）、collapse 折叠或投影变化后（`245/248`）、step 0 autocompact 之前（`255`）与之后（`265`）、plan 注入后（`273`，仅上报）。每次重算都通过 `onContextStats` 回调上报（除 `210` 之外）。
- **顺序蕴含**：microcompact 先于 collapse ⇒ collapse 看到的 `messages` 已经是清空后的；collapse 的投影 `modelMessages` 才是 autoCompact 的输入 ⇒ autocompact 的 0.85 判定基于**投影后**的估算；autoCompact 成功后 collapse 状态被清空。
- 回合结束由 `tty-app.ts:1658-1660` 把 `result.messages` 整体 `splice` 回 `args.messages` 并 `saveSession`。

---

## 9. Gotchas（quirk / 不一致 / 定义了但未使用的常量）

以下每一条都是复刻时需要**显式决策**的点。

**定义为常量但从未被使用：**

1. `THRESHOLDS.BLOCKED_UTILIZATION = 0.95`（`constants.ts:4`）没有任何引用；`blocked` 级别用的是 `token-estimator.ts:186` 里的字面量 `0.95`。同理 `0.85`/`0.50` 也是字面量（`token-estimator.ts:188,190`），所以 `THRESHOLDS` 只有 `MICROCOMPACT_UTILIZATION`、`AUTOCOMPACT_UTILIZATION` 真正被消费。
2. `RETENTION.MIN_KEEP_TOKENS = 10_000`（`constants.ts:23`）从未使用。`findRetentionBoundary` 只用 `MAX_KEEP_TOKENS` 与 `MIN_KEEP_MESSAGES`，因此"下限 token"实际上不存在（`compact.ts:58-86`）。
3. `LIMITS.PTL_MAX_RETRIES = 2`（`constants.ts:30`）与 `LIMITS.SUMMARY_MAX_OUTPUT_TOKENS = 4_096`（`constants.ts:29`）从未使用：摘要请求（compact 与 collapse）都不设置 max_tokens，也没有 prompt-too-long 重试逻辑。
4. `ContextCollapseOptions.reason` 的枚举包含 `'overflow_recovery'`（`context-collapse.ts:27`），但代码里只可能出现 `'context_pressure'`（默认）或调用方显式传入的 `'manual'`（`tty-app.ts:1089`）。

**行为不一致 / 潜在 bug：**

5. **snip 与 collapse 对"并行工具调用"的分组判定相反**：snip 的 `buildMessageGroups` 只把"紧邻配对"当闭合，连续多 call 批次全部被判未闭合 + 结果判孤儿 → 整批受保护（`snipCompact.ts:154-182`）；collapse 的 `toolGroupIsClosed` 做双向集合相等 → 批次判闭合、可折叠（`context-collapse.ts:328-353`）。
6. **snip 计算"释放量"用纯估算，但"触发/目标"用 provider-aware 总量**：`desiredTokensToFree = max(2000, contextStats.totalTokens - floor(effectiveInput*0.60))`，而 `tokensBefore/tokensAfter` 都是 `estimateMessagesTokens`（`snipCompact.ts:408-409,442-446,480-481`）。两者基准不同，可能算出的"需要释放"远大于实际能释放。
7. **snip 成功后把所有保留消息的 provider usage 标记为 stale**（`snipCompact.ts:395-400`），于是 `tokenCountWithEstimation` 立刻退化为 `estimate_only`；`TokenAccountingResult.stale/reason` 也会因此被填上 snip 的原因（`token-estimator.ts:145-155`）。
8. **collapse 投影会再次把所有 provider usage 标 stale**（`context-collapse.ts:234,245`）⇒ 只要存在任何已提交 span，模型可见投影的 stats 永远是 `estimate_only`；同时 `usageStaleReason` 会被 collapse 的原因**覆盖**（`markProviderUsageStale` 无条件重写 reason，`token-estimator.ts:168-171`）。多个机制叠加时最后写入者获胜。
9. **collapse 的候选索引基于原始数组，stats 基于投影数组**（`context-collapse.ts:626-637`）：`findCollapseCandidate(messages, …)` 用原始 `messages`，而 `currentTokens/effectiveInput` 取自投影后的 stats。两者消息数不同，属于不同索引空间。
10. **agent-loop 内联的 snip 不重置 collapse 状态**（`agent-loop.ts:212-225`），而 `/snip` 命令路径会重置（`tty-app.ts:1153`）。snip 会移动数组下标，若 span 的 `messageIds` 是位置伪 id（`message-{i}`，因为 compact 生成的 `context_summary` 没有 `id`，见 `compact.ts:173-178`），下标变化后 `projectSpan` 校验失败 → 该 span 静默失效、被折叠的原文会重新出现在投影里（`context-collapse.ts:172-192`）。autocompact 与手动 compact 路径都重置了 collapse 状态（`agent-loop.ts:263`、`tty-app.ts:1205`），恰好避开这个问题。
11. **`committedCollapsedMessageIds` 名字与实现对不上**：它同时收集 `'staged'` 与 `'committed'`（`context-collapse.ts:355-364`），这是为了让多 span 同一 pass 内互不重叠所必需的。
12. **`compactConversation` 硬编码 `messages.slice(1, boundary)`**（`compact.ts:142`）：若 `messages[0]` 不是 `system`，它既不被压缩也不被保留（只有 `systemMessages` 会被重新拼回），即**下标 0 的非 system 消息会被静默丢弃**。
13. **`alignBoundaryToApiRound` 可能把边界左移到 0**，此时 `messagesToCompress` 为空 → compact 返回 `None`（`compact.ts:50-52,150-152`）。表现为"利用率很高但压缩静默失败"，并且这次失败会**计入 autocompact 的连续失败计数**（`auto-compact.ts:70-74`），三次后进程内禁用。
14. **`parseSummaryFromResponse` 在"只有 `<analysis>`、没有 `<summary>`"时返回 `None`，而在"两者都没有"时返回整段原文**（`prompt.ts:23-38`）；`<summary></summary>`（空）会穿透到该分支（因为空串为假）。
15. **`applyToolResultBudget` 在 `replacementMap` 为空时返回原数组引用且不做内容规范化**（`tool-result-storage.ts:279-281`）：`content=None` 的条目此时仍是 `None`，而单条路径会把它变成 `''`/占位文本。批路径**不会**为空白内容生成 `(tool completed with no output)` 占位。
16. **批预算对 `content.strip() == ''` 的条目不给 `visibleSize` 累加**（`tool-result-storage.ts:228-231`）：空白串（如 `' \n\t '`，长度 4）被记为 seen 却不计入可见体积，因此这 4 字符永远不会触发预算。
17. **`persistToolResult` 的 `wx` + EEXIST 容忍**（`tool-result-storage.ts:99-109`）：若同一 `toolUseId` 的文件已存在但内容是旧的，函数仍用**新**内容计算 `preview/originalSize` 组装替换文本，磁盘上却保留旧内容——"替换文本描述的"与"文件里的"会不一致（正常路径由 `state.replacements` 事先拦截，只有非幂等调用才会出现）。
18. **批预算失败时先标 `seenIds`**（`tool-result-storage.ts:258-262`）：`persist` 失败的候选已被记为"已见"，同一 id 在后续批次会走 `seenIds` 分支，永久失去被替换的机会（内容仍全量进入上下文）。
19. **`sessionId` 是模块加载时生成的 UUID**（`tool-result-storage.ts:46`）：同一进程内所有会话共用一个 `tool-results/<uuid>` 目录；跨进程重启后目录名变化，旧的持久化文件路径仍写在历史 transcript 里（指向已存在但不再复用的目录）。
20. **snip 会把 `snip_boundary` 的原始 `content` 丢弃在发往模型之前**：Anthropic 适配器渲染 `snip_boundary` 时使用固定的 `buildAnthropicSnipBoundaryText()`（不含 `removedCount`/`tokensFreed` 数字），而非 `message.content`（`src/anthropic-adapter.ts:282-287`、`snipCompact.ts:365-372`）。`context_summary` 则渲染为 `` `[Context Summary from earlier conversation]\n${content}` ``（`anthropic-adapter.ts:275-280`）。
21. **microcompact 的清空不落盘**：`saveSession` 只追加 `alreadySavedCount` 之后、且无 `id` 的消息（`session.ts:230-239`），而被清空的旧 `tool_result` 位于该下标之前且没有 `id`，所以清空只存在于内存 transcript（`tty-app.ts:1658-1660`）。snip/compact 路径通过写入新消息（边界/摘要）间接持久化，或重置 `alreadySavedCount`（`tty-app.ts:1149-1152`）。
22. **JS 数值/字符串语义**：`Math.round(x)` 是"半数向上"（Python `round` 是"半数取偶"，`round(50.5)` 在 JS 是 51、Python 是 50）——涉及 `formatChars` 的 `round(chars/1000)` 与 snip 边界文本里的 `round(tokensFreed)`；`String.length` 是 UTF-16 码元数，Python `len(str)` 是码点数（emoji 等星平面字符会让两者差 1 倍）；`JSON.stringify` 输出无空格、不转义非 ASCII、跳过 `undefined` 属性，对应 `json.dumps(..., separators=(',',':'), ensure_ascii=False)` 仍会在 `None`→`null` 等细节上不同。`assistant_thinking` 的 `blocks` 无 `content` 字段，估算它时绝不能用 `content`。
23. **`estimateCollapseSummaryTokens` 只用于候选估算**（`context-collapse.ts:129-131`）；真正判定"净省是否达标"用的是 `estimateMessagesTokens([buildCollapsedSummaryMessage(span)])`（含真实摘要长度，`context-collapse.ts:680-685`）。两者可能给出不同结论。
24. **collapse 单 pass 时"净省不足"计入失败计数**（`context-collapse.ts:682-685`），这会推动 `consecutiveFailures` 达到 3 并**禁用整个 collapse 机制**（`context-collapse.ts:549-557`）——不是因为模型出错，而是因为省得不够。
25. **`markProtectedGroups` 的第二阶段遍历所有组（含候选范围之外的组）**（`snipCompact.ts:226-234`）：范围外的组被标 `'outside_candidate_range'` 后仍可再追加 `'near_file_edit'`/`'near_important_error'`，这些 reason 只用于日志/调试（`reasons` 数组从未被外部读取，只在 `snipCompact.ts:423` 打印 `range.reason`）。
26. **`isProtectedToolName` 的子串匹配**（`snipCompact.ts:99-104`）：`'edit'` 会命中 `credit`、`'write'` 会命中 `overwrite`、`'modify'`/`'patch'` 同理，保护面比 `PROTECTED_TOOL_NAMES` 字面集合大得多。
27. **snip 的删除区间可能包含 `assistant_thinking`**：snip 的分组对 thinking 没有特殊处理，`protected=False`（`snipCompact.ts:184-191`），因此思维块会被删除；collapse 则把 thinking 与其 tool 组绑定（`context-collapse.ts:258-279`）。
28. **`snipCompact` 的 `utilization` 不截顶**（`snipCompact.ts:414-415`），而 `ContextStats.utilization` 截顶到 1（`token-estimator.ts:183`）；调用点传的是 `contextStats.effectiveInput` 作为 `modelContextWindow`（`agent-loop.ts:216`），即两个参数通常相等。
29. **`usageBoundary.messageId` 只在边界消息是 `assistant_tool_call` 时非 `None`**（`token-estimator.ts:120-123,138-141`），其余两种情况该字段为 `undefined`，不要用它做唯一键。
30. **`failedCollapseResult` 返回的 `state.spans` 是原 span 的浅拷贝**，但 `committedCollapseResult` 返回的 span 对象是**新建的 `{...span, status:'committed'}`**（`context-collapse.ts:580-588`）——调用方若持有 `plannedSpans` 引用不会看到 `status` 变化。

---

## 附录 A：所有占位/标记消息一览（复制粘贴用）

| 机制 | 插入位置 | role | 精确内容模板 |
|---|---|---|---|
| microcompact | 原地替换旧 `tool_result.content` | `tool_result`（不新增消息） | `[Output cleared for context space]` |
| snip | 删除区段的原位 | `snip_boundary` | 见 §2.7（`[Snipped earlier conversation segment]` 开头，含 `messages` 与 `approximate tokens freed` 两行） |
| collapse | 投影中替换 span | `context_summary` | 见 §3.5（`[Collapsed context summary]` 开头 + 两行说明 + 空行 + 摘要） |
| autocompact / manual compact | 结果数组的 `systemMessages` 之后 | `context_summary` | 纯摘要文本（`parseSummaryFromResponse` 的输出），无前缀 |
| tool-result-storage | 原地替换超大 `tool_result.content` | `tool_result`（不新增消息） | 见 §7.4（`<persisted-output>` / `</persisted-output>` 包裹） |
| tool-result-storage | 原地替换空 `tool_result.content` | `tool_result` | `({toolName} completed with no output)` |

## 附录 B：阈值速查

| 数值 | 名称 | 出处 | 语义 |
|---|---|---|---|
| 0.50 | `MICROCOMPACT_UTILIZATION` | `constants.ts:2` | microcompact 触发（`>=`） |
| 0.70 | `SNIP_COMPACT_THRESHOLD` | `constants.ts:7` | snip 触发（`>=`） |
| 0.75 | `CONTEXT_COLLAPSE_UTILIZATION` | `constants.ts:13` | collapse 触发（`<` 即跳过） |
| 0.65 | `CONTEXT_COLLAPSE_TARGET_USAGE` | `constants.ts:14` | 多 span 循环停止（`<=` 即停） |
| 0.60 | `SNIP_TARGET_USAGE` | `constants.ts:8` | snip 目标利用率 |
| 0.95 / 0.85 / 0.50 | `warningLevel` 阈值（字面量） | `token-estimator.ts:186-190` | blocked / critical / warning |
| 0.85 | `AUTOCOMPACT_UTILIZATION` | `constants.ts:3` | autocompact 触发 |
| 3 | `KEEP_RECENT_TOOL_RESULTS` / `MIN_KEEP_MESSAGES` 语境不同 | `constants.ts:21,22` | 前者保留工具结果条数，后者保留消息条数 |
| 6 | `MIN_KEEP_MESSAGES` / `SNIP_MIN_MESSAGES_TO_REMOVE` | `constants.ts:22,9` | 两者独立定义，值相同 |
| 12 | `SNIP_KEEP_RECENT_MESSAGES` / `CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES` | `constants.ts:10,15` | 尾部保护窗口 |
| 2000 | `SNIP_MIN_TOKENS_TO_FREE` / `CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE` / `PREVIEW_SIZE_CHARS` | `constants.ts:11,16`、`tool-result-storage.ts:13` | 前两者 token，后者字符 |
| 40000 | `MAX_KEEP_TOKENS` | `constants.ts:24` | compact 尾部保留 token 上限 |
| 20000 | `MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT` | `constants.ts:31` | 小窗口模型跳过 autocompact |
| 3 | `MAX_AUTOCOMPACT_FAILURES` / `CONTEXT_COLLAPSE_MAX_FAILURES` | `constants.ts:28,18` | 熔断阈值 |
| 2 | `CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS` | `constants.ts:17` | 单次折叠 span 上限 |
| 50000 / 200000 | `DEFAULT_MAX_RESULT_SIZE_CHARS` / `MAX_TOOL_RESULTS_PER_BATCH_CHARS` | `tool-result-storage.ts:11,12` | 字符数阈值 |
| 0.15 / 128 | collapse 摘要开销估算系数/下限 | `context-collapse.ts:130` | `max(128, ceil(tokens*0.15))` |
| 500 | compact 摘要输入对 tool_result 的截断 | `compact.ts:106-108` | 字符 |
| 50% of 2000 = 1000 | preview 换行切点门槛 | `tool-result-storage.ts:73` | 严格 `>` |
