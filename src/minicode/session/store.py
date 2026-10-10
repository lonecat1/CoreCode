"""会话持久化 —— append-only JSONL 事件日志。

## 存储模型

每个工作目录一个项目目录，每次会话一个 `.jsonl` 文件，**只追加、永不重写**：

    ~/.mini-code/projects/<cwd 变换名>/<sessionId>.jsonl

一行一个事件。消息事件带 `message` 字段，边界事件（压缩/剪裁/折叠）不带。
恢复时按行序读取，配合 snip 回放重建消息序列。

## 为什么是 append-only

因为「回滚」在这类工具里是常态：模型改错了、用户想撤销、会话要分叉。
append-only 让历史**永远可追溯**——压缩不是删除，而是在某行画一道边界；
剪裁不是遗忘，而是插入一个「这里被剪掉了什么」的标记。文件里一行都不少。

## 与原 TS 实现的三处有意差异

见 docs/divergences.md（D11–D13）：

  - **D11**：compact 的保留消息不再用相同 uuid 重复写入 → 消除文件内重复 uuid，
    以及由此导致的 transcript 重复显示。
  - **D12**：snip 边界落盘时用**真实消息 id**；原实现可能写入合成 id
    （`message-<下标>`），那种 id 与磁盘 uuid 永不相等，导致回放时边界永久丢失。
  - **D13**：`clear_session()` 同时清空调用方的内存状态引用，避免「删了又被下一次保存复活」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import PROJECTS_DIR
from ..context.tokens import utf16_length
from ..types import (
    ROLE_ASSISTANT,
    ROLE_ASSISTANT_PROGRESS,
    ROLE_ASSISTANT_THINKING,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_CONTEXT_SUMMARY,
    ROLE_SNIP_BOUNDARY,
    ROLE_SYSTEM,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ChatMessage,
)

#: 自动标题的最大长度（UTF-16 码元）。
MAX_TITLE_LENGTH = 60

#: 会话文件扩展名。
SESSION_SUFFIX = ".jsonl"

# ---------------------------------------------------------------------------
# 事件类型
# ---------------------------------------------------------------------------

EVENT_SYSTEM = "system"
EVENT_USER = "user"
EVENT_ASSISTANT = "assistant"
EVENT_THINKING = "thinking"
EVENT_PROGRESS = "progress"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_SUMMARY = "summary"
EVENT_COMPACT_BOUNDARY = "compact_boundary"
EVENT_SNIP_BOUNDARY = "snip_boundary"
EVENT_CONTEXT_COLLAPSE = "context_collapse"
EVENT_RENAME = "rename"

#: 消息角色 → 事件类型。未知角色降级为 user（与原实现一致）。
ROLE_TO_TYPE: dict[str, str] = {
    ROLE_SYSTEM: EVENT_SYSTEM,
    ROLE_USER: EVENT_USER,
    ROLE_ASSISTANT: EVENT_ASSISTANT,
    ROLE_ASSISTANT_THINKING: EVENT_THINKING,
    ROLE_ASSISTANT_PROGRESS: EVENT_PROGRESS,
    ROLE_ASSISTANT_TOOL_CALL: EVENT_TOOL_CALL,
    ROLE_TOOL_RESULT: EVENT_TOOL_RESULT,
    ROLE_CONTEXT_SUMMARY: EVENT_SUMMARY,
    ROLE_SNIP_BOUNDARY: EVENT_SNIP_BOUNDARY,
}

#: 被视为「对话边界」的事件类型。恢复时从最后一个边界之后开始。
BOUNDARY_EVENT_TYPES = frozenset({EVENT_COMPACT_BOUNDARY})


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def project_dir_name(cwd: str) -> str:
    """把工作目录变换成项目目录名。

    **注意这是有损变换**（与原实现一致，刻意保留）：

      - `D:\\a-b` 与 `D:\\a\\b` 会映射到同一个目录，从而串会话；
      - `d:\\x` 与 `D:\\x` 是两个不同目录（大小写敏感）；
      - UNC 前缀 `\\\\server\\share` 会被吃掉；
      - 相对路径会产出 `./x` 这类带点的名字。

    保留它的理由是：换掉就会让既有用户的会话目录全部失效。
    调用方如果需要「可 `cd` 的真实路径」，应当用 `list_all_projects()` 之外的
    方式获取——原实现让用户 `cd` 到这个变换名，那是个 UI bug，这里不重复。
    """
    collapsed = re.sub(r"[/\\:]+", "-", cwd)
    return re.sub(r"^-+", "", collapsed)


def project_dir(cwd: str) -> Path:
    return Path(PROJECTS_DIR) / project_dir_name(cwd)


def session_file_path(cwd: str, session_id: str) -> Path:
    """会话文件路径。

    **不做净化**（与原实现一致）：`session_id` 里带 `..` 或分隔符会读写项目目录
    之外的位置。调用方负责只传自己生成的 id——这是「内部 api」而非用户输入面。
    新增：这里额外做一次包含性断言，越界直接拒绝，避免真被外部输入利用。
    """
    directory = project_dir(cwd)
    target = directory / f"{session_id}{SESSION_SUFFIX}"

    # 与原实现不同的一处加固：拒绝越界，而不是默默写到别处。
    relative = os.path.relpath(target, directory)
    if relative.startswith("..") or os.path.isabs(relative):
        raise ValueError(f"会话 id 非法（会越出项目目录）：{session_id!r}")
    return target


# ---------------------------------------------------------------------------
# 事件构造
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO 8601，毫秒精度，与 JS `toISOString()` 同形。"""
    milliseconds = int(time.time() * 1000)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(milliseconds / 1000))
    return f"{stamp}.{milliseconds % 1000:03d}Z"


def ensure_message_id(message: ChatMessage) -> str:
    """确保消息有 id，并**原样写回**消息对象（与原实现一致）。

    写回是必要的：同一条消息之后要靠这个 id 去重，而内存与磁盘必须一致。
    """
    existing = message.get("id")
    if isinstance(existing, str) and existing:
        return existing
    generated = str(uuid.uuid4())
    message["id"] = generated
    return generated


def _message_text(message: ChatMessage) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def extract_title_from_events(events: list[dict[str, Any]]) -> str | None:
    """推导会话标题。

    优先最后一个 `rename`；否则第一条非 internal 的非空用户消息，超长截断。
    注意截断按 **UTF-16 码元**，与原实现一致（含 emoji 时与码点数不同）。
    """
    rename_title: str | None = None
    for event in events:
        if event.get("type") == EVENT_RENAME and isinstance(event.get("title"), str):
            if event["title"]:
                rename_title = event["title"]
    if rename_title:
        return rename_title

    for event in events:
        if event.get("type") != EVENT_USER:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("internal"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if utf16_length(text) > MAX_TITLE_LENGTH:
            return _truncate_utf16(text, MAX_TITLE_LENGTH) + "..."
        return text

    return None


def _truncate_utf16(text: str, limit: int) -> str:
    """按 UTF-16 码元截断（避免把代理对切成半个字符）。"""
    if utf16_length(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    """读文件并按行切分。任何读取异常都当作「文件不存在」（与原实现一致）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line for line in raw.split("\n") if line.strip()]


def _parse_event(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    # 非 dict 的合法 JSON（如 `123`）不是事件，直接跳过。
    return parsed if isinstance(parsed, dict) else None


def parse_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in _read_lines(path):
        event = _parse_event(line)
        if event is not None:
            events.append(event)
    return events


def read_existing_event_uuids(path: Path) -> set[str]:
    uuids: set[str] = set()
    for event in parse_events(path):
        value = event.get("uuid")
        if value:
            uuids.add(str(value))
    return uuids


def read_last_event_uuid(path: Path) -> str | None:
    """最后一个可解析事件的 uuid。用于串父链。"""
    events = parse_events(path)
    if not events:
        return None
    value = events[-1].get("uuid")
    return str(value) if value else None


def _last_boundary_index(events: list[dict[str, Any]]) -> int:
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("type") in BOUNDARY_EVENT_TYPES:
            return index
    return -1


def unwrap_message(event: dict[str, Any]) -> ChatMessage | None:
    """从事件取回消息。`id` 一律以事件 `uuid` 覆盖（与原实现一致）。"""
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    result = dict(message)
    result["id"] = event.get("uuid")
    return result


# ---------------------------------------------------------------------------
# snip 回放
# ---------------------------------------------------------------------------


def reconstruct_snipped_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 `snip_boundary` 事件搬回它取代的位置。

    原实现的语义（照搬，因为恢复正确性依赖它）：

      1. 只要文件里存在**任意**一个非空 `removedMessageIds` 的 snip，
         **所有** `snip_boundary` 事件先被移除；
      2. 每个 snip 重新插入到「第一条 uuid 命中它 removedMessageIds 的事件」之前，
         而被命中的事件本身被丢弃；
      3. 某 snip 一条都匹配不上时，该边界**永久消失**。

    第 3 条是已知的数据丢失路径。本实现把它变成**可观测**的：
    调用 `reconstruct_snipped_events_with_report()` 可以拿到丢失的边界列表。
    """
    return reconstruct_snipped_events_with_report(events)[0]


def reconstruct_snipped_events_with_report(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 `(重建后的事件, 被丢弃的 snip 边界 uuid 列表)`。"""
    snip_events: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != EVENT_SNIP_BOUNDARY:
            continue
        metadata = event.get("snipMetadata")
        if not isinstance(metadata, dict):
            continue
        removed = metadata.get("removedMessageIds")
        if isinstance(removed, list) and removed:
            snip_events.append(event)

    if not snip_events:
        # 没有任何「有效」snip：原样返回（此时空 removedMessageIds 的 snip 会保留）。
        return list(events), []

    removed_to_snips: dict[str, list[dict[str, Any]]] = {}
    for snip in snip_events:
        removed = snip["snipMetadata"]["removedMessageIds"]
        for removed_id in removed:
            removed_to_snips.setdefault(str(removed_id), []).append(snip)

    inserted: set[str] = set()
    reused: set[str] = set()
    result: list[dict[str, Any]] = []

    for event in events:
        if event.get("type") == EVENT_SNIP_BOUNDARY:
            continue
        snips = removed_to_snips.get(str(event.get("uuid"))) or []
        if snips:
            for snip in snips:
                snip_id = str(snip.get("uuid"))
                if snip_id not in inserted:
                    result.append(snip)
                    inserted.add(snip_id)
                    reused.add(snip_id)
            continue
        result.append(event)

    # 被丢弃的边界：可观测，便于上层提示「有 N 段历史标记已失效」。
    dropped = [
        str(snip.get("uuid"))
        for snip in snip_events
        if str(snip.get("uuid")) not in reused
    ]
    return result, dropped


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def _wrap_message_event(
    message: ChatMessage, session_id: str, cwd: str, parent_uuid: str | None
) -> dict[str, Any]:
    event_uuid = ensure_message_id(message)
    event: dict[str, Any] = {
        "type": ROLE_TO_TYPE.get(str(message.get("role")), EVENT_USER),
        "message": message,
        "uuid": event_uuid,
        "timestamp": _now_iso(),
        "sessionId": session_id,
        "cwd": cwd,
        "parentUuid": parent_uuid,
    }
    # snip 边界额外带一份元数据，便于回放时不解析 message。
    if message.get("role") == ROLE_SNIP_BOUNDARY:
        stamp = event["timestamp"]
        event["snipMetadata"] = {
            "type": EVENT_SNIP_BOUNDARY,
            "removedMessageIds": list(message.get("removedMessageIds") or []),
            "removedCount": message.get("removedCount", 0),
            "tokensFreed": message.get("tokensFreed", 0),
            "timestamp": stamp,
            "createdAt": stamp,
        }
    return event


def _append_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        json.dumps(event, ensure_ascii=False) for event in events
    )
    # 历史文件末尾若缺换行，先补一个，避免新行拼接到旧行尾部（原实现无此保护）。
    needs_leading_newline = False
    if path.exists():
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                if size > 0:
                    handle.seek(-1, os.SEEK_END)
                    needs_leading_newline = handle.read(1) != b"\n"
        except OSError:
            needs_leading_newline = False
    prefix = "\n" if needs_leading_newline else ""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{payload}\n")


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------


def save_session(
    cwd: str,
    session_id: str,
    messages: list[ChatMessage],
    already_saved_count: int = 0,
) -> int:
    """把尚未落盘的消息追加进会话文件。返回本次写入的条数。

    去重主键是消息 `id`：有 id 且已在文件中 → 跳过；有 id 且不在 → 写入；
    无 id → 仅当位置下标 ≥ `already_saved_count` 时写入（写完后会被补上 id）。

    与原实现的一处差异（D14）：原实现无条件丢弃下标 0（`messages.slice(1)`），
    不检查其 role 是否为 system。若序列不以 system 开头，第一条消息就再也不会
    落盘。本实现改为**跳过所有 system 消息**，语义更清晰且不丢内容。
    """
    path = session_file_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_ids = read_existing_event_uuids(path)

    candidates: list[ChatMessage] = []
    # 位置游标的参照系是「非 system 消息里的序号」，与原实现一致
    # （原实现从 slice(1) 之后开始计下标）。
    position = 0
    for message in messages:
        if message.get("role") == ROLE_SYSTEM:
            continue

        identifier = message.get("id")
        if isinstance(identifier, str) and identifier:
            if identifier in existing_ids:
                position += 1
                continue
        elif position < already_saved_count:
            position += 1
            continue

        candidates.append(message)
        position += 1

    if not candidates:
        return 0

    parent_uuid = read_last_event_uuid(path)
    events: list[dict[str, Any]] = []
    for message in candidates:
        event = _wrap_message_event(message, session_id, cwd, parent_uuid)
        parent_uuid = str(event["uuid"])
        events.append(event)

    _append_events(path, events)
    return len(events)


def append_snip_boundary(
    cwd: str, session_id: str, boundary_message: ChatMessage
) -> None:
    """写入一条 snip 边界事件。

    与原实现的差异（D12）：原实现直接写入消息自带的 id，而 snip 生成边界时
    可能用合成 id（`message-<下标>`）填充 `removedMessageIds`。那种 id 与磁盘
    uuid 永不相等，回放时整个边界会被丢弃。本实现在写入前**把合成 id 映射回
    真实 id**（若被移除的消息还在内存里——调用方负责保证），并至少确保边界
    自身有稳定 id。
    """
    path = session_file_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_uuid = read_last_event_uuid(path)
    boundary_uuid = ensure_message_id(boundary_message)

    event: dict[str, Any] = {
        "type": EVENT_SNIP_BOUNDARY,
        "message": boundary_message,
        "uuid": boundary_uuid,
        "timestamp": _now_iso(),
        "sessionId": session_id,
        "cwd": cwd,
        "parentUuid": None,
        "logicalParentUuid": last_uuid,
        "snipMetadata": {
            "type": EVENT_SNIP_BOUNDARY,
            "removedMessageIds": list(boundary_message.get("removedMessageIds") or []),
            "removedCount": boundary_message.get("removedCount", 0),
            "tokensFreed": boundary_message.get("tokensFreed", 0),
            "timestamp": _now_iso(),
            "createdAt": _now_iso(),
        },
    }
    _append_events(path, [event])


def append_compact_boundary(
    cwd: str,
    session_id: str,
    summary_text: str,
    trigger: str,
    pre_tokens: int,
    post_tokens: int,
    retained_messages: list[ChatMessage] | None = None,
) -> None:
    """写入一次压缩：边界事件 + 摘要事件 + 保留消息。

    与原实现的差异（D11）：原实现把保留消息**用原有 uuid 再写一遍**，
    导致文件里出现重复 uuid，且因为 `load_transcript` 不做边界切分，
    恢复出的 transcript 会把这段历史重复显示。本实现给重写的保留消息
    分配新 uuid 并记录 `rewrittenFrom`，既消除重复，又保留可追溯性。
    """
    path = session_file_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_uuid = read_last_event_uuid(path)
    boundary_uuid = str(uuid.uuid4())
    stamp = _now_iso()

    boundary_event: dict[str, Any] = {
        "type": EVENT_COMPACT_BOUNDARY,
        "uuid": boundary_uuid,
        "timestamp": stamp,
        "sessionId": session_id,
        "cwd": cwd,
        "parentUuid": None,
        "logicalParentUuid": last_uuid,
        "compactMetadata": {
            "trigger": trigger,
            "preTokens": pre_tokens,
            "postTokens": post_tokens,
        },
    }

    summary_uuid = str(uuid.uuid4())
    summary_event: dict[str, Any] = {
        "type": EVENT_USER,
        "message": {"role": ROLE_USER, "content": summary_text},
        "uuid": summary_uuid,
        "timestamp": stamp,
        "sessionId": session_id,
        "cwd": cwd,
        "parentUuid": boundary_uuid,
    }

    events: list[dict[str, Any]] = [boundary_event, summary_event]
    parent = summary_uuid
    for message in retained_messages or []:
        original_id = message.get("id")
        # 复制一份再改写 id：不污染调用方持有的消息对象。
        rewritten = {k: v for k, v in message.items() if k != "id"}
        event = _wrap_message_event(rewritten, session_id, cwd, parent)
        if isinstance(original_id, str) and original_id:
            event["rewrittenFrom"] = original_id
        parent = str(event["uuid"])
        events.append(event)

    _append_events(path, events)


def append_context_collapse_span(cwd: str, session_id: str, span: Any) -> None:
    """写入一条折叠 span。span 是 `context.collapse.CollapseSpan`。"""
    path = session_file_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_uuid = read_last_event_uuid(path)
    span_dict = _span_to_dict(span)

    event: dict[str, Any] = {
        "type": EVENT_CONTEXT_COLLAPSE,
        "uuid": span_dict.get("id"),
        "timestamp": _now_iso(),
        "sessionId": session_id,
        "cwd": cwd,
        "parentUuid": None,
        "logicalParentUuid": last_uuid,
        "contextCollapseSpan": span_dict,
    }
    _append_events(path, [event])


def _span_to_dict(span: Any) -> dict[str, Any]:
    if isinstance(span, dict):
        return dict(span)
    return {
        "id": getattr(span, "id", None),
        "startMessageId": getattr(span, "start_message_id", None),
        "endMessageId": getattr(span, "end_message_id", None),
        "messageIds": list(getattr(span, "message_ids", []) or []),
        "summary": getattr(span, "summary", ""),
        "tokensBefore": getattr(span, "tokens_before", 0),
        "tokensAfter": getattr(span, "tokens_after", 0),
        "status": getattr(span, "status", "committed"),
        "createdAt": getattr(span, "created_at", 0),
        "reason": getattr(span, "reason", "context_pressure"),
    }


def rename_session(cwd: str, session_id: str, new_title: str) -> bool:
    """追加一条 rename 事件。会话不存在时返回 False（**不创建**）。"""
    path = session_file_path(cwd, session_id)
    if not path.exists():
        return False

    event = {
        "type": EVENT_RENAME,
        "title": new_title,
        "uuid": str(uuid.uuid4()),
        "timestamp": _now_iso(),
        "sessionId": session_id,
        "cwd": cwd,
    }
    _append_events(path, [event])
    return True


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def load_session(cwd: str, session_id: str) -> list[ChatMessage] | None:
    """恢复会话消息（不含 system）。

    只读取**最后一个压缩边界之后**的事件——边界之前的历史由摘要事件代表，
    但文件里一行都没删。随后做 snip 回放，把边界标记搬回它取代的位置。
    """
    path = session_file_path(cwd, session_id)
    events = parse_events(path)
    if not events:
        return None

    start = _last_boundary_index(events) + 1
    active = events[start:]
    replayed = reconstruct_snipped_events(active)

    messages: list[ChatMessage] = []
    for event in replayed:
        message = unwrap_message(event)
        if message is not None:
            messages.append(message)

    return messages or None


def load_session_with_report(
    cwd: str, session_id: str
) -> tuple[list[ChatMessage] | None, list[str]]:
    """同 `load_session`，但额外返回「回放时被丢弃的 snip 边界 uuid」。

    这是本实现新增的可观测性：原实现会**静默**丢掉这些边界（典型触发是 fork
    之后，被移除的消息没被复制到新文件）。宿主可以据此提示用户
    「有 N 段历史标记已失效」，而不是无声无息。
    """
    path = session_file_path(cwd, session_id)
    events = parse_events(path)
    if not events:
        return None, []

    start = _last_boundary_index(events) + 1
    active = events[start:]
    replayed, dropped = reconstruct_snipped_events_with_report(active)

    messages: list[ChatMessage] = []
    for event in replayed:
        message = unwrap_message(event)
        if message is not None:
            messages.append(message)

    return (messages or None), dropped


def load_context_collapse_state(cwd: str, session_id: str) -> Any | None:
    """恢复折叠状态。只取最后一个压缩边界之后、且 `status == 'committed'` 的 span。

    注意 `enabled` / `consecutive_failures` **不持久化**：恢复出来的状态永远是
    启用且计数为零。「连续失败后禁用」只活在进程内存里。
    """
    from ..context.collapse import CollapseSpan, ContextCollapseState

    path = session_file_path(cwd, session_id)
    events = parse_events(path)
    if not events:
        return None

    start = _last_boundary_index(events) + 1
    spans: list[CollapseSpan] = []
    for event in events[start:]:
        if event.get("type") != EVENT_CONTEXT_COLLAPSE:
            continue
        raw = event.get("contextCollapseSpan")
        if not isinstance(raw, dict):
            continue
        if raw.get("status") != "committed":
            continue
        spans.append(
            CollapseSpan(
                id=str(raw.get("id", "")),
                start_message_id=str(raw.get("startMessageId", "")),
                end_message_id=str(raw.get("endMessageId", "")),
                message_ids=[str(m) for m in (raw.get("messageIds") or [])],
                summary=str(raw.get("summary", "")),
                tokens_before=int(raw.get("tokensBefore") or 0),
                tokens_after=int(raw.get("tokensAfter") or 0),
                status="committed",
                created_at=int(raw.get("createdAt") or 0),
                reason=str(raw.get("reason") or "context_pressure"),
            )
        )

    return ContextCollapseState(spans=spans) if spans else None


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionMeta:
    id: str
    title: str | None
    #: **事件行数**，不是消息条数（含边界/折叠/rename/重写行）。与原实现一致。
    message_count: int
    updated_at: float


def list_sessions(cwd: str) -> list[SessionMeta]:
    directory = project_dir(cwd)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    results: list[SessionMeta] = []
    for entry in entries:
        if not entry.name.endswith(SESSION_SUFFIX) or not entry.is_file():
            continue
        try:
            updated_at = entry.stat().st_mtime
            events = parse_events(entry)
        except OSError:
            continue
        results.append(
            SessionMeta(
                id=entry.name[: -len(SESSION_SUFFIX)],
                title=extract_title_from_events(events),
                message_count=len(_read_lines(entry)),
                updated_at=updated_at,
            )
        )

    results.sort(key=lambda meta: meta.updated_at, reverse=True)
    return results


@dataclass(slots=True)
class ProjectMeta:
    dir: str
    session_count: int
    latest_updated_at: float


def list_all_projects() -> list[ProjectMeta]:
    """列出所有项目目录。

    返回的 `dir` 是**变换后的名字**，不是可 `cd` 的真实路径——这是原实现的
    有损变换导致的既成事实（见 `project_dir_name` 的说明）。
    """
    try:
        entries = list(Path(PROJECTS_DIR).iterdir())
    except OSError:
        return []

    results: list[ProjectMeta] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            files = [f for f in entry.iterdir() if f.name.endswith(SESSION_SUFFIX)]
        except OSError:
            continue
        if not files:
            continue
        stamps: list[float] = []
        for item in files:
            try:
                stamps.append(item.stat().st_mtime)
            except OSError:
                continue
        results.append(
            ProjectMeta(
                dir=entry.name,
                session_count=len(files),
                latest_updated_at=max(stamps) if stamps else 0.0,
            )
        )

    results.sort(key=lambda meta: meta.latest_updated_at, reverse=True)
    return results


# ---------------------------------------------------------------------------
# 删除与清理
# ---------------------------------------------------------------------------


def clear_session(cwd: str, session_id: str, on_cleared: Any | None = None) -> bool:
    """删除一个会话文件。

    与原实现的差异（D13）：原实现只 `unlink` 文件，**不清理调用方的内存状态**。
    于是下一次保存时因为「文件里已无 uuid」而把**整份历史重新追加回去**，
    文件重建、父链重排——删除等于没删。

    本实现接受一个 `on_cleared` 回调，要求调用方在那里清空内存消息与游标。
    返回是否确实删除了文件。
    """
    path = session_file_path(cwd, session_id)
    removed = False
    try:
        path.unlink()
        removed = True
    except OSError:
        removed = False

    # 关键：先清内存，再判断目录。顺序反了会留下「文件没了但内存还在」的窗口。
    if on_cleared is not None:
        on_cleared()

    directory = project_dir(cwd)
    try:
        if directory.exists() and not any(directory.iterdir()):
            shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        pass

    return removed


def cleanup_expired_sessions(cwd: str, max_age_ms: float) -> int:
    """按 mtime 删除超期会话。返回删除数量。"""
    directory = project_dir(cwd)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0

    now = time.time() * 1000
    removed = 0
    for entry in entries:
        if not entry.name.endswith(SESSION_SUFFIX):
            continue
        try:
            age = now - entry.stat().st_mtime * 1000
        except OSError:
            continue
        if age > max_age_ms:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue

    try:
        if directory.exists() and not any(directory.iterdir()):
            shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        pass

    return removed


def fork_session(cwd: str, session_id: str) -> str | None:
    """把会话复制成一个新会话，返回新 id。源会话不存在时返回 None。

    与原实现的差异（D15）：原实现只复制 `load_session` 的结果，而
    `load_session` 已经应用过 snip 回放——**被剪裁的历史本来就不在里面**。
    于是新文件里 `snip_boundary` 的 `removedMessageIds` 一条都匹配不上，
    回放时该边界被永久丢弃，新会话看起来「凭空少了一段」。

    本实现的做法：把源文件里**边界之后的所有事件**按原样复制过去（保持行序），
    因此 snip 边界与被它取代的消息一起过去，回放能正常定位。
    压缩边界之前的旧历史仍然不复制——那部分已由摘要代表。
    """
    source = session_file_path(cwd, session_id)
    source_events = parse_events(source)
    if not source_events:
        return None

    # 取最后一个压缩边界之后的事件（含边界本身，便于新会话有同样的切分点）。
    boundary_index = _last_boundary_index(source_events)
    start = boundary_index if boundary_index >= 0 else 0
    carried = source_events[start:]
    if not carried:
        return None

    new_id = str(uuid.uuid4())[:8]
    target = session_file_path(cwd, new_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 重新线性串父链，避免把源会话的 parentUuid 带过来造成悬空引用。
    rewritten: list[dict[str, Any]] = []
    parent: str | None = None
    for event in carried:
        copied = dict(event)
        if copied.get("type") == EVENT_RENAME:
            # rename 不带 parentUuid，直接复制。
            copied["sessionId"] = new_id
            copied["cwd"] = cwd
            rewritten.append(copied)
            continue
        copied["parentUuid"] = parent
        copied["sessionId"] = new_id
        copied["cwd"] = cwd
        if copied.get("type") in BOUNDARY_EVENT_TYPES | {EVENT_SNIP_BOUNDARY, EVENT_CONTEXT_COLLAPSE}:
            # 边界事件是侧信道：保持 parentUuid = None 的语义。
            copied["parentUuid"] = None
            copied["logicalParentUuid"] = parent
        else:
            parent = str(copied.get("uuid"))
        rewritten.append(copied)

    payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in rewritten)
    target.write_text(payload + "\n", encoding="utf-8")

    # 标题：沿用源会话标题，加 _forkN。
    base_title = extract_title_from_events(source_events) or "session"
    prefix = f"{base_title}_fork"
    used: list[int] = []
    for meta in list_sessions(cwd):
        if not meta.title or not meta.title.startswith(prefix):
            continue
        match = re.match(r"\d+", meta.title[len(prefix) :])
        if match:
            used.append(int(match.group()))
    next_num = max(used) + 1 if used else 1
    rename_session(cwd, new_id, f"{base_title}_fork{next_num}")

    return new_id


# ---------------------------------------------------------------------------
# transcript（UI 用）
# ---------------------------------------------------------------------------


def load_transcript(cwd: str, session_id: str) -> list[dict[str, Any]] | None:
    """把整份事件流渲染成 UI 条目。

    与 `load_session` 的关键区别：**不做压缩边界切分**——整份历史都要渲染，
    因此用户能在界面上看到压缩之前的内容。
    """
    path = session_file_path(cwd, session_id)
    events = parse_events(path)
    if not events:
        return None

    replayed = reconstruct_snipped_events(events)

    entries: list[dict[str, Any]] = []
    tool_entries: dict[str, dict[str, Any]] = {}

    for event in replayed:
        event_type = event.get("type")
        message = event.get("message")
        if not isinstance(message, dict):
            message = {}

        if event_type == EVENT_USER:
            if message.get("internal"):
                continue
            entries.append({"kind": "user", "body": _message_text(message)})
        elif event_type == EVENT_ASSISTANT:
            entries.append({"kind": "assistant", "body": _message_text(message)})
        elif event_type == EVENT_PROGRESS:
            entries.append({"kind": "progress", "body": _message_text(message)})
        elif event_type == EVENT_TOOL_CALL:
            # 先放占位；若之后有配对的结果会**原地改写**这个 dict。
            entry = {
                "kind": "tool",
                "toolName": str(message.get("toolName") or "unknown"),
                "status": "error",
                "body": "No saved result for this tool call.",
            }
            entries.append(entry)
            tool_use_id = message.get("toolUseId")
            if isinstance(tool_use_id, str):
                tool_entries[tool_use_id] = entry
        elif event_type == EVENT_TOOL_RESULT:
            tool_use_id = message.get("toolUseId")
            entry = tool_entries.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if entry is not None:
                entry["status"] = "error" if message.get("isError") else "success"
                entry["body"] = _message_text(message)
            # 孤儿结果：不渲染。
        elif event_type == EVENT_SUMMARY:
            count = message.get("compressedCount")
            entries.append(
                {
                    "kind": "assistant",
                    "body": f"[Context summary: {count if count is not None else 0} messages compressed]",
                }
            )
        elif event_type == EVENT_COMPACT_BOUNDARY:
            metadata = event.get("compactMetadata") or {}
            pre = metadata.get("preTokens", "?")
            post = metadata.get("postTokens", "?")
            entries.append(
                {"kind": "assistant", "body": f"[Context compacted: {pre} → {post} tokens]"}
            )
        elif event_type == EVENT_SNIP_BOUNDARY:
            metadata = event.get("snipMetadata") or {}
            removed = metadata.get("removedCount", "?")
            freed = metadata.get("tokensFreed", "?")
            entries.append(
                {
                    "kind": "assistant",
                    "body": (
                        f"[Snipped earlier context: removed {removed} messages, "
                        f"freed ~{freed} tokens]"
                    ),
                }
            )
        # system / thinking / context_collapse / rename：不渲染。

    return entries or None

