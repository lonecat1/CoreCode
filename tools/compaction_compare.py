"""压缩层的**判定对照**：同一批合成对话，两边给出的阈值判定必须一致。

## 为什么单独做这个

前面几轮的端到端对照跑的是「真实交互」，覆盖的是命令层、工具层、权限层。
压缩层不一样：它的触发点由**阈值**决定（50% / 70% / 85% / 95%），而阈值是
最容易「差不多就行」的地方——`>=` 写成 `>`、`0.85` 写成 `0.84`，日常使用
几乎看不出来，但行为在边界上完全不同。

所以这里不走 REPL，而是**直接调两边的纯函数**，喂同一批合成对话，比对：

    estimateMessagesTokens        token 估算
    computeContextStats           利用率 / warningLevel / 有效输入
    shouldAutoCompact             85% 门槛
    snipCompactConversation       70% 门槛 + 候选区间的选择
    microcompact                  50% 门槛 + 清空哪些消息

## 为什么这些函数可以被直接调

它们都**不调模型**：

  - `computeContextStats` 是纯算术；
  - `shouldAutoCompact` 只看阈值；
  - `snipCompactConversation` 的边界文案是**本地合成**的（removedCount /
    tokensFreed 拼出来），不需要摘要；
  - `microcompact` 只是把旧工具结果的正文替换成固定标记。

（原版签名也是这样：`snipCompactConversation({messages, contextStats,
modelContextWindow})`，没有 model adapter 参数。）

## 怎么跑

TS 侧用 `tsx --eval` 直接 import 源模块，**不在 TS 仓库里留任何文件**；
Python 侧 import 自己的模块。两边都输出 JSON，最后逐字段比对。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TS_ROOT = Path(r"D:\CoreCode\MiniCode")
TSX_CLI = TS_ROOT / "node_modules" / "tsx" / "dist" / "cli.mjs"
PY = sys.executable

#: 模型：`claude-sonnet-4` 的窗口是 200000 / 预留 16000 → 有效输入 184000。
#: 用它是因为两边都对得上，且阈值算起来是整数好读。
MODEL = "claude-sonnet-4"

#: 合成对话的用例：`(名字, 每条字符数, 消息条数, tool_result 占比)`。
#:
#: 用例设计受三条**结构条件**约束——不知道它们会得到一堆「什么都没触发」
#: 的假通过（第一版就是这样）：
#:
#:   1. **snip** 需要 `条数 > SNIP_KEEP_RECENT_MESSAGES(12)
#:      + SNIP_MIN_MESSAGES_TO_REMOVE(6)` = 至少 19 条，且要过 0.70 且
#:      至少省下 `SNIP_MIN_TOKENS_TO_FREE(2000)` 个 token。
#:   2. **microcompact** 需要 `tool_result` **多于 `KEEP_RECENT_TOOL_RESULTS(3)`**
#:      ——它保留最近 3 条，只清更早的。
#:   3. **auto-compact** 在 0.85，**warningLevel** 的 `blocked` 在 0.95。
#:
#: 所以既有「单纯很长但条数少」的用例（验证阈值边界），也有「条数够多、
#: 工具结果够多」的用例（验证候选区间与清空行为）。
CASES: list[tuple[str, int, int, float]] = [
    # --- 阈值边界：条数很少，只验数值口径 ---
    ("tiny", 100, 2, 0.0),
    ("quarter", 80_000, 2, 0.0),
    ("half_edge", 138_000, 2, 0.0),
    ("half_over", 145_000, 2, 0.0),
    ("snip_edge", 193_000, 2, 0.0),
    ("snip_over", 210_000, 2, 0.0),
    ("autocompact_edge", 235_000, 2, 0.0),
    ("autocompact_over", 260_000, 2, 0.0),
    ("blocked", 560_000, 2, 0.0),
    # --- 够多消息：snip 的候选区间才可能成立 ---
    ("many_19_small", 12_000, 19, 0.0),   # 条数够、token 不够 → below_threshold
    ("many_19_big", 40_000, 19, 0.0),     # 条数够、也过 0.70 → 应该剪
    ("many_30_big", 40_000, 30, 0.0),
    ("many_30_huge", 90_000, 30, 0.0),
    # --- 工具结果够多：microcompact 才会清 ---
    ("micro_4t", 20_000, 12, 0.5),        # 6 条 tool_result > 3
    ("micro_8t", 25_000, 20, 0.5),
    ("micro_8t_big", 60_000, 20, 0.5),
    # 混合：既能剪也能清
    ("mixed_30", 45_000, 30, 0.4),
]


def build_messages(
    per_message_chars: int, count: int, tool_ratio: float
) -> list[dict]:
    """造一段可预测的对话。

    `tool_ratio` 是 `tool_result` 的目标占比：0 表示纯 user/assistant 交替，
    0.5 表示每两条里有一条工具结果。工具结果的 `toolName` 用 `read_file`——
    它在 `COMPACTABLE_TOOLS` 里，`microcompact` 才会考虑清它。
    """
    messages: list[dict] = []
    tool_every = 0 if tool_ratio <= 0 else max(2, round(1 / tool_ratio))
    for index in range(count):
        is_tool = tool_every > 0 and index % tool_every == tool_every - 1
        content = f"[{index}] " + ("x" * per_message_chars)
        if is_tool:
            messages.append(
                {
                    "role": "tool_result",
                    "content": content,
                    "id": f"m{index}",
                    "toolUseId": f"t{index}",
                    "toolName": "read_file",
                    "isError": False,
                }
            )
        else:
            messages.append(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": content,
                    "id": f"m{index}",
                }
            )
    return messages


def cases_json() -> str:
    return json.dumps(
        [
            {"name": name, "messages": build_messages(chars, count, ratio)}
            for name, chars, count, ratio in CASES
        ],
        ensure_ascii=False,
    )


TS_PROBE = r"""
import { readFileSync } from "node:fs";
import { computeContextStats, estimateMessagesTokens }
  from "file:///D:/CoreCode/MiniCode/src/utils/token-estimator.ts";
import { shouldAutoCompact }
  from "file:///D:/CoreCode/MiniCode/src/compact/auto-compact.ts";
import { snipCompactConversation }
  from "file:///D:/CoreCode/MiniCode/src/compact/snipCompact.ts";
import { microcompact }
  from "file:///D:/CoreCode/MiniCode/src/compact/microcompact.ts";
import { getModelContextWindow }
  from "file:///D:/CoreCode/MiniCode/src/utils/model-context.ts";

const cases = JSON.parse(readFileSync(process.env.CASES_FILE, "utf8"));
const model = process.env.CASES_MODEL;

// `tsx --eval` 按 CJS 输出，**不支持顶层 await**，所以包一层 async IIFE。
(async () => {
  const out = [];
  for (const c of cases) {
    const stats = computeContextStats(c.messages, model);
    const window = getModelContextWindow(model);
    const snip = await snipCompactConversation({
      messages: c.messages,
      contextStats: stats,
      modelContextWindow: window.effectiveInput,
    });
    const afterMicro = microcompact(c.messages, model);
    out.push({
      name: c.name,
      estimate: estimateMessagesTokens(c.messages),
      total: stats.accounting.totalTokens,
      effective: stats.effectiveInput,
      utilization: Number(stats.utilization.toFixed(6)),
      level: stats.warningLevel,
      source: stats.accounting.source,
      shouldAutoCompact: shouldAutoCompact(c.messages, model),
      snip: {
        didSnip: snip.didSnip ?? false,
        reason: snip.reason ?? null,
        tokensFreed: snip.tokensFreed ?? 0,
        counts: (snip.messages ?? c.messages).length,
      },
      micro: {
        changed: afterMicro !== c.messages,
        counts: afterMicro.length,
        tokens: estimateMessagesTokens(afterMicro),
      },
    });
  }
  console.log(JSON.stringify(out));
})();
"""


def ts_side(cases_file: str) -> list[dict]:
    env = {**os.environ, "CASES_FILE": cases_file, "CASES_MODEL": MODEL}
    proc = subprocess.run(
        ["node", str(TSX_CLI), "--eval", TS_PROBE],
        cwd=str(TS_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=300,
    )
    if proc.returncode != 0:
        raise SystemExit(f"TS 侧失败：\n{proc.stdout}\n{proc.stderr}")
    # `tsx` 可能把别的东西打到 stdout，只取最后一个 JSON 行
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.startswith("["):
            return json.loads(line)
    raise SystemExit(f"TS 侧没有输出 JSON：{proc.stdout!r}")


def py_side(cases_file: str) -> list[dict]:
    sys.path.insert(0, str(REPO / "src"))
    from minicode.context.compact import should_auto_compact
    from minicode.context.microcompact import microcompact
    from minicode.context.model_context import get_model_context_window
    from minicode.context.snip import snip_compact_conversation
    from minicode.context.tokens import compute_context_stats, estimate_messages_tokens

    cases = json.loads(Path(cases_file).read_text(encoding="utf-8"))
    out = []
    for case in cases:
        messages = case["messages"]
        stats = compute_context_stats(messages, MODEL)
        window = get_model_context_window(MODEL)
        snip = snip_compact_conversation(
            messages, stats, window.effective_input
        )
        after_micro = microcompact(messages, MODEL)
        out.append(
            {
                "name": case["name"],
                "estimate": estimate_messages_tokens(messages),
                "total": stats.accounting.total_tokens,
                "effective": stats.effective_input,
                "utilization": round(stats.utilization, 6),
                "level": stats.warning_level,
                "source": stats.accounting.source,
                "shouldAutoCompact": should_auto_compact(messages, MODEL),
                "snip": {
                    "didSnip": snip.did_snip,
                    "reason": snip.reason,
                    "tokensFreed": snip.tokens_freed,
                    "counts": len(snip.messages or messages),
                },
                "micro": {
                    "changed": after_micro is not messages,
                    "counts": len(after_micro),
                    "tokens": estimate_messages_tokens(after_micro),
                },
            }
        )
    return out


def main() -> int:
    handle, cases_file = tempfile.mkstemp(suffix=".json", prefix="compact-cases-")
    os.close(handle)
    try:
        Path(cases_file).write_text(cases_json(), encoding="utf-8")
        ts = ts_side(cases_file)
        py = py_side(cases_file)
    finally:
        try:
            os.remove(cases_file)
        except OSError:
            pass

    by_ts = {row["name"]: row for row in ts}
    by_py = {row["name"]: row for row in py}

    failures: list[str] = []
    print(f"{'用例':<20} {'利用率':>8} {'级别':<9} {'auto':<6} {'snip':<18} {'micro':<10}")
    print("-" * 78)
    for name, _chars, _count, _ratio in CASES:
        a = by_ts.get(name)
        b = by_py.get(name)
        if a is None or b is None:
            failures.append(f"{name}（一侧缺失）")
            continue

        diffs = [
            key for key in ("estimate", "total", "effective", "utilization",
                            "level", "source", "shouldAutoCompact")
            if a[key] != b[key]
        ]
        if a["snip"] != b["snip"]:
            diffs.append("snip")
        if a["micro"] != b["micro"]:
            diffs.append("micro")

        mark = "OK  " if not diffs else "DIFF"
        snip_text = (
            f"{'剪掉' if a['snip']['didSnip'] else '未剪'}"
            f"({a['snip']['reason'] or '-'})"
        )
        micro_text = "改" if a["micro"]["changed"] else "未改"
        print(
            f"{mark} {name:<16} {a['utilization']:>8.4f} {a['level']:<9} "
            f"{str(a['shouldAutoCompact']):<6} {snip_text:<18} {micro_text:<10}"
        )
        if diffs:
            failures.append(name)
            for key in diffs:
                print(f"       {key}:  TS={a[key]!r}")
                print(f"       {' ' * len(key)}  PY={b[key]!r}")

    print()
    print(f"{len(CASES) - len(failures)}/{len(CASES)} 个用例完全一致")
    if failures:
        print(f"不一致：{failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
