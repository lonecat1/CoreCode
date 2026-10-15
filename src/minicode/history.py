"""输入历史 —— 跨会话保留用户敲过的输入。

对应原版 `MiniCode/src/history.ts`（71 行）。

## 它与终端自己的历史是两回事

原版把历史存在 `<MINICODE_HOME>/history.jsonl`，用途是 TUI 里的上下键补全
（`tty-app.ts` 的 `historyUp` / `historyDown`）。

行模式 REPL 里，上下键由**终端自己**负责（readline），本模块不参与那个。
它仍然有价值的地方是**跨会话**：

  - 终端历史是每个 shell 一份、且不区分项目。你在 A 项目敲的命令，
    在 B 项目的终端里按上键也会冒出来。
  - 这里按**项目**记录，并带上会话 id 与时间戳，因此可以回答
    「我上个星期在这个项目里敲过什么」。

## 一条容易忽略的性质

原版**只追加不重复**：`saveHistoryEntries` 先读回已有条目，过滤掉已经出现过的，
没有新条目就**直接返回、不碰文件**（`if (newEntries.length === 0) return`）。

少了这个判断，每次退出都会把整份历史再追加一遍，文件会成倍增长。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .config import history_path, minicode_home

__all__ = [
    "MAX_ENTRIES",
    "HistoryEntry",
    "append_history",
    "clear_history",
    "load_history",
    "load_history_entries",
    "save_history_entries",
]

#: 历史文件最多保留多少条。超出时**从头部丢弃**（保留最近的）。
MAX_ENTRIES = 500


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """一条历史记录。

    `project` 与 `session_id` 让历史能按项目/会话过滤——原版存了它们但没用到，
    本实现保留同样的字段以便将来能按项目筛选，也便于逐字段与原版对齐。
    """

    display: str
    timestamp: float
    project: str = ""
    session_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "timestamp": self.timestamp,
            "project": self.project,
            "sessionId": self.session_id,
        }


def _read_lines(path: Any | None = None) -> list[str]:
    """读原始行。文件不存在或读不了都返回空列表——历史坏掉不该让程序起不来。"""
    try:
        with open(path or history_path(), encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [line for line in raw.strip().split("\n") if line]


def _parse_lines(lines: Iterable[str]) -> list[HistoryEntry]:
    """把原始行解析成条目。**坏行跳过而不是整体失败。**

    历史是追加写的，一次断电就可能留下半行。让整个文件读不出来，代价是用户
    丢掉全部历史——而坏行只有一行。
    """
    entries: list[HistoryEntry] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        display = parsed.get("display")
        if not isinstance(display, str):
            continue
        timestamp = parsed.get("timestamp")
        entries.append(
            HistoryEntry(
                display=display,
                timestamp=float(timestamp)
                if isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                else 0.0,
                project=str(parsed.get("project") or ""),
                session_id=str(parsed.get("sessionId") or ""),
            )
        )
    return entries


def load_history_entries(path: Any | None = None) -> list[HistoryEntry]:
    """读回全部历史条目。"""
    return _parse_lines(_read_lines(path))


def load_history(path: Any | None = None) -> list[str]:
    """只要显示文本，对应原版 `loadHistoryEntries()` 的返回类型。"""
    return [entry.display for entry in load_history_entries(path)]


def save_history_entries(
    entries: Iterable[str],
    cwd: str,
    session_id: str,
    path: Any | None = None,
    *,
    now: float | None = None,
) -> int:
    """把新条目追加到历史文件。返回**实际追加**的条数。

    ## 只追加不重复

    已有的显示文本会被过滤掉。**没有新条目时直接返回，不碰文件**——否则每次
    退出都会把整份历史再写一遍，文件成倍增长。

    ## 截断

    追加后如果超过 `MAX_ENTRIES`，保留**最后** MAX_ENTRIES 行。保留最近的，
    因为「刚才敲过什么」比「上个月敲过什么」有用。

    ## 只读一次

    去重需要读回已有条目，截断也需要知道总行数。**同一次读取同时服务两件事**
    ——分别读的话，逐条追加的场景（REPL 每次提交都调一次）会退化成 O(n²)，
    500 条上限时慢到能感觉出来。
    """
    target = path or history_path()
    existing_lines = _read_lines(target)
    existing = {entry.display for entry in _parse_lines(existing_lines)}

    new_entries: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        text = str(entry)
        # 既要排除文件里已有的，也要排除这一批里重复的。
        if not text or text in existing or text in seen:
            continue
        seen.add(text)
        new_entries.append(text)

    if not new_entries:
        return 0

    stamp = time.time() if now is None else now
    lines = [
        json.dumps(
            HistoryEntry(
                display=text, timestamp=stamp, project=cwd, session_id=session_id
            ).to_json(),
            ensure_ascii=False,
        )
        for text in new_entries
    ]

    parent = os.path.dirname(str(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    _trim(target, existing_lines + lines)
    return len(new_entries)


def _trim(path: Any, lines: list[str]) -> None:
    """超过上限时保留最后 MAX_ENTRIES 行。

    行数由调用方传入（它已经读过一次），避免为了截断再读一遍整个文件。
    **失败不影响追加结果**——截断是清理，不是功能。
    """
    if len(lines) <= MAX_ENTRIES:
        return
    try:
        kept = lines[-MAX_ENTRIES:]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(kept) + "\n")
    except OSError:
        # 截断失败不该让「追加成功」这件事变成失败。
        return


def append_history(
    text: str, cwd: str, session_id: str, path: Any | None = None
) -> int:
    """追加**一条**输入。REPL 每次提交时调它。"""
    return save_history_entries([text], cwd, session_id, path)


def clear_history(path: Any | None = None) -> None:
    """清空历史文件。"""
    target = path or history_path()
    try:
        os.remove(target)
    except FileNotFoundError:
        return


def history_directory() -> str:
    """历史文件所在目录（供调用方确保它存在）。"""
    return str(minicode_home())
