"""记忆资产的持久化与索引更新。

每个项目一个独立资产库；用户画像存全局库，因此换项目仍能复用。写入使用
同目录临时文件 + `os.replace`，避免进程中断留下半份 JSON。资产文件是事实
来源，索引文件只是派生数据；两者 id 集合不一致时会自动重建索引。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from .index import LexicalIndex
from .types import (
    MEMORY_KIND_EPISODIC,
    MEMORY_KIND_PROCEDURAL,
    MEMORY_KIND_PROFILE,
    MEMORY_KIND_REPAIR,
    MEMORY_KIND_SKILL,
    MEMORY_KINDS,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_PROJECT,
    MEMORY_STATUS_ACTIVE,
    MemoryAsset,
    now_ms,
)

STORE_VERSION = 1
MAX_ASSETS_PER_STORE = 2_500

_SAFE_PROJECT_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def project_storage_key(cwd: str) -> str:
    """生成稳定、无碰撞的项目目录名。

    会话存储使用可读但有损的路径变换；记忆库不能承受两个不同项目串库，
    因此这里把绝对路径规范化后取摘要，同时保留一个便于人工辨认的前缀。
    """
    resolved = os.path.normcase(os.path.abspath(cwd))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    leaf = Path(resolved).name or "workspace"
    safe = _SAFE_PROJECT_NAME.sub("-", leaf).strip("-.") or "workspace"
    return f"{safe[:32]}-{digest}"


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _merge_unique(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


class MemoryStore:
    """一个作用域（项目或全局）内的资产与索引。"""

    def __init__(self, directory: Path, *, scope: str, project: str = "") -> None:
        self.directory = Path(directory)
        self.scope = scope
        self.project = project
        self.assets_path = self.directory / "assets.json"
        self.index_path = self.directory / "index.json"
        self._lock = threading.RLock()
        self._assets: dict[str, MemoryAsset] = {}
        self._index = LexicalIndex()
        self._loaded = False

    # -- 读取 ---------------------------------------------------------------

    @property
    def assets(self) -> list[MemoryAsset]:
        with self._lock:
            self._ensure_loaded()
            return list(self._assets.values())

    def get(self, asset_id: str) -> MemoryAsset | None:
        with self._lock:
            self._ensure_loaded()
            return self._assets.get(asset_id)

    def list_assets(
        self,
        *,
        kind: str | None = None,
        status: str = MEMORY_STATUS_ACTIVE,
    ) -> list[MemoryAsset]:
        with self._lock:
            self._ensure_loaded()
            values = list(self._assets.values())
        if kind is not None:
            values = [item for item in values if item.kind == kind]
        if status:
            values = [item for item in values if item.status == status]
        return values

    def find_by_dedupe(self, dedupe_key: str) -> MemoryAsset | None:
        if not dedupe_key:
            return None
        with self._lock:
            self._ensure_loaded()
            for asset in self._assets.values():
                if asset.dedupe_key == dedupe_key and asset.status == MEMORY_STATUS_ACTIVE:
                    return asset
        return None

    # -- 写入 ---------------------------------------------------------------

    def upsert(self, incoming: MemoryAsset) -> tuple[MemoryAsset, bool]:
        """按 `dedupe_key` 合并同类经验。返回 `(资产, 是否新建)`。"""
        with self._lock:
            self._ensure_loaded()
            existing = self.find_by_dedupe(incoming.dedupe_key)
            if existing is None:
                incoming.scope = self.scope
                incoming.project = self.project
                incoming.updated_at = max(incoming.updated_at, now_ms())
                self._assets[incoming.id] = incoming
                self._index.upsert(incoming)
                self._prune_if_needed()
                self._save_locked()
                return incoming, True

            self._merge(existing, incoming)
            self._index.upsert(existing)
            self._prune_if_needed()
            self._save_locked()
            return existing, False

    def touch(self, asset_ids: Iterable[str], *, persist: bool = True) -> None:
        changed = False
        stamp = now_ms()
        with self._lock:
            self._ensure_loaded()
            for asset_id in set(asset_ids):
                asset = self._assets.get(asset_id)
                if asset is None:
                    continue
                asset.last_accessed = stamp
                asset.access_count += 1
                changed = True
            if changed and persist:
                self._save_locked()

    def flush(self) -> None:
        with self._lock:
            self._ensure_loaded()
            self._save_locked()

    def archive(self, asset_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            asset = self._assets.get(asset_id)
            if asset is None:
                return False
            asset.status = "archived"
            asset.updated_at = now_ms()
            self._index.remove(asset_id)
            self._save_locked()
            return True

    # -- 查询 ---------------------------------------------------------------

    def search(self, query: str, limit: int = 12) -> list[tuple[MemoryAsset, float]]:
        with self._lock:
            self._ensure_loaded()
            hits = self._index.search(query, limit=max(limit * 3, limit))
            results: list[tuple[MemoryAsset, float]] = []
            stamp = now_ms()
            for asset_id, score in hits:
                asset = self._assets.get(asset_id)
                if asset is None or asset.status != MEMORY_STATUS_ACTIVE:
                    continue
                # 出现次数代表经验稳定性；重要性与近期性只做温和加权，
                # 避免旧的高频记忆永久压住新的精确匹配。
                stability = 1.0 + min(0.35, (asset.occurrences - 1) * 0.05)
                recency_days = max(0.0, (stamp - asset.updated_at) / 86_400_000)
                recency = 1.0 / (1.0 + recency_days / 90.0)
                adjusted = score * stability * (
                    0.82 + asset.importance * 0.22 + recency * 0.08
                )
                results.append((asset, adjusted))
        results.sort(key=lambda item: item[1], reverse=True)
        return results[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            assets = self.assets
            counts = {kind: 0 for kind in MEMORY_KINDS}
            for asset in assets:
                if asset.status == MEMORY_STATUS_ACTIVE:
                    counts[asset.kind] = counts.get(asset.kind, 0) + 1
            return {
                "assets": sum(counts.values()),
                "byKind": counts,
                "directory": str(self.directory),
                "indexedTerms": len(self._index.postings),
            }

    # -- 内部 ---------------------------------------------------------------

    def _merge(self, existing: MemoryAsset, incoming: MemoryAsset) -> None:
        if incoming.kind == "skill":
            existing.occurrences = max(existing.occurrences, incoming.occurrences)
        else:
            existing.occurrences += max(1, incoming.occurrences)
        existing.confidence = min(
            0.99, max(existing.confidence, incoming.confidence) + 0.025
        )
        existing.importance = min(
            1.0, max(existing.importance, incoming.importance) + 0.01
        )
        existing.evidence = _merge_unique(existing.evidence, incoming.evidence)
        existing.tags = _merge_unique(existing.tags, incoming.tags)
        existing.updated_at = max(existing.updated_at, incoming.updated_at)
        existing.last_accessed = max(existing.last_accessed, incoming.last_accessed)
        existing.session_id = incoming.session_id or existing.session_id

        # 新内容通常包含本次更具体的动作；长度相同则保留更新的一份。
        if len(incoming.content) >= len(existing.content):
            existing.content = incoming.content
            existing.title = incoming.title
            existing.summary = incoming.summary

        merged_metadata = dict(existing.metadata)
        for key, value in incoming.metadata.items():
            if key == "examples" and isinstance(value, list):
                merged_metadata[key] = _merge_unique(
                    merged_metadata.get(key) or [], value
                )[-5:]
            else:
                merged_metadata[key] = value
        existing.metadata = merged_metadata

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = _read_json(self.assets_path)
        if isinstance(raw, dict) and raw.get("version") == STORE_VERSION:
            values = raw.get("assets")
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    asset = MemoryAsset.from_dict(item)
                    if asset is not None:
                        asset.scope = self.scope
                        asset.project = self.project
                        self._assets[asset.id] = asset

        index_raw = _read_json(self.index_path)
        loaded_index = LexicalIndex.from_dict(index_raw)
        active_ids = {
            asset.id
            for asset in self._assets.values()
            if asset.status == MEMORY_STATUS_ACTIVE
        }
        if loaded_index is None or set(loaded_index.lengths) != active_ids:
            self._index = LexicalIndex()
            self._index.rebuild(
                asset
                for asset in self._assets.values()
                if asset.status == MEMORY_STATUS_ACTIVE
            )
            if self._assets:
                self._save_locked()
        else:
            self._index = loaded_index

    def _save_locked(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "scope": self.scope,
            "project": self.project,
            "assets": [
                asset.to_dict()
                for asset in sorted(self._assets.values(), key=lambda item: item.created_at)
            ],
        }
        _atomic_write_json(self.assets_path, payload)
        _atomic_write_json(self.index_path, self._index.to_dict())

    def _prune_if_needed(self) -> None:
        if len(self._assets) <= MAX_ASSETS_PER_STORE:
            return
        ranked = sorted(
            self._assets.values(),
            key=lambda asset: (
                asset.status == MEMORY_STATUS_ACTIVE,
                asset.importance,
                asset.access_count,
                asset.occurrences,
                asset.updated_at,
            ),
        )
        remove_count = max(0, len(self._assets) - MAX_ASSETS_PER_STORE)
        for asset in ranked[:remove_count]:
            self._assets.pop(asset.id, None)
            self._index.remove(asset.id)


def store_root(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home) / "evolved-memory"
    from ..config import minicode_home

    return minicode_home() / "evolved-memory"


def project_store(cwd: str, *, home: Path | None = None) -> MemoryStore:
    root = store_root(home)
    directory = root / "projects" / project_storage_key(cwd)
    return MemoryStore(directory, scope=MEMORY_SCOPE_PROJECT, project=cwd)


def global_store(*, home: Path | None = None) -> MemoryStore:
    return MemoryStore(
        store_root(home) / "global",
        scope=MEMORY_SCOPE_GLOBAL,
        project="",
    )


__all__ = [
    "MAX_ASSETS_PER_STORE",
    "MemoryStore",
    "STORE_VERSION",
    "global_store",
    "project_storage_key",
    "project_store",
    "store_root",
    "MEMORY_KIND_PROCEDURAL",
    "MEMORY_KIND_EPISODIC",
    "MEMORY_KIND_PROFILE",
    "MEMORY_KIND_REPAIR",
    "MEMORY_KIND_SKILL",
]
