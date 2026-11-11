"""Skill 分层路由：粗召回 + 精排 + 紧凑注入。

路由空间分成三层：

    原子 Tool        read_file / edit_file / run_command ...
    高级 Skill       带元信息、适用边界和示例的 SKILL.md
    Skill 目录       分类层级与每类数量，而不是每个技能的长描述

一次路由分两阶段：

  1. 粗召回：按意图、标签、分类和词法重叠，把数百个 Skill 压到
     `MAX_STAGE1_CANDIDATES` 个；
  2. 精排：综合 tag/intent/boundary/example/priority，去重叠后只保留 top-k。

注入给模型的只有精排后的技能卡片；完整正文仍由 `load_skill` 按需读取。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .evolution.index import tokenize
from .skills import SkillSummary, discover_skills

MAX_STAGE1_CANDIDATES = 32
MAX_FINAL_SKILLS = 3
DEFAULT_CONTEXT_BUDGET = 2_000
OVERLAP_THRESHOLD = 0.82

_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "code_review": ("review", "audit", "inspect", "审查", "评审", "检查", "代码检查"),
    "debugging": ("debug", "bug", "error", "failure", "fix", "修复", "报错", "排查"),
    "implementation": (
        "implement",
        "build",
        "feature",
        "create",
        "add",
        "实现",
        "开发",
        "新增",
        "构建",
    ),
    "verification": ("test", "verify", "lint", "typecheck", "测试", "验证", "校验"),
    "documentation": ("docs", "documentation", "readme", "文档", "说明"),
    "security": ("security", "vulnerability", "secret", "安全", "漏洞", "凭据"),
    "refactoring": ("refactor", "cleanup", "restructure", "重构", "整理"),
    "deployment": ("deploy", "release", "publish", "部署", "发布"),
    "data": ("data", "sql", "analysis", "数据", "分析"),
}

_INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    "code_review": ("read_file", "grep_files", "list_files"),
    "debugging": ("read_file", "grep_files", "run_command"),
    "implementation": (
        "read_file",
        "grep_files",
        "edit_file",
        "patch_file",
        "run_command",
    ),
    "verification": ("run_command", "read_file"),
    "documentation": ("read_file", "write_file", "patch_file"),
    "security": ("grep_files", "read_file", "run_command"),
    "refactoring": ("read_file", "grep_files", "edit_file", "run_command"),
    "deployment": ("run_command",),
    "data": ("read_file", "run_command"),
}

_TOOL_TAGS: dict[str, tuple[str, ...]] = {
    "list_files": ("filesystem", "discover"),
    "grep_files": ("filesystem", "search"),
    "read_file": ("filesystem", "read"),
    "write_file": ("filesystem", "write"),
    "edit_file": ("filesystem", "edit"),
    "patch_file": ("filesystem", "edit", "refactor"),
    "modify_file": ("filesystem", "edit"),
    "run_command": ("verification", "build", "test"),
    "web_search": ("web", "research"),
    "web_fetch": ("web", "read"),
    "ask_user": ("clarification",),
    "load_skill": ("skill", "workflow"),
}


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _token_set(text: str) -> set[str]:
    return {token for token in tokenize(text) if token}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def detect_intents(text: str) -> list[str]:
    """轻量意图识别；返回按命中次数排序的意图。"""
    lowered = str(text or "").lower()

    def hit(keyword: str) -> bool:
        lowered_keyword = keyword.lower()
        if re.fullmatch(r"[a-z0-9_ -]+", lowered_keyword):
            return (
                re.search(
                    rf"(?<![a-z0-9_]){re.escape(lowered_keyword)}"
                    rf"(?:s|es|ed|d|ing)?(?![a-z0-9_])",
                    lowered,
                )
                is not None
            )
        return lowered_keyword in lowered

    scores: dict[str, int] = {}
    for intent, keywords in _INTENT_KEYWORDS.items():
        score = sum(1 for keyword in keywords if hit(keyword))
        if score:
            scores[intent] = score
    return [
        intent
        for intent, _score in sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        )
    ]


@dataclass(slots=True)
class SkillCandidate:
    skill: SkillSummary
    score: float
    lexical: float
    tag_score: float
    intent_score: float
    example_score: float
    boundary_penalty: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.skill.name,
            "description": self.skill.description,
            "category": self.skill.category,
            "subcategory": self.skill.subcategory,
            "tags": list(self.skill.tags or []),
            "intents": list(self.skill.intents or []),
            "when_to_use": self.skill.when_to_use,
            "when_not_to_use": self.skill.when_not_to_use,
            "examples": list(self.skill.examples or [])[:3],
            "capabilities": list(self.skill.capabilities or []),
            "source": self.skill.source,
            "path": self.skill.path,
            "score": round(self.score, 4),
            "reason": self.reason,
        }


@dataclass(slots=True)
class SkillRoute:
    query: str
    intents: list[str]
    candidates: list[SkillCandidate]
    suggested_tools: list[str]
    category_counts: dict[str, int]
    total_skills: int
    stage1_count: int
    overlap_dropped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "intents": list(self.intents),
            "candidates": [item.to_dict() for item in self.candidates],
            "suggested_tools": list(self.suggested_tools),
            "category_counts": dict(self.category_counts),
            "total_skills": self.total_skills,
            "stage1_count": self.stage1_count,
            "overlap_dropped": self.overlap_dropped,
        }


class SkillRouter:
    """项目内 Skill 目录的两阶段路由器。"""

    def __init__(
        self,
        cwd: str,
        *,
        tool_definitions: list[Any] | None = None,
        max_stage1: int = MAX_STAGE1_CANDIDATES,
        max_final: int = MAX_FINAL_SKILLS,
        context_budget: int = DEFAULT_CONTEXT_BUDGET,
    ) -> None:
        self.cwd = cwd
        self.max_stage1 = max(1, max_stage1)
        self.max_final = max(1, max_final)
        self.context_budget = max(400, context_budget)
        self._tool_names: list[str] = []
        self._tool_tags: dict[str, set[str]] = {}
        self._skills: list[SkillSummary] = []
        self._category_counts: dict[str, int] = {}
        self.refresh(tool_definitions=tool_definitions)

    def refresh(self, *, tool_definitions: list[Any] | None = None) -> None:
        self._skills = discover_skills(self.cwd)
        counts = Counter(
            (skill.category or "general").strip() or "general"
            for skill in self._skills
        )
        self._category_counts = dict(
            sorted(counts.items(), key=lambda item: item[0].casefold())
        )
        if tool_definitions is not None:
            self._tool_names = []
            self._tool_tags = {}
            for tool in tool_definitions:
                name = str(getattr(tool, "name", "") or "")
                if not name:
                    continue
                self._tool_names.append(name)
                text = f"{name} {getattr(tool, 'description', '')}"
                tags = set(_TOOL_TAGS.get(name, ()))
                tags.update(_token_set(text))
                self._tool_tags[name] = tags

    @property
    def total_skills(self) -> int:
        return len(self._skills)

    @property
    def category_counts(self) -> dict[str, int]:
        return dict(self._category_counts)

    def catalog_text(self) -> str:
        if not self._skills:
            return "Available skill catalog: none discovered."
        ordered = list(self._category_counts.items())
        shown = ", ".join(f"{name}={count}" for name, count in ordered[:12])
        if len(ordered) > 12:
            shown += f", +{len(ordered) - 12} more categories"
        return (
            f"Available skill catalog: {len(self._skills)} skills in "
            f"{len(self._category_counts)} categories ({shown}). "
            "Relevant skill cards are routed dynamically from the current task; "
            "call load_skill with a routed name to read the full workflow."
        )

    def route(
        self,
        query: str,
        *,
        intent: str = "",
        top_k: int | None = None,
    ) -> SkillRoute:
        clean_query = str(query or "").strip()
        intents = _unique([intent, *detect_intents(clean_query)]) if intent else detect_intents(clean_query)
        query_tokens = _token_set(clean_query)
        top_k = max(1, min(self.max_final, top_k or self.max_final))
        if not self._skills:
            return SkillRoute(
                query=clean_query,
                intents=intents,
                candidates=[],
                suggested_tools=self._suggest_tools(intents, []),
                category_counts=self.category_counts,
                total_skills=0,
                stage1_count=0,
            )

        coarse: list[tuple[float, SkillSummary, dict[str, float]]] = []
        for skill in self._skills:
            features = self._features(skill, clean_query, query_tokens, intents)
            coarse_score = (
                features["lexical"] * 0.52
                + features["intent"] * 0.27
                + features["tag"] * 0.16
                + skill.priority * 0.05
                - features["boundary"] * 0.2
            )
            coarse.append((coarse_score, skill, features))

        coarse.sort(key=lambda item: item[0], reverse=True)
        stage1 = coarse[: self.max_stage1]
        reranked: list[SkillCandidate] = []
        for _coarse_score, skill, features in stage1:
            score = (
                features["lexical"] * 0.3
                + features["tag"] * 0.23
                + features["intent"] * 0.22
                + features["example"] * 0.13
                + skill.priority * 0.12
                - features["boundary"] * 0.45
            )
            reasons: list[str] = []
            if features["intent"] > 0:
                reasons.append(f"intent={intents[0] if intents else 'task'}")
            if features["tag"] > 0:
                reasons.append("tag-overlap")
            if features["example"] > 0:
                reasons.append("example-match")
            if features["boundary"] > 0:
                reasons.append("boundary-penalty")
            reranked.append(
                SkillCandidate(
                    skill=skill,
                    score=score,
                    lexical=features["lexical"],
                    tag_score=features["tag"],
                    intent_score=features["intent"],
                    example_score=features["example"],
                    boundary_penalty=features["boundary"],
                    reason=", ".join(reasons) or "catalog-priority",
                )
            )
        reranked.sort(key=lambda item: item.score, reverse=True)

        selected: list[SkillCandidate] = []
        dropped = 0
        for candidate in reranked:
            meaningful = (
                candidate.intent_score > 0
                or candidate.lexical >= 0.03
                or candidate.tag_score >= 0.05
                or candidate.example_score >= 0.05
            )
            if not meaningful or candidate.score < 0.06:
                continue
            if any(
                self._similarity(candidate.skill, existing.skill)
                >= OVERLAP_THRESHOLD
                for existing in selected
            ):
                dropped += 1
                continue
            selected.append(candidate)
            if len(selected) >= top_k:
                break
        return SkillRoute(
            query=clean_query,
            intents=intents,
            candidates=selected,
            suggested_tools=self._suggest_tools(intents, selected),
            category_counts=self.category_counts,
            total_skills=len(self._skills),
            stage1_count=len(stage1),
            overlap_dropped=dropped,
        )

    def context_for_messages(self, messages: list[dict[str, Any]]) -> str:
        query = self._query_from_messages(messages)
        if not query:
            return ""
        return self.format_context(self.route(query))

    def format_context(self, result: SkillRoute) -> str:
        if not result.candidates:
            if not result.total_skills:
                return ""
            return (
                "<skill_routing>\n"
                f"Skill catalog: {result.total_skills} skills in "
                f"{len(result.category_counts)} categories. "
                "No routed skill candidate matched the current task.\n"
                "</skill_routing>"
            )

        lines = [
            "<skill_routing>",
            f"Task intent: {', '.join(result.intents) if result.intents else 'unspecified'}",
            f"Catalog: {result.total_skills} skills / "
            f"{len(result.category_counts)} categories; "
            f"coarse candidates={result.stage1_count}; "
            f"overlap-dropped={result.overlap_dropped}.",
            "Relevant skills after stage-1 recall and stage-2 reranking:",
        ]
        used = sum(len(line) for line in lines)
        for candidate in result.candidates:
            skill = candidate.skill
            layer = skill.category
            if skill.subcategory:
                layer = f"{layer}/{skill.subcategory}"
            card = [
                f"- {skill.name} [layer={layer}; score={candidate.score:.2f}; "
                f"reason={candidate.reason}]",
                f"  Use when: {skill.when_to_use or skill.description}",
            ]
            if skill.when_not_to_use:
                card.append(f"  Not for: {skill.when_not_to_use}")
            if skill.examples:
                card.append(f"  Example: {skill.examples[0]}")
            if skill.tags:
                card.append(f"  Tags: {', '.join(skill.tags[:8])}")
            card.append(f'  Load with: load_skill(name="{skill.name}")')
            block = "\n".join(card)
            if used + len(block) > self.context_budget:
                break
            lines.append(block)
            used += len(block)
        if result.suggested_tools:
            lines.append(
                "Suggested atomic tools: " + ", ".join(result.suggested_tools)
            )
        lines.append("</skill_routing>")
        return "\n".join(lines)

    # -- 内部 ---------------------------------------------------------------

    def _features(
        self,
        skill: SkillSummary,
        query: str,
        query_tokens: set[str],
        intents: list[str],
    ) -> dict[str, float]:
        tags = {str(tag).lower() for tag in (skill.tags or [])}
        intents_set = {str(item).lower() for item in (skill.intents or [])}
        doc_tokens = _token_set(
            " ".join(
                [
                    skill.name,
                    skill.description,
                    skill.when_to_use,
                    skill.category,
                    " ".join(tags),
                    " ".join(skill.examples or []),
                ]
            )
        )
        lexical = _jaccard(query_tokens, doc_tokens)
        tag_score = _jaccard(query_tokens, tags)
        if intents:
            intent_score = 1.0 if intents_set & set(intents) else 0.0
        else:
            intent_score = 0.0
        example_tokens = set()
        for example in skill.examples or []:
            example_tokens.update(_token_set(example))
        example_score = _jaccard(query_tokens, example_tokens)
        boundary_text = (skill.when_not_to_use or "").lower()
        boundary_score = (
            1.0
            if boundary_text and any(token in boundary_text for token in query_tokens)
            else 0.0
        )
        return {
            "lexical": lexical,
            "tag": tag_score,
            "intent": intent_score,
            "example": example_score,
            "boundary": boundary_score,
        }

    def _similarity(self, left: SkillSummary, right: SkillSummary) -> float:
        left_tags = {str(tag).lower() for tag in (left.tags or [])}
        right_tags = {str(tag).lower() for tag in (right.tags or [])}
        tag_similarity = _jaccard(left_tags, right_tags)
        text_similarity = _jaccard(
            _token_set(f"{left.description} {left.when_to_use}"),
            _token_set(f"{right.description} {right.when_to_use}"),
        )
        if left.category != right.category:
            tag_similarity *= 0.7
        return tag_similarity * 0.55 + text_similarity * 0.45

    def _suggest_tools(
        self, intents: list[str], candidates: list[SkillCandidate]
    ) -> list[str]:
        if not self._tool_names:
            return []
        desired: list[str] = []
        for intent in intents[:2]:
            desired.extend(_INTENT_TOOLS.get(intent, ()))
        for candidate in candidates[:2]:
            desired.extend(candidate.skill.capabilities or [])
        available = set(self._tool_names)
        return _unique(name for name in desired if name in available)[:6]

    def _query_from_messages(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for message in reversed(messages[-10:]):
            role = message.get("role")
            if role == "user" and not message.get("internal"):
                content = str(message.get("content") or "").strip()
                if content:
                    parts.append(content[:2_500])
                    break
            if role == "tool_result" and message.get("isError"):
                content = str(message.get("content") or "").strip()
                if content:
                    parts.append(content[:800])
        return "\n".join(parts)


__all__ = [
    "DEFAULT_CONTEXT_BUDGET",
    "MAX_FINAL_SKILLS",
    "MAX_STAGE1_CANDIDATES",
    "OVERLAP_THRESHOLD",
    "SkillCandidate",
    "SkillRoute",
    "SkillRouter",
    "detect_intents",
]
