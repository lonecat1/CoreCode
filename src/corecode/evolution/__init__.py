"""自进化记忆沉淀。

公开入口：

    EvolvedMemory(cwd).reflect_turn(...)   回合后反思、提炼、分类入库、索引更新
    EvolvedMemory(cwd).build_context(...)  下回合/下一步按需检索并注入

存储位置由 `CORECODE_HOME` 决定，项目资产与全局用户画像分库保存。
"""

from .engine import EvolvedMemory, EvolvedMemoryPaths
from .reflect import TurnReflection, redact_sensitive, reflect_turn
from .store import MemoryStore, project_storage_key
from .types import (
    MEMORY_KIND_EPISODIC,
    MEMORY_KIND_PROCEDURAL,
    MEMORY_KIND_PROFILE,
    MEMORY_KIND_REPAIR,
    MEMORY_KIND_SKILL,
    MEMORY_KINDS,
    MemoryAsset,
    MemoryHit,
    ReflectionResult,
)

__all__ = [
    "EvolvedMemory",
    "EvolvedMemoryPaths",
    "MEMORY_KINDS",
    "MEMORY_KIND_EPISODIC",
    "MEMORY_KIND_PROCEDURAL",
    "MEMORY_KIND_PROFILE",
    "MEMORY_KIND_REPAIR",
    "MEMORY_KIND_SKILL",
    "MemoryAsset",
    "MemoryHit",
    "MemoryStore",
    "ReflectionResult",
    "TurnReflection",
    "project_storage_key",
    "redact_sensitive",
    "reflect_turn",
]
