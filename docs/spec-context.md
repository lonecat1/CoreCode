# CoreCode 上下文管理子系统 — 实现规范（Python 复刻版）

> 来源仓库（只读）：`D:\CoreCode\CoreCode`。本规范基于以下文件的逐行阅读：`src/compact/constants.ts`、`src/compact/microcompact.ts`、`src/compact/snipCompact.ts`、`src/compact/context-collapse.ts`、`src/compact/auto-compact.ts`、`src/compact/compact.ts`、`src/compact/prompt.ts`、`src/utils/token-estimator.ts`、`src/utils/model-context.ts`、`src/utils/context.ts`、`src/utils/tool-result-storage.ts`，并交叉验证调用点 `src/agent-loop.ts`。文中所有规则都给出 `file:line` 引用。
>
> 本文只描述语义，不修改任何被描述文件。所有常量、模板字符串、排序规则均按源码原样给出；JS 与 Python 的数值/字符串差异集中列在 §9「Gotchas」。

---

## 0. 总览

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

### 1.1 触发条件与全部数值

### 1.2 精确算法

### 1.3 保护规则

### 1.4 突变范围

### 1.5 状态与失败行为

无状态、无失败计数、无熔断；纯函数。

---

## 2. `snipCompact`（删除中间安全区段 + 插入边界消息）

### 2.1 触发条件与全部数值

```
triggerTokens  = contextStats.totalTokens                 # provider-aware 总量
tokensBefore   = estimateMessagesTokens(params.messages)  # 纯本地估算
effectiveInput = contextStats.effectiveInput > 0 ? contextStats.effectiveInput : params.modelContextWindow
utilization    = effectiveInput > 0 ? triggerTokens / effectiveInput : contextStats.utilization
if utilization < 0.70: 返回 noSnip('below_threshold')
```

设 `n = len(messages)`：

**候选区段是"半开区间 `[start, end)`"，以真实消息下标表示，且 `end` 之外（含最后一条 user 及其之后）全部不可删。**

顺序遍历，产出 `MessageGroup{start,end,messages,tokens,protected,reasons}`；`tokens = sum(estimateMessageTokens(m) for m in group.messages)`：

| 当前消息 | 分组规则 | `protected` | `reasons` |
|---|---|---|---|
| `assistant_tool_call` | 若 `messages[i+1]` 存在、`role == 'tool_result'` 且 `toolUseId` 相等 → 组 = `[call, result]`（长度 2）；否则组 = `[call]` | 长度 1 时 `True`，长度 2 时 `False` | 长度 1 → `['unclosed_tool_call']`；长度 2 → `[]` |
| `tool_result`（未被前一条 call 吞掉） | `[result]` | 恒 `True` | `['orphan_tool_result']` |
| 其它（含 `system`/`user`/`assistant`/`assistant_thinking`/`context_summary`/`snip_boundary`） | 单条 | `False` | `[]` |

**检测语义**：
- 「未闭合 tool_call」= 该 `assistant_tool_call` 的**紧邻下一条**不是 `toolUseId` 相同的 `tool_result`。
- 「孤儿 tool_result」= 任何没有被其紧邻前一条同 id `assistant_tool_call` 消费掉的 `tool_result`。
- **并行调用批次（连续多条 `assistant_tool_call` 后再跟多条 `tool_result`）在本函数下全部被判为未闭合 + 孤儿，因此整批受保护**（这是与 collapse 的 `toolGroupIsClosed` 的关键差异，见 §9）。

- 若 `group.start < candidateStart or group.end > candidateEnd` → 加 `'outside_candidate_range'` 并 `continue`（完全落在候选区间**内**的组才有机会被删）。
- 否则若 `any(isBoundaryMessage(m) for m in group.messages)` → 加 `'boundary_message'`。

- `groupHasProtectedTool(group)` → `protectNearbyGroups(groups, i, 'near_file_edit')`。
- `groupHasImportantError(group)` → `protectNearbyGroups(groups, i, 'near_important_error')`。

```
normalized = toolName.strip().lower()
return (normalized in PROTECTED_TOOL_NAMES
        or 'patch' in normalized or 'write' in normalized
        or 'edit' in normalized or 'modify' in normalized)
```

（子串匹配意味着 `patch_anything`、`overwrite_file`、`credit_report`、`modify_*` 等也会被保护——`edit` 是 `credit` 的子串。）

```
messageTextLooksImportantError(m) or (m.role == 'tool_result' and toolResultLooksImportantError(m))
```

1. `tokens` **降序**
2. `messagesCount` **降序**
3. `start` **升序**（兜底稳定）

取 `bestRun = safeRuns[0]`；为空 → `noSnip('no_safe_interval')`。

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

| 字段 | 值 |
|---|---|
| `id` | `f'snip-{timestamp}-{removedMessageIds[0] or "none"}'`，`timestamp = Date.now()`（毫秒） |
| `role` | `'snip_boundary'` |
| `content` | `buildSnipBoundaryContent({removedCount: len(removedMessages), tokensFreed: deletion.tokens})` |
| `removedMessageIds` | 上述 id 数组 |
| `removedCount` | `len(removedMessages)` |
| `tokensFreed` | `deletion.tokens`（此时还是"删除总量"，未扣边界开销） |
| `timestamp` | 同上 `Date.now()` |

```
[Snipped earlier conversation segment]

A middle portion of the earlier conversation was removed to preserve context space.

Removed range:
- messages: {removedCount}
- approximate tokens freed: {max(0, round(tokensFreed))}

The recent conversation and active task context are preserved.
```

```
messagesAfterSnip = markRetainedUsageStale(
    messages[:deletion.start]
  + [{...boundaryMessage,
      content: buildSnipBoundaryContent({removedCount, tokensFreed: estimatedTokensFreed}),
      tokensFreed: estimatedTokensFreed}]
  + messages[deletion.end:]
)
```

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

### 2.8 突变范围、状态、回滚

---

## 3. `contextCollapse`（投影式摘要折叠）

### 3.1 触发条件与全部数值

```
currentState  = normalize(state)                 # 浅拷贝 spans
if not currentState.enabled: return unchanged(messages, currentState)
currentProjected = projectCollapsedView(messages, currentState)
stats = computeContextStats(currentProjected, model)
if stats.utilization < options.utilizationThreshold: return unchanged(currentProjected, currentState)
```

**注意返回的是 `currentProjected`**：即使本次不折叠，已提交 span 的投影依然生效。

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

```
protectedGroup = group.protected
              or group.start < 0                       # 恒 False
              or group.end > protectedStart
              or any(isCollapseBoundary(m) for m in group.messages)
              or any(collapsedIds.has(messageId(m, group.start + offset))
                     for offset, m in enumerate(group.messages))
```

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

与 snip 的分组**不同**：按"API 轮次"聚合，且用闭包检测保护：

| 起始消息 | 组内容 | `protected` |
|---|---|---|
| `assistant_thinking` | 该 thinking + 紧随的所有连续 `assistant_tool_call` + 紧随的所有连续 `tool_result` | `hasToolCall and not toolGroupIsClosed(group)` |
| `assistant_tool_call` | 紧随的所有连续 `assistant_tool_call` + 紧随的所有连续 `tool_result` | `not toolGroupIsClosed(group)` |
| `tool_result` | 单条 | `True` |
| 其它 | 单条 | `False` |

```
calls   = {m.toolUseId for m in messages if m.role == 'assistant_tool_call'}
results = {m.toolUseId for m in messages if m.role == 'tool_result'}
if not calls and not results: return True
if not calls or not results:  return False
return all(cid in results for cid in calls) and all(rid in calls for rid in results)
```

即：**双向包含检查**（call 集合与 result 集合必须完全相等）；空组视为闭合；只要一侧为空即不闭合。**并行调用批次在此判定下是闭合的**（与 snip 相反）。

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

**副作用（极重要）**：投影输出的每一条非摘要消息都过了一遍 `markProviderUsageStale` ⇒ 只要存在任何已提交 span，**模型可见投影中所有 provider usage 都是 stale**，`tokenCountWithEstimation` 恒为 `estimate_only`。

1. `span.status != 'committed'` 或 `len(span.messageIds) == 0` → `None`。
2. 建立 `indexById: {messageId(m, i): i}`（`messageId = m.id ?? f'message-{i}'`）。
3. `indices = [indexById[id] for id in span.messageIds]`；任一缺失 → `None`。
4. `indices` 必须**严格连续**（`indices[i] == indices[i-1] + 1`），否则 `None`。
5. `start = indices[0]`、`end = indices[-1] + 1`；必须 `messageId(messages[start], start) == span.startMessageId` 且 `messageId(messages[end-1], end-1) == span.endMessageId`，否则 `None`。
6. 返回 `{start, end, message: buildCollapsedSummaryMessage(span)}`。

| 字段 | 值 |
|---|---|
| `id` | `f'collapse-summary-{span.id}'` |
| `role` | `'context_summary'` |
| `content` | `buildCollapsedSummaryContent(span)` |
| `compressedCount` | `len(span.messageIds)` |
| `timestamp` | `span.createdAt` |

```
[Collapsed context summary]
This summary replaces messages {span.startMessageId} through {span.endMessageId} in the model-visible context only.
The original transcript is preserved in the session/UI.

{span.summary}
```

### 3.6 摘要请求文本

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

| 结果构造器 | 触发时机 | `messages` | `state` | `collapsed` | `span(s)` |
|---|---|---|---|---|---|
| `unchangedCollapseResult` | `!enabled`；利用率低于阈值；无候选；plans 为空 | 传入值（通常是 `currentProjected`） | **原对象引用**（未归一化） | `False` | `spans: []` |
| `failedCollapseResult` | 适配器异常；响应非 assistant/空；摘要解析失败；单 pass 且净省不足 | `currentProjected` | 新对象：`consecutiveFailures = state.consecutiveFailures + 1`；`enabled = False if consecutiveFailures >= maxFailures else state.enabled`；`spans = [...state.spans]` | `False` | `spans: []` |
| `committedCollapseResult` | 至少一个 planned span | `projectCollapsedView(messages, nextState)` | 新对象：`spans = [...state.spans, ...spans(status='committed')]`、`consecutiveFailures = 0` | `True` | `span = committed[0]`，`spans = 全部新提交 span` |

### 3.8 突变范围、状态、回滚

---

## 4. `autoCompact` + `compactConversation`（全量 LLM 压缩）

文件：`src/compact/auto-compact.ts`（82 行）、`src/compact/compact.ts`（200 行）、`src/compact/prompt.ts`（38 行）、`src/compact/manual-compact.ts`（15 行）。

### 4.1 门控与常量

```
[auto-compact] source={accounting.source} total={accounting.totalTokens} provider={accounting.providerUsageTokens} estimate={accounting.estimatedTokens} utilization={utilization:.3f} threshold=0.85 should={true|false}
```

### 4.2 状态与持久性

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

1. `m = regex_search(r'<summary>([\s\S]*?)</summary>', response)`；若匹配且捕获组**真值（非空字符串）** → 返回 `group.strip()`。
2. 否则 `a = regex_search(r'<analysis>([\s\S]*?)</analysis>', response)`；若**没有** `<analysis>` → `t = response.strip()`；若 `len(t) > 0` → 返回 `t`；否则 `None`。
3. 若存在 `<analysis>` 但没有 `<summary>` → 返回 `None`。

（`<summary>   </summary>`：捕获组 `'   '` 为真 → 返回 `''`，调用方的 `if not summary` 会把它当失败。）

### 4.5 突变范围与失败行为

---

## 5. `token-estimator`

文件：`src/utils/token-estimator.ts`（208 行）。

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

| role | 长度定义 |
|---|---|
| `system`/`user`/`assistant`/`assistant_progress` | `len(content)`（**UTF-16 码元数**） |
| `assistant_thinking` | `len(JSON.stringify(blocks))`；异常 → 0 |
| `assistant_tool_call` | `len(JSON.stringify(input))`；异常 → 0 |
| `tool_result`/`context_summary`/`snip_boundary` | `len(content)` |
| 其它 | 0 |

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

| 条件 | 级别 |
|---|---|
| `utilization >= 0.95` | `'blocked'` |
| `0.85 <= utilization < 0.95` | `'critical'` |
| `0.50 <= utilization < 0.85` | `'warning'` |
| `utilization < 0.50` | `'normal'` |

---

## 6. `model-context`

文件：`src/utils/model-context.ts`（149 行）。

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

---

## 7. `tool-result-storage`（工具结果持久化 + 预算）

文件：`src/utils/tool-result-storage.ts`（292 行）。

### 7.1 常量

### 7.2 写入位置与路径安全

```
if len(content) <= 2000: return {preview: content, hasMore: False}
truncated = content[:2000]
lastNewline = truncated.rfind('\n')                 # JS lastIndexOf，未找到为 -1
cutPoint = lastNewline if lastNewline > 2000 * 0.5 else 2000     # 即 > 1000
return {preview: content[:cutPoint], hasMore: True}
```

换行边界特例：只有当**最后一个换行的位置严格大于 1000** 时才在换行处切（`preview` 不含该换行符本身），否则硬切 2000。

| 条件 | 输出 |
|---|---|
| `chars >= 1_000_000` | `f'{(chars / 1_000_000):.1f}M chars'` |
| `chars >= 1_000` | `f'{round(chars / 1_000)}K chars'` |
| 其它 | `f'{chars} chars'` |

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
<persisted-output>\nOutput too large (50K chars). Full output saved to: C:\Users\x\.corecode\tool-results\<sid>\<id>.txt\n\nPreview (first 2K chars):\n{preview}[\n...]\n</persisted-output>
```

严格顺序：

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

```
{seenIds: set[str], replacements: dict[str, str]}
```

---

## 8. `agent-loop` 单步执行顺序（阈值与重算点）

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

---

## 9. Gotchas（quirk / 不一致 / 定义了但未使用的常量）

以下每一条都是复刻时需要**显式决策**的点。

**定义为常量但从未被使用：**

**行为不一致 / 潜在 bug：**

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

