"""Skills —— 通过 `SKILL.md` 扩展工作流，不需要插件平台。

一个 skill 就是「一个目录 + 一份 SKILL.md」。模型通过 `load_skill` 按需读取
它的正文，因此技能只在实际用到时占用上下文——这比把所有工作流塞进系统提示
要省得多。

`SKILL.md` 可选 front matter 提供 category/tags/intents/适用边界/示例；
`skill_routing.py` 用这些元信息做两阶段召回。没有 front matter 的旧技能
会从正文推导最小元信息，保持兼容。

## 四个发现位置与优先级

    <cwd>/.mini-code/skills          project          （最高）
    <MINICODE_HOME>/skills           user
    <cwd>/.claude/skills             compat_project
    ~/.claude/skills                 compat_user      （最低）

同名时**先到者胜**（project 覆盖 user，以此类推）。兼容 `.claude/` 两个位置
是为了让已有的 Claude Code 技能能直接用。

## 与 TS 实现的两处差异（见 docs/divergences.md）

  - **D18**：user 级目录走 `MINICODE_HOME` 而不是硬编码 `~/.mini-code`。
    原实现在四处硬编码了 `os.homedir()`，导致 `MINICODE_HOME` 形同虚设；
    本项目统一由 `config` 决定家目录。
  - **D19**：`load_skill` / `install_skill` / `remove_managed_skill` 校验
    skill 名，拒绝含路径分隔符或 `..` 的名字。原实现直接 `path.join`，
    `../..` 可以读写技能目录之外的位置。

## 描述如何提取

没有 YAML front-matter。取**首个非标题段落里的首个非标题行**，去掉反引号。
简单、可预测，而且对「作者随手写的 Markdown」足够健壮。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import minicode_home
from .skill_metadata import SkillMetadata, parse_skill_metadata, split_frontmatter

#: 兼容的 Claude Code 技能位置。
CLAUDE_SKILLS_DIRNAME = ".claude"

SOURCE_PROJECT = "project"
SOURCE_USER = "user"
SOURCE_COMPAT_PROJECT = "compat_project"
SOURCE_COMPAT_USER = "compat_user"

DEFAULT_DESCRIPTION = "No description provided."


@dataclass(slots=True)
class SkillSummary:
    name: str
    description: str
    path: str
    source: str
    category: str = "general"
    subcategory: str = ""
    tags: list[str] | None = None
    intents: list[str] | None = None
    when_to_use: str = ""
    when_not_to_use: str = ""
    examples: list[str] | None = None
    priority: float = 0.5
    capabilities: list[str] | None = None


@dataclass(slots=True)
class LoadedSkill:
    name: str
    description: str
    path: str
    source: str
    content: str
    metadata: SkillMetadata | None = None


class SkillError(Exception):
    """skill 名非法或安装失败。"""


# ---------------------------------------------------------------------------
# 名称校验（本实现的加固，见 D19）
# ---------------------------------------------------------------------------


def validate_skill_name(name: str) -> str:
    """校验并规范化 skill 名。

    拒绝：空、绝对路径、含路径分隔符、含 `..`、以 `.` 开头。
    这些名字会被拼进文件路径，因此必须在入口处挡住——
    `load_skill("../secrets")` 不应该能读到技能目录之外。
    """
    normalized = (name or "").strip()
    if not normalized:
        raise SkillError("skill 名不能为空。")
    if os.path.isabs(normalized):
        raise SkillError(f"skill 名不能是绝对路径：{normalized!r}")
    if "/" in normalized or "\\" in normalized:
        raise SkillError(f"skill 名不能包含路径分隔符：{normalized!r}")
    if normalized in (".", "..") or normalized.startswith("."):
        raise SkillError(f"skill 名不能以 `.` 开头：{normalized!r}")
    return normalized


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def skill_roots(cwd: str) -> list[tuple[Path, str]]:
    """按优先级返回 `(目录, source)`。"""
    home = minicode_home()
    return [
        (Path(cwd) / ".mini-code" / "skills", SOURCE_PROJECT),
        (home / "skills", SOURCE_USER),
        (Path(cwd) / CLAUDE_SKILLS_DIRNAME / "skills", SOURCE_COMPAT_PROJECT),
        (Path.home() / CLAUDE_SKILLS_DIRNAME / "skills", SOURCE_COMPAT_USER),
    ]


def managed_skill_root(scope: str, cwd: str) -> Path:
    """管理命令写入的位置。"""
    if scope == "project":
        return Path(cwd) / ".mini-code" / "skills"
    return minicode_home() / "skills"


def extract_description(markdown: str) -> str:
    """取首个非标题段落里的首个非标题行。"""
    normalized = markdown.replace("\r\n", "\n")
    paragraphs = [block.strip() for block in normalized.split("\n\n")]
    for block in paragraphs:
        if not block or block.startswith("#"):
            continue
        for line in block.split("\n"):
            candidate = line.strip()
            if candidate and not candidate.startswith("#"):
                return candidate.replace("`", "")
    return DEFAULT_DESCRIPTION


def _iter_skill_directories(root: Path) -> list[tuple[Path, str]]:
    """支持 `root/<name>` 与 `root/<category>/<name>` 两种目录布局。"""
    result: list[tuple[Path, str]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return result
    for entry in entries:
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            result.append((entry, ""))
            continue
        try:
            children = sorted(entry.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child / "SKILL.md").is_file():
                result.append((child, entry.name))
    return result


def _read_skill(
    directory: Path,
    name: str,
    source: str,
    *,
    default_category: str = "",
) -> LoadedSkill | None:
    skill_file = directory / "SKILL.md"
    try:
        # 用 `utf-8-sig`：它会**吞掉**开头的 BOM（若有）。
        #
        # 这不是洁癖。现实中 SKILL.md 常由 Windows 工具或带 BOM 的编辑器写出；
        # 若用普通 `utf-8` 读，BOM 会留在正文最前面，于是 `# 标题` 变成
        # `\ufeff# 标题`——不再以 `#` 开头，标题被当成描述行，
        # 系统提示里就会出现一段带乱码的描述。这个 bug 真实发生过。
        content = skill_file.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.strip():
        return None
    _frontmatter, body = split_frontmatter(content)
    fallback_description = extract_description(body)
    metadata = parse_skill_metadata(
        content,
        name=name,
        fallback_description=fallback_description,
    )
    if default_category and metadata.category == "general":
        metadata.category = default_category
    return LoadedSkill(
        name=name,
        description=metadata.description,
        path=str(skill_file),
        source=source,
        content=body,
        metadata=metadata,
    )


def discover_skills(cwd: str) -> list[SkillSummary]:
    """扫描全部位置，支持一层分类目录，按优先级去重（先到者胜）。

    `skills/<name>/SKILL.md` 与 `skills/<category>/<name>/SKILL.md` 都可发现；
    后者的父目录名作为默认 category。
    """
    found: dict[str, LoadedSkill] = {}

    for root, source in skill_roots(cwd):
        if not root.is_dir():
            continue
        for directory, category in _iter_skill_directories(root):
            if directory.name in found:
                continue
            loaded = _read_skill(
                directory,
                directory.name,
                source,
                default_category=category,
            )
            if loaded is not None:
                found[directory.name] = loaded

    return [
        SkillSummary(
            name=item.name,
            description=item.description,
            path=item.path,
            source=item.source,
            category=item.metadata.category if item.metadata else "general",
            subcategory=item.metadata.subcategory if item.metadata else "",
            tags=list(item.metadata.tags) if item.metadata else [],
            intents=list(item.metadata.intents) if item.metadata else [],
            when_to_use=item.metadata.when_to_use if item.metadata else "",
            when_not_to_use=(
                item.metadata.when_not_to_use if item.metadata else ""
            ),
            examples=list(item.metadata.examples) if item.metadata else [],
            priority=item.metadata.priority if item.metadata else 0.5,
            capabilities=(
                list(item.metadata.capabilities) if item.metadata else []
            ),
        )
        for item in found.values()
    ]


def load_skill(cwd: str, name: str) -> LoadedSkill | None:
    """按优先级查找并加载一个 skill。名字非法时返回 None（不抛）。"""
    try:
        normalized = validate_skill_name(name)
    except SkillError:
        return None

    for root, source in skill_roots(cwd):
        if not root.is_dir():
            continue
        for directory, category in _iter_skill_directories(root):
            if directory.name != normalized:
                continue
            loaded = _read_skill(
                directory,
                normalized,
                source,
                default_category=category,
            )
            if loaded is not None:
                return loaded
    return None


# ---------------------------------------------------------------------------
# 管理（安装 / 移除）
# ---------------------------------------------------------------------------


def install_skill(
    cwd: str,
    source_path: str,
    *,
    name: str | None = None,
    scope: str = "user",
) -> tuple[str, str]:
    """把一个 skill 安装到管理目录。返回 `(名称, 目标文件路径)`。

    源可以是「包含 SKILL.md 的目录」，也可以直接是 SKILL.md 文件路径。
    """
    resolved = Path(source_path)
    if not resolved.is_absolute():
        resolved = (Path(cwd) / source_path).resolve()

    content: str | None = None
    inferred = ""

    if resolved.is_dir():
        candidate = resolved / "SKILL.md"
        try:
            content = candidate.read_text(encoding="utf-8-sig", errors="replace")
            inferred = resolved.name
        except (OSError, UnicodeDecodeError):
            content = None

    if content is None:
        candidate = resolved if resolved.name == "SKILL.md" else resolved / "SKILL.md"
        try:
            content = candidate.read_text(encoding="utf-8-sig", errors="replace")
            inferred = candidate.parent.name
        except (OSError, UnicodeDecodeError) as error:
            raise SkillError(f"找不到可安装的 SKILL.md：{source_path}") from error

    skill_name = validate_skill_name(name if name is not None else inferred)

    target_dir = managed_skill_root(scope, cwd) / skill_name
    target_file = target_dir / "SKILL.md"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file.write_text(content, encoding="utf-8")
    return skill_name, str(target_file)


def remove_managed_skill(cwd: str, name: str, *, scope: str = "user") -> tuple[bool, str]:
    """移除一个已安装的 skill。返回 `(是否真的删除了, 路径)`。"""
    skill_name = validate_skill_name(name)
    target = managed_skill_root(scope, cwd) / skill_name
    if not target.exists():
        return False, str(target)
    try:
        shutil.rmtree(target)
    except OSError:
        return False, str(target)
    return True, str(target)
