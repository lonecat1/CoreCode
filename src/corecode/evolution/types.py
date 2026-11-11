"""自进化记忆的资产模型。

记忆资产不是聊天消息的别名。聊天消息回答「当时发生了什么」，资产回答
「以后遇到相似任务时，什么经验值得复用」。因此这里刻意保留来源、证据、
置信度、出现次数和访问统计：这些字段共同决定一条经验是否继续留在索引里，
以及是否足够稳定到可以生长为 Skill。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MEMORY_KIND_PROCEDURAL = "procedural"
MEMORY_KIND_EPISODIC = "episodic"
MEMORY_KIND_PROFILE = "profile"
MEMORY_KIND_REPAIR = "repair"
MEMORY_KIND_SKILL = "skill"

MEMORY_KINDS = (
    MEMORY_KIND_PROCEDURAL,
    MEMORY_KIND_EPISODIC,
    MEMORY_KIND_PROFILE,
    MEMORY_KIND_REPAIR,
    MEMORY_KIND_SKILL,
)

MEMORY_SCOPE_PROJECT = "project"
MEMORY_SCOPE_GLOBAL = "global"

MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_ARCHIVED = "archived"


def now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


@dataclass(slots=True)
class MemoryAsset:
    """一条可持久化、可索引、可复用的记忆资产。"""

    kind: str
    title: str
    content: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    scope: str = MEMORY_SCOPE_PROJECT
    project: str = ""
    session_id: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.6
    importance: float = 0.5
    occurrences: int = 1
    dedupe_key: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)
    last_accessed: int = 0
    access_count: int = 0
    status: str = MEMORY_STATUS_ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "tags": list(self.tags),
            "scope": self.scope,
            "project": self.project,
            "sessionId": self.session_id,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "importance": self.importance,
            "occurrences": self.occurrences,
            "dedupeKey": self.dedupe_key,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastAccessed": self.last_accessed,
            "accessCount": self.access_count,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MemoryAsset | None:
        kind = str(raw.get("kind") or "")
        title = str(raw.get("title") or "").strip()
        content = str(raw.get("content") or "").strip()
        if kind not in MEMORY_KINDS or not title or not content:
            return None
        identifier = str(raw.get("id") or "").strip() or uuid.uuid4().hex
        metadata = raw.get("metadata")
        scope = str(raw.get("scope") or MEMORY_SCOPE_PROJECT)
        if scope not in (MEMORY_SCOPE_PROJECT, MEMORY_SCOPE_GLOBAL):
            scope = MEMORY_SCOPE_PROJECT
        status = str(raw.get("status") or MEMORY_STATUS_ACTIVE)
        if status not in (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_ARCHIVED):
            status = MEMORY_STATUS_ACTIVE
        created = _as_int(raw.get("createdAt"), now_ms())
        return cls(
            id=identifier,
            kind=kind,
            title=title,
            content=content,
            summary=str(raw.get("summary") or ""),
            tags=_string_list(raw.get("tags")),
            scope=scope,
            project=str(raw.get("project") or ""),
            session_id=str(raw.get("sessionId") or ""),
            evidence=_string_list(raw.get("evidence")),
            confidence=_clamp(raw.get("confidence"), 0.6),
            importance=_clamp(raw.get("importance"), 0.5),
            occurrences=max(1, _as_int(raw.get("occurrences"), 1)),
            dedupe_key=str(raw.get("dedupeKey") or ""),
            created_at=created,
            updated_at=_as_int(raw.get("updatedAt"), created),
            last_accessed=_as_int(raw.get("lastAccessed"), 0),
            access_count=max(0, _as_int(raw.get("accessCount"), 0)),
            status=status,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


@dataclass(slots=True)
class ReflectionResult:
    """一次回合沉淀的可观察结果。"""

    assets: list[MemoryAsset] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    skills: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MemoryHit:
    asset: MemoryAsset
    score: float
