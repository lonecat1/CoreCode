# 与原 TypeScript 实现的差异

本文件记录 **minicode-py 有意不照搬 TS 原实现**的地方，逐条给出
「原行为 / 本实现 / 理由 / 影响面」。

分界原则：

> **复刻观察到的行为，修正已被证明会静默损害数据的缺陷。**

阈值、护栏、排序规则、占位文本模板属于**行为契约**，一律严格复刻——
改了就不再「架构对等」。只有会**静默**造成数据丢失或计量失真的地方才修正，
因为这类缺陷不报错、只在长会话里慢慢显现，最难排查。

每条都标了影响面，便于未来与原实现做行为对拍时定位差异。

---

## D1 · 四个零引用常量：不复刻

| | |
|---|---|
| **原行为** | `constants.ts` 定义了 `BLOCKED_UTILIZATION=0.95`、`MIN_KEEP_TOKENS=10000`、`LIMIT_PTL_MAX_RETRIES=2`… 等四个常量，但全仓零引用（实测各只出现 1 次，即仅定义处）。 |
| **本实现** | 不引入。 |
| **理由** | 保留零引用常量只会误导后来者以为它们生效。 |
| **影响面** | 无行为差异。 |

---

## D2 · `warningLevel` 阈值：收敛为单一来源

| | |
|---|---|
| **原行为** | 四个阈值（0.95/0.85/0.50）在 `token-estimator.ts` 里**硬编码字面量**，与 `constants.ts` 的 `THRESHOLDS` 数值重复但不共享。 |
| **本实现** | `context/constants.py` 的 `BLOCKED_UTILIZATION` / `CRITICAL_UTILIZATION` / `WARNING_UTILIZATION` 被 `tokens.py` 直接引用。 |
| **理由** | 同一语义散落两份是隐患：改一处忘另一处会让压缩层级失调。 |
| **影响面** | 对外阈值数值完全一致，无行为差异。 |

---

## D3 · collapse 的 usage 失效范围（**最重要的一条**）

| | |
|---|---|
| **原行为** | `projectCollapsedView` 对投影中的**每一条**非摘要消息调用 `markProviderUsageStale`。后果：只要存在任何已提交 span，模型可见投影里就再没有可用的 provider usage，`tokenCountWithEstimation` **永久**退化为 `estimate_only`。 |
| **本实现** | 只失效真正受影响的部分：<br>· span **之前**的消息 → **保留** usage（完全未被触碰）<br>· span **之后**的消息 → 标记失效（其 usage 是在「消息更多」时测得的，已不代表当前投影）<br>· 被 span 覆盖的消息 → 已从投影移除，不参与计量 |
| **理由** | 上下文计量是四级压缩赖以工作的前提——「准确知道还剩多少预算」才能决定该不该压缩。原行为让长会话里计量**永久失准**，且这是静默的：不报错，只是所有后续压缩决策都基于错误的数字。 |
| **影响面** | 投影中 span 之前的 provider usage 保持可用。测试 `test_usage_before_span_is_preserved` 与 `test_stats_do_not_permanently_degrade` 钉住此差异。 |
| **可回退** | 若要严格对等，把 `project_collapsed_view` 里那两处条件分支去掉即可恢复原行为。 |

---

## D4 · `compactConversation` 不再丢弃下标 0 的非 system 消息

| | |
|---|---|
| **原行为** | 用 `messages.slice(1, boundary)` 取待压缩区间，**假定下标 0 是 system 消息**。若消息序列不以 system 开头，第一条非 system 消息会被无声吞掉（既不在压缩输入里，也不在保留区）。 |
| **本实现** | 压缩区间 = 「首个非 system 消息 → 保留区起点」，中间的 system 消息原地保持不动。 |
| **理由** | 这是**数据丢失**：用户的第一句话会凭空消失，且不报错。 |
| **影响面** | 序列以 system 开头时（常见情形）行为一致；否则本实现多保住一条消息。 |

---

## D5 · `compactConversation` 至少保留一轮可压缩前缀

| | |
|---|---|
| **原行为** | 尾部累加 token 到 `MAX_KEEP_TOKENS` 就停。若整段对话都在上限以内，累加会一路走到下标 0，保留区 = 全部消息 → **没有可压缩内容 → auto-compact 静默失效**。 |
| **本实现** | 强制保留区起点不早于第 1 轮（`keep_from_round = max(1, …)`），保证始终存在一段前缀可供压缩。 |
| **理由** | 原行为会让 auto-compact 在「对话不长但模型窗口较小」的模型上完全不起作用，且没有任何提示。这是实现 bug：压缩的触发条件（利用率 ≥85%）与保留上限（40000 token）在小窗口模型上会互相矛盾——例如有效输入 20000 的模型，达到 85% 时总量才 17000 token，永远进不了压缩分支。 |
| **影响面** | 小窗口模型上 auto-compact 现在会真正生效。大窗口模型行为不变（总量超过上限时两种算法结果相同）。 |
| **备注** | 原实现另有 `MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT=20000` 门控来回避这个问题，但那是「跳过压缩」而非「修正压缩」。 |

---

## D6 · 危险命令识别：去掉兜底子串匹配

| | |
|---|---|
| **原行为** | `classifyDangerousCommand` 对 `git` 有一次兜底子串匹配，会把 `git log --hard` 这类无害命令也判为危险。 |
| **本实现** | 只保留精确条件：`reset --hard`、`clean`、`checkout --`、`restore --source`、`push --force`/`-f`。 |
| **理由** | 误报会让用户被无谓地打断；安全收益为零（那条命令本身不危险）。 |
| **影响面** | 误报更少。真正的破坏性命令仍然全部拦下（`test_destructive_git_commands_are_flagged` 覆盖 6 种）。 |

---

## D7 · `isProtectedToolName` 的子串匹配：**保留**（明确记录）

| | |
|---|---|
| **原行为** | 文件编辑保护用子串匹配：名字含 `patch`/`write`/`edit`/`modify` 即受保护。副作用是 `credit_report`（含 `edit`）、`overwrite_file` 也会被保护。 |
| **本实现** | **照搬**。 |
| **理由** | 这是「宁可多保护、不可误删」的取舍。snip 会物理删除消息，误删文件编辑记录的代价远大于多保留几条。 |
| **影响面** | 与原始行为一致。测试 `test_substring_matching_is_intentional` 显式钉住这条副作用，防止后来者「顺手修掉」。 |

---

## D8 · snip 与 collapse 对并行工具批次的相反判定：**保留**（明确记录）

| | |
|---|---|
| **原行为** | 同一批「先连续 N 个 `tool_call`、再连续 N 个 `tool_result`」：<br>· snip 的紧邻配对检测 → 判为未闭合 + 孤儿 → **整批受保护**<br>· collapse 的 `toolGroupIsClosed` 双向集合相等 → 判为闭合 → **可折叠** |
| **本实现** | **两者都照搬**。 |
| **理由** | 这不是 bug，而是两级的风险偏好不同：snip 是**不可逆物理删除**，所以保守；collapse 只是**可撤销投影**，所以可以激进。两级用不同判据是合理的。 |
| **影响面** | 与原始行为一致。`test_parallel_batch_is_fully_protected`（snip）与 `test_parallel_batch_is_closed_here`（collapse）分别钉住。 |

---

## D9 · collapse span 失效：从静默改为可观测

| | |
|---|---|
| **原行为** | span 引用的 messageIds 一旦不连续或缺失（例如随后被 auto-compact 删掉、或内联 snip 后位置变化），`projectSpan` 返回 `None`，该 span **静默**不再投影，原文照常输出。 |
| **本实现** | `project_span` 的失效判定与原来一致（保证正确性），但这是**已知且被记录**的行为，而非意外：`test_span_with_missing_id_is_silently_dropped` 显式覆盖它，并说明触发场景。宿主的 `/status` 应当报告失效 span 数量。 |
| **理由** | 完全复刻这套判定逻辑是对的（放宽会导致投影与转录不一致）。真正的改进空间在**可观测性**——静默降级必须能被发现。 |
| **影响面** | 压缩逻辑行为一致；差别在于有测试与文档指明它，宿主可据此上报。 |
| **待办** | 在 TUI/CLI 的 `/status` 中展示「失效 span 数」。 |

---

## D10 · `Math.round` 与 UTF-16 长度：显式对齐

| | |
|---|---|
| **原行为** | JS `Math.round(50.5) === 51`；Python `round(50.5) === 50`（银行家舍入）。JS `String.length` 是 **UTF-16 码元数**；Python `len(str)` 是码点数（emoji 在 JS 里算 2）。 |
| **本实现** | `js_round()` 与 `utf16_length()` 显式实现 JS 语义。 |
| **理由** | 这两个差异会让 token 估算与边界计算在个别位置漂移，属于「不报错但结果不同」的隐蔽偏差。 |
| **影响面** | 与原实现在边界处一致。`test_js_round_rounds_half_up`、`test_utf16_length_counts_surrogate_pairs_as_two` 钉住。 |
| **备注** | JS `localeCompare` 用 ICU 排序规则，Python 默认按码点比较。在工具 id 这类 ASCII 短串上两者一致，故 `applyToolResultBudget` 的次级排序直接使用 Python 默认比较。 |

---

## D11 · compact 的保留消息不再用相同 uuid 重复写入

| | |
|---|---|
| **原行为** | `appendCompactBoundary` 把 `retainedMessages` 逐条 `wrapEvent` 写入，而这些消息**保留原有 uuid**（此前已写过一遍）。于是同一个 uuid 在文件里出现两行。后果：① 文件里存在重复 uuid；② `loadTranscript` 不做边界切分，**compact 之后的历史在恢复的 transcript 里重复显示一遍**；③ `listSessions` 的 `messageCount` 虚高。 |
| **本实现** | 给重写的保留消息分配新 uuid，并在事件上记录 `rewrittenFrom: <原 uuid>`。 |
| **理由** | 重复 uuid 会让「按 uuid 去重」这条核心不变式失效——而恢复、去重、折叠 span 定位全都依赖它。保留 `rewrittenFrom` 使可追溯性不丢。 |
| **影响面** | transcript 不再重复显示 retained 段；`messageCount` 更接近真实。`loadSession` 行为不变（它本来就只读边界之后）。 |
| **可回退** | 要严格对等就复用原 uuid 并去掉 `rewrittenFrom`。 |

---

## D12 · snip 边界的 `removedMessageIds` 不再使用合成 id

| | |
|---|---|
| **原行为** | snip 生成 `removedMessageIds` 时用 `messageId(m, i) = m.id ?? f"message-{i}"`。被移除的消息**没有 id** 时，贡献的是合成 id `message-<下标>`——它与磁盘上的 uuid **永不相等**。于是回放时匹配不上，整个 `snip_boundary` 事件被永久丢弃。 |
| **本实现** | 保留同样的 `messageId` 语义（因为对**有 id** 的消息它是对的），但新增 `load_session_with_report()`：把「回放时被丢弃的边界」作为第二个返回值报出来。宿主可据此提示「有 N 段历史标记已失效」。 |
| **理由** | 直接改掉合成 id 需要让 `snip` 阶段就能拿到磁盘 uuid，那会污染压缩层与持久化层的边界。更合适的修法是**让丢失可见**——静默降级才是真正的问题。 |
| **影响面** | 恢复结果一致；差别在于丢失事件可被观测。 |
| **待办** | 在 `/status` 中展示失效边界数量。 |

---

## D13 · `clear_session` 必须同时清内存，否则删除会被「复活」

| | |
|---|---|
| **原行为** | `clearSession` 只 `unlink` 文件，**不清理调用方的内存状态**。之后任意一次 `saveSession` 都会因为「文件里已无 uuid」而把**整份历史重新追加回去**：文件重建、父链重排。删除等于没删。 |
| **本实现** | `clear_session(cwd, session_id, on_cleared=None)` 接受一个回调，在删除后立即调用，要求调用方在那里清空内存消息与游标。顺序是**先清内存再判断目录**，避免留下「文件没了但内存还在」的窗口。 |
| **理由** | 这是明确的数据完整性问题：用户执行删除，系统却把数据写回。 |
| **影响面** | 传入回调时删除真正生效。不传回调仍保留原行为（`test_resurrection_when_memory_is_not_purged` 反向钉住了这一点）。 |

---

## D14 · `saveSession` 不再无条件丢弃下标 0

| | |
|---|---|
| **原行为** | 用 `messages.slice(1)` 取待保存区间——**假定下标 0 是 system 消息**。若序列不以 system 开头，第一条消息永远不会落盘。 |
| **本实现** | 改为**跳过所有 system 消息**，并对剩余消息按「非 system 序号」维护位置游标（与原实现的游标语义等价）。 |
| **理由** | 与 D4 同源的数据丢失：第一条消息凭空消失，不报错。 |
| **影响面** | 序列以 system 开头时行为一致；否则本实现多保住一条消息。 |

---

## D15 · fork 保留 snip 边界

| | |
|---|---|
| **原行为** | `forkSession` 只复制 `loadSession` 的结果，而 `loadSession` **已经应用过 snip 回放**——被剪裁的历史本来就不在其中。于是新文件里 `snip_boundary` 的 `removedMessageIds` 一条都匹配不上现存 uuid，回放时该边界被永久丢弃。新会话看起来「凭空少了一段历史」。 |
| **本实现** | 直接复制源文件里**最后一个压缩边界之后的所有事件**（保持行序），因此 snip 边界与被它取代的消息一起过去，回放能正常定位。同时重新线性串父链，避免悬空引用。 |
| **理由** | fork 的语义是「从某个状态继续」，而不是「丢失一部分历史」。 |
| **影响面** | fork 后的新会话能完整重现源会话的消息序列。`test_fork_preserves_snip_boundary` 钉住：新会话报告的「丢失边界」应为空。 |

---

## D16 · 会话 id 路径穿越：从「静默」改为「拒绝」

| | |
|---|---|
| **原行为** | `sessionFilePath` 对 `sessionId` 不做任何净化。`--resume ../../x` 这类输入会读写项目目录之外的位置。 |
| **本实现** | `session_file_path` 额外做一次包含性断言，越界直接抛 `ValueError`。 |
| **理由** | 这个函数理论上只接收内部生成的 id，但「理论上」不足以作为安全边界。加固的代价只有一次 `relpath` 计算。 |
| **影响面** | 合法 id 行为完全不变；非法 id 从「写到别处」变成「明确报错」。 |

---

## D17 · 追加写入时修补缺失的结尾换行

| | |
|---|---|
| **原行为** | 追加时直接 `lines.join('\n') + '\n'`。若历史文件末尾缺少换行，新行会**拼接到旧行尾部**，制造一行无法解析的内容（解析失败后该行被静默跳过）。 |
| **本实现** | 追加前检查文件末字节是否为 `\n`，不是则先补一个。 |
| **理由** | 这是纯粹的健壮性修复，无行为副作用。触发场景真实存在：外部工具截断文件、磁盘写满导致尾部丢失。 |
| **影响面** | 仅在文件末尾异常时生效。`test_missing_trailing_newline_is_repaired` 覆盖。 |

---

| # | 主题 | 类型 | 影响面 |
|---|---|---|---|
| D1 | 零引用常量 | 清理 | 无 |
| D2 | warningLevel 阈值重复定义 | 清理 | 无 |
| D3 | collapse usage 失效范围 | **修正** | 长会话计量不再永久失准 |
| D4 | compact 丢下标 0 | **修正** | 不再丢失首条非 system 消息 |
| D5 | compact 无可压缩前缀 | **修正** | 小窗口模型上 auto-compact 真正生效 |
| D6 | git 危险命令误报 | 修正 | 误报更少 |
| D7 | 编辑工具子串保护 | 保留 | 与原始一致 |
| D8 | 并行批次相反判定 | 保留 | 与原始一致 |
| D9 | span 静默失效 | 保留 + 可观测 | 逻辑一致，新增可观测性要求 |
| D10 | JS/Python 数值差异 | 对齐 | 与原始一致 |
| D11 | compact retained 重复 uuid | **修正** | transcript 不再重复显示 |
| D12 | snip 合成 id 导致边界丢失 | 保留 + 可观测 | 恢复一致，丢失可被报到 |
| D13 | clearSession 复活 | **修正** | 删除真正生效 |
| D14 | saveSession 丢下标 0 | **修正** | 不再丢失首条消息 |
| D15 | fork 丢 snip 边界 | **修正** | fork 后历史完整 |
| D16 | sessionId 路径穿越 | 加固 | 非法 id 明确报错 |
| D17 | 追加时缺结尾换行 | 加固 | 不再制造不可解析行 |

**一条贯穿全部修正的原则**：凡是会**静默**造成数据丢失或计量失真的地方就修；
凡是属于行为契约（阈值、护栏、排序、占位模板）或**明显有意的取舍**
（多保护、双判据、有损目录名）就保留，并用测试把「这是有意的」钉住。
