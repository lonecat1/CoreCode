"""记忆资产的轻量词法索引。

项目保持零运行时依赖，因此这里不引入向量数据库。索引采用 BM25 的可解释
实现，并对中文使用字符二元组：这不需要分词词典，也能让「错误修复」匹配到
「修复错误」「错误处理」等同义邻近表达。索引随每次资产写入增量更新，而不是
每回合全量重扫。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from .types import MemoryAsset

INDEX_VERSION = 1

_ASCII_TOKEN = re.compile(r"[a-z0-9_][a-z0-9_.:/\\-]*", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "with",
    "you",
    "your",
    "了",
    "的",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
}


def tokenize(text: str) -> list[str]:
    """把中英文混合文本切成稳定的检索词。"""
    lowered = str(text or "").lower()
    tokens: list[str] = []

    for match in _ASCII_TOKEN.finditer(lowered):
        token = match.group(0).strip(".-:/\\")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        tokens.append(token)

    for match in _CJK_RUN.finditer(lowered):
        run = match.group(0)
        if len(run) == 1:
            if run not in _STOPWORDS:
                tokens.append(run)
            continue
        for index in range(len(run) - 1):
            gram = run[index : index + 2]
            if gram not in _STOPWORDS:
                tokens.append(gram)
        if len(run) <= 4:
            tokens.append(run)

    return tokens


def _asset_text(asset: MemoryAsset) -> str:
    metadata_text = " ".join(str(value) for value in asset.metadata.values())
    return "\n".join(
        [
            asset.kind,
            asset.title,
            asset.summary,
            asset.content,
            " ".join(asset.tags),
            metadata_text,
        ]
    )


class LexicalIndex:
    """可持久化的 BM25 倒排索引。"""

    def __init__(self) -> None:
        # term -> {asset_id: term_frequency}
        self.postings: dict[str, dict[str, int]] = {}
        # asset_id -> {term: term_frequency}
        self.terms_by_id: dict[str, dict[str, int]] = {}
        self.lengths: dict[str, int] = {}

    @property
    def document_count(self) -> int:
        return len(self.lengths)

    @property
    def average_length(self) -> float:
        if not self.lengths:
            return 0.0
        return sum(self.lengths.values()) / len(self.lengths)

    def upsert(self, asset: MemoryAsset) -> None:
        self.remove(asset.id)
        counts = Counter(token for token in tokenize(_asset_text(asset)) if token)
        if not counts:
            # 依然记录文档长度 0，保证删除/重建时 id 集合一致。
            self.terms_by_id[asset.id] = {}
            self.lengths[asset.id] = 0
            return

        self.terms_by_id[asset.id] = dict(counts)
        self.lengths[asset.id] = sum(counts.values())
        for term, frequency in counts.items():
            self.postings.setdefault(term, {})[asset.id] = frequency

    def remove(self, asset_id: str) -> None:
        terms = self.terms_by_id.pop(asset_id, None)
        if terms:
            for term in terms:
                postings = self.postings.get(term)
                if postings is None:
                    continue
                postings.pop(asset_id, None)
                if not postings:
                    self.postings.pop(term, None)
        self.lengths.pop(asset_id, None)

    def rebuild(self, assets: Iterable[MemoryAsset]) -> None:
        self.postings.clear()
        self.terms_by_id.clear()
        self.lengths.clear()
        for asset in assets:
            self.upsert(asset)

    def search(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms or self.document_count == 0:
            return []

        total = self.document_count
        average = self.average_length or 1.0
        scores: dict[str, float] = {}

        # BM25 参数。k1 控制词频饱和，b 控制长度惩罚。
        k1 = 1.4
        b = 0.72
        for term in query_terms:
            postings = self.postings.get(term)
            if not postings:
                continue
            document_frequency = len(postings)
            inverse_frequency = math.log(
                1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for asset_id, frequency in postings.items():
                length = self.lengths.get(asset_id, 0)
                denominator = frequency + k1 * (
                    1.0 - b + b * (length / average)
                )
                contribution = inverse_frequency * (
                    frequency * (k1 + 1.0) / denominator
                )
                scores[asset_id] = scores.get(asset_id, 0.0) + contribution

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return ranked[: max(0, limit)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "postings": self.postings,
            "termsById": self.terms_by_id,
            "lengths": self.lengths,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> LexicalIndex | None:
        if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
            return None
        postings = raw.get("postings")
        terms_by_id = raw.get("termsById")
        lengths = raw.get("lengths")
        if not isinstance(postings, dict) or not isinstance(terms_by_id, dict):
            return None
        if not isinstance(lengths, dict):
            return None

        index = cls()
        try:
            index.postings = {
                str(term): {
                    str(asset_id): int(frequency)
                    for asset_id, frequency in values.items()
                    if isinstance(values, dict)
                }
                for term, values in postings.items()
                if isinstance(values, dict)
            }
            index.terms_by_id = {
                str(asset_id): {
                    str(term): int(frequency)
                    for term, frequency in values.items()
                    if isinstance(values, dict)
                }
                for asset_id, values in terms_by_id.items()
                if isinstance(values, dict)
            }
            index.lengths = {
                str(asset_id): max(0, int(length))
                for asset_id, length in lengths.items()
            }
        except (TypeError, ValueError):
            return None
        return index
