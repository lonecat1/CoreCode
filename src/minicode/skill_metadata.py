"""Skill 元信息的解析与兼容层。

`SKILL.md` 可以先不写 front matter；路由层会从正文和描述推导最小元信息。
带 front matter 的 Skill 则可以使用分类、标签、意图、适用边界和示例。

支持的 front matter 是 YAML 的一个保守子集（不引入 PyYAML）：

    ---
    description: Review Python changes
    category: quality
    tags: [python, security]
    intents: [code_review]
    when_to_use: Reviewing a pull request
    when_not_to_use: Runtime debugging
    examples:
      - "审查这个 Python 补丁"
    priority: 0.8
    ---

支持标量、JSON/逗号数组、`- item` 块数组，以及 `|` / `>` 多行文本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_KEY_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
_HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*|[\u3400-\u9fff]{2,}")

_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("code_review", ("review", "audit", "inspect", "审查", "评审", "检查代码")),
    ("debugging", ("debug", "bug", "error", "failure", "修复", "报错", "排查")),
    ("implementation", ("implement", "build", "feature", "create", "实现", "开发", "新增")),
    ("verification", ("test", "verify", "lint", "typecheck", "测试", "验证", "校验")),
    ("documentation", ("docs", "documentation", "readme", "文档", "说明")),
    ("security", ("security", "vulnerability", "secret", "安全", "漏洞")),
    ("refactoring", ("refactor", "cleanup", "restructure", "重构", "整理")),
    ("deployment", ("deploy", "release", "publish", "部署", "发布")),
    ("data", ("data", "sql", "analysis", "数据", "分析")),
)


def _clean_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text[0:1] in {"'", '"'} and text[-1:] == text[0]:
        text = text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    except ValueError:
        pass
    return text


def _parse_inline_list(value: str) -> list[str] | None:
    text = value.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    body = text[1:-1]
    return [
        str(_clean_scalar(item)).strip()
        for item in body.split(",")
        if str(item).strip()
    ]


def _parse_frontmatter_block(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        match = _KEY_LINE.match(raw)
        if not match:
            index += 1
            continue
        key = match.group(1)
        value = match.group(2).strip()
        index += 1

        following: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if candidate.strip() and not candidate.startswith((" ", "\t", "-")):
                break
            following.append(candidate)
            index += 1

        if value in {"|", ">"}:
            pieces = [item.strip() for item in following if item.strip()]
            data[key] = ("\n" if value == "|" else " ").join(pieces)
            continue
        if not value and following:
            list_items = [
                item.strip()[1:].strip()
                for item in following
                if item.strip().startswith("-")
            ]
            if list_items:
                data[key] = [str(_clean_scalar(item)) for item in list_items]
                continue
        inline = _parse_inline_list(value)
        if inline is not None:
            data[key] = inline
        elif value:
            data[key] = _clean_scalar(value)
        elif following:
            data[key] = " ".join(item.strip() for item in following if item.strip())
    return data


def split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    normalized = markdown.replace("\r\n", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, normalized
    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end < 0:
        return {}, normalized
    data = _parse_frontmatter_block(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return data, body


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).replace(";", ",").split(",")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip().strip("`'\"")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _infer_intents(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for intent, keywords in _INTENT_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            found.append(intent)
    return found


def _derive_tags(name: str, text: str) -> list[str]:
    tokens = [
        token.lower()
        for token in _TOKEN.findall(f"{name} {text}")
        if len(token) >= 2
    ]
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
        if len(result) >= 12:
            break
    return result


def _section(body: str, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    lines = body.splitlines()
    capture = False
    depth = 0
    collected: list[str] = []
    for line in lines:
        match = _HEADING.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip().strip("# ").casefold()
            if capture and level <= depth:
                break
            if any(name in title for name in wanted):
                capture = True
                depth = level
                collected = []
                continue
        if capture:
            collected.append(line)
    return "\n".join(collected).strip()


def _bullets(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*")):
            value = stripped[1:].strip()
            if value:
                result.append(value)
    return result


@dataclass(slots=True)
class SkillMetadata:
    name: str
    description: str
    category: str = "general"
    subcategory: str = ""
    tags: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    when_to_use: str = ""
    when_not_to_use: str = ""
    examples: list[str] = field(default_factory=list)
    priority: float = 0.5
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "subcategory": self.subcategory,
            "tags": list(self.tags),
            "intents": list(self.intents),
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "examples": list(self.examples),
            "priority": self.priority,
            "capabilities": list(self.capabilities),
        }


def parse_skill_metadata(
    markdown: str,
    *,
    name: str,
    fallback_description: str,
) -> SkillMetadata:
    data, body = split_frontmatter(markdown)
    description = str(data.get("description") or fallback_description).strip()
    category = str(data.get("category") or "general").strip() or "general"
    subcategory = str(data.get("subcategory") or "").strip()
    tags = _string_list(data.get("tags"))
    intents = _string_list(data.get("intents"))
    when_to_use = str(
        data.get("when_to_use")
        or _section(body, "when to use", "use when", "适用", "何时使用")
        or description
    ).strip()
    when_not = str(
        data.get("when_not_to_use")
        or _section(body, "when not to use", "do not use", "不适用", "何时不要使用")
    ).strip()
    examples = _string_list(data.get("examples"))
    if not examples:
        examples = _bullets(_section(body, "examples", "example requests", "示例"))
    capabilities = _string_list(data.get("capabilities"))
    try:
        priority = float(data.get("priority", 0.5))
    except (TypeError, ValueError):
        priority = 0.5
    if priority > 1.0:
        priority /= 100.0
    priority = max(0.0, min(1.0, priority))
    if not tags:
        tags = _derive_tags(name, " ".join([description, when_to_use]))
    if not intents:
        intents = _infer_intents(
            " ".join([description, when_to_use, *examples])
        )
    return SkillMetadata(
        name=name,
        description=description,
        category=category,
        subcategory=subcategory,
        tags=tags,
        intents=intents,
        when_to_use=when_to_use,
        when_not_to_use=when_not,
        examples=examples[:8],
        priority=priority,
        capabilities=capabilities,
    )


__all__ = [
    "SkillMetadata",
    "parse_skill_metadata",
    "split_frontmatter",
]
