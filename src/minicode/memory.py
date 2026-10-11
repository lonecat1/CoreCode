"""分层项目记忆 —— 从多个位置收集指令文件。

设计目标是「让项目能说明自己的规矩」，而不要求用户记住某个特定文件名的魔法。

## 分层与优先级

从全局到具体，后加载的覆盖先加载的（因为模型更重视靠后的内容）：

    1. `<MINICODE_HOME>/MINI.md` 或 `CLAUDE.md`    全局
    2. `<MINICODE_HOME>/rules/*.md`               全局规则
    3. 从文件系统根到 cwd，逐级取每个目录的：
         MINI.md · MINI.local.md · .mini-code/MINI.md
         CLAUDE.md · CLAUDE.local.md · .claude/CLAUDE.md
    4. 每级的 `.mini-code/rules/*.md`

**`@path` 引用**：一行只有 `@some/file.md` 时会被就地展开。四类不安全情形
（绝对路径、含 `..`、循环引用、文件不存在）都**不报错**，而是留下一条 HTML
注释说明跳过了什么——静默忽略和直接崩溃都不合适。

内容按 sha256 去重（后出现者胜，因为更具体），并受总字符预算限制。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .config import MINICODE_HOME

#: 单文件字符上限。
MAX_PER_FILE_CHARS = 8_000
#: 全部记忆的字符上限。
MAX_TOTAL_CHARS = 20_000

#: 每个目录按顺序尝试的文件名（先命中者胜，不叠加）。
CANDIDATES_PER_DIR: tuple[str, ...] = (
    "MINI.md",
    "MINI.local.md",
    os.path.join(".mini-code", "MINI.md"),
    "CLAUDE.md",
    "CLAUDE.local.md",
    os.path.join(".claude", "CLAUDE.md"),
)

#: 整行 `@path` 引用。
_INCLUDE_LINE = "@"


@dataclass(slots=True)
class MemoryFile:
    path: str
    scope: str  # global | rules | project
    content: str


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _read_if_present(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def _resolve_includes(
    content: str, from_file: Path, visited: set[str], depth: int = 0
) -> str:
    """展开整行的 `@path` 引用。任何异常都降级成注释，不抛。"""
    lines: list[str] = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped.startswith(_INCLUDE_LINE) or " " in stripped:
            lines.append(raw)
            continue

        reference = stripped[len(_INCLUDE_LINE) :]
        if not reference:
            lines.append(raw)
            continue

        # 安全检查：绝对路径与含 `..` 的路径一律拒绝。
        if os.path.isabs(reference) or ".." in Path(reference).parts:
            lines.append(f"<!-- include skipped: unsafe path {reference} -->")
            continue

        target = (from_file.parent / reference).resolve()
        key = str(target)
        if key in visited:
            lines.append(f"<!-- include skipped: cycle detected {reference} -->")
            continue

        included = _read_if_present(target)
        if included is None:
            lines.append(f"<!-- include skipped: not found {reference} -->")
            continue

        if depth >= 8:
            lines.append(f"<!-- include skipped: too deep {reference} -->")
            continue

        visited.add(key)
        lines.append(f"<!-- included from {reference} -->")
        lines.append(_resolve_includes(included, target, visited, depth + 1))
        lines.append(f"<!-- end include {reference} -->")

    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated]"


def _rule_files(directory: Path) -> list[Path]:
    rules = directory / ".mini-code" / "rules"
    if not rules.is_dir():
        return []
    try:
        return sorted(
            (item for item in rules.iterdir() if item.suffix == ".md" and item.is_file()),
            key=lambda item: item.name,
        )
    except OSError:
        return []


def discover_memory_files(cwd: str, home: Path | None = None) -> list[MemoryFile]:
    """按「全局 → 全局规则 → 根目录 → cwd」的顺序收集。"""
    base = home or Path(MINICODE_HOME)
    found: list[MemoryFile] = []

    # 1. 全局：第一个非空的即停止（不叠加，避免两份全局规则打架）。
    for name in ("MINI.md", "CLAUDE.md"):
        content = _read_if_present(base / name)
        if content:
            found.append(
                MemoryFile(
                    path=str(base / name),
                    scope="global",
                    content=_resolve_includes(content, base / name, {(base / name).resolve()}),
                )
            )
            break

    # 2. 全局规则
    for rule in _rule_files(base):
        content = _read_if_present(rule)
        if content:
            found.append(
                MemoryFile(
                    path=str(rule),
                    scope="rules",
                    content=_resolve_includes(content, rule, {rule.resolve()}),
                )
            )

    # 3. 从文件系统根到 cwd，逐级收集（根在前，cwd 在后）。
    directories: list[Path] = []
    current = Path(cwd).resolve()
    while True:
        directories.append(current)
        if current.parent == current:
            break
        current = current.parent
    directories.reverse()

    for directory in directories:
        for candidate in CANDIDATES_PER_DIR:
            target = directory / candidate
            if not target.is_file():
                continue
            content = _read_if_present(target)
            if content:
                found.append(
                    MemoryFile(
                        path=str(target),
                        scope="project",
                        content=_resolve_includes(content, target, {target.resolve()}),
                    )
                )
            break  # 每级只取第一个命中的文件

        for rule in _rule_files(directory):
            content = _read_if_present(rule)
            if content:
                found.append(
                    MemoryFile(
                        path=str(rule),
                        scope="rules",
                        content=_resolve_includes(content, rule, {rule.resolve()}),
                    )
                )

    return _dedupe(found)


def _dedupe(files: list[MemoryFile]) -> list[MemoryFile]:
    """按内容 sha256 去重：后出现者胜（更具体的位置优先）。"""
    seen: dict[str, MemoryFile] = {}
    order: list[str] = []
    for item in reversed(files):
        digest = hashlib.sha256(item.content.strip().encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen[digest] = item
        order.append(digest)
    order.reverse()
    return [seen[digest] for digest in order]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def load_memory(cwd: str, home: Path | None = None) -> str:
    """渲染成可注入 system 提示的文本块。无文件时返回空串。"""
    files = discover_memory_files(cwd, home)
    if not files:
        return ""

    budget = MAX_TOTAL_CHARS
    sections: list[str] = ["# Instructions"]
    for item in files:
        if budget <= 0:
            sections.append(
                "_Additional instruction content omitted after reaching the prompt budget._"
            )
            break
        allowance = min(MAX_PER_FILE_CHARS, budget)
        body = _truncate(item.content.strip(), allowance)
        sections.append(f"## {os.path.basename(item.path)} (scope: {item.scope})\n{body}")
        budget -= len(body)

    return "\n\n".join(sections)


def describe_memory_files(cwd: str, home: Path | None = None) -> list[dict[str, object]]:
    """给 `/memory` 用的概要。"""
    result: list[dict[str, object]] = []
    for item in discover_memory_files(cwd, home):
        try:
            relative = os.path.relpath(item.path, cwd)
        except ValueError:
            relative = item.path
        result.append(
            {
                "path": relative,
                "scope": item.scope,
                "lines": len(item.content.splitlines()),
                "chars": len(item.content),
            }
        )
    return result


def render_memory_report(cwd: str, home: Path | None = None) -> str:
    entries = describe_memory_files(cwd, home)
    if not entries:
        return "没有发现任何指令文件（MINI.md / CLAUDE.md / .mini-code/rules/*.md）。"
    lines = [f"已加载 {len(entries)} 个指令文件："]
    for entry in entries:
        lines.append(
            f"  [{entry['scope']}] {entry['path']}  "
            f"{entry['lines']} 行 / {entry['chars']} 字符"
        )
    return "\n".join(lines)
