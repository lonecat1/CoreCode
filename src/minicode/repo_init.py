"""仓库初始化 —— `/init` 的实现。

对应原版 `MiniCode/src/init.ts`（343 行）。

## 它做什么

在**当前仓库**里放三样东西，让 MiniCode 下次打开就知道这个项目是什么：

    .mini-code/     项目级配置目录
    .gitignore      追加本项目自己的本地产物（不动用户已有的规则）
    MINI.md         基于**实际探测结果**生成的仓库说明

第三样是核心：它不是一份通用模板，而是先探测仓库（语言、框架、有没有
`src/` `tests/`），再据此生成「验证命令」「仓库结构」「框架注意事项」三节。
一份说「本仓库用 pytest」的说明，比一份说「请填写测试命令」的模板有用得多。

## 三条不可违反的性质

**① 绝不覆盖已有文件。** `MINI.md` 用独占创建（`'wx'`）——已存在就跳过。
用户的 `MINI.md` 可能写了大量项目知识，被模板覆盖是不可接受的损失。

**② `.gitignore` 只追加，不重写。** 只补上缺失的那两行，已有的规则原样保留。

**③ 幂等。** 重复跑不会产生变化，第二次的结果是「全部 skipped」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "GITIGNORE_COMMENT",
    "GITIGNORE_ENTRIES",
    "InitArtifact",
    "InitReport",
    "detect_repo",
    "initialize_repo",
    "render_init_mini_md",
    "render_init_report",
]

InitStatus = Literal["created", "updated", "skipped"]

#: 追加到 `.gitignore` 的注释行。用它判断「本项目是否已经初始化过」。
GITIGNORE_COMMENT = "# MiniCode local artifacts"

#: 追加到 `.gitignore` 的条目。
#:
#: 只忽略**本地产物**：项目级覆盖配置与会话记录。`MINI.md` 刻意**不在**这里
#: ——它是给团队看的仓库说明，应当提交。
GITIGNORE_ENTRIES: tuple[str, ...] = (
    ".mini-code/settings.local.json",
    ".mini-code/sessions/",
)


@dataclass(slots=True)
class InitArtifact:
    name: str
    status: InitStatus


@dataclass(slots=True)
class InitReport:
    project_root: str
    artifacts: list[InitArtifact] = field(default_factory=list)


@dataclass(slots=True)
class RepoDetection:
    """探测结果。每个字段都对应一个**文件系统事实**，不是猜测。"""

    typescript: bool = False
    package_json: bool = False
    python: bool = False
    rust_workspace: bool = False
    rust_root: bool = False
    react: bool = False
    nextjs: bool = False
    vite: bool = False
    nest: bool = False
    src_dir: bool = False
    tests_dir: bool = False
    rust_dir: bool = False


def _exists(*parts: str) -> bool:
    return os.path.exists(os.path.join(*parts))


def _read_text_or_empty(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def detect_repo(cwd: str) -> RepoDetection:
    """探测仓库形态。

    `package.json` 的内容按**小写**匹配，因此 `"React"` / `"react"` 都能认出来。
    原版如此（`readFileSync(...).toLowerCase()`），保持一致。
    """
    package_json_text = _read_text_or_empty(os.path.join(cwd, "package.json")).lower()

    return RepoDetection(
        rust_workspace=_exists(cwd, "rust", "Cargo.toml"),
        rust_root=_exists(cwd, "Cargo.toml"),
        python=(
            _exists(cwd, "pyproject.toml")
            or _exists(cwd, "requirements.txt")
            or _exists(cwd, "setup.py")
        ),
        package_json=_exists(cwd, "package.json"),
        typescript=_exists(cwd, "tsconfig.json") or "typescript" in package_json_text,
        nextjs='"next"' in package_json_text,
        react='"react"' in package_json_text,
        vite='"vite"' in package_json_text,
        nest="@nestjs" in package_json_text,
        src_dir=_exists(cwd, "src"),
        tests_dir=_exists(cwd, "tests"),
        rust_dir=_exists(cwd, "rust"),
    )


def detected_languages(d: RepoDetection) -> list[str]:
    """语言清单。

    注意 `TypeScript` 与 `JavaScript/Node.js` 是**互斥**的：有 tsconfig 或
    依赖里有 typescript 就报 TypeScript，否则才报 JS。同时列出两者会让人
    以为这是两个独立的语言层。
    """
    languages: list[str] = []
    if d.rust_workspace or d.rust_root:
        languages.append("Rust")
    if d.python:
        languages.append("Python")
    if d.typescript:
        languages.append("TypeScript")
    elif d.package_json:
        languages.append("JavaScript/Node.js")
    return languages


def detected_frameworks(d: RepoDetection) -> list[str]:
    frameworks: list[str] = []
    if d.nextjs:
        frameworks.append("Next.js")
    if d.react:
        frameworks.append("React")
    if d.vite:
        frameworks.append("Vite")
    if d.nest:
        frameworks.append("NestJS")
    return frameworks


def verification_lines(cwd: str, d: RepoDetection) -> list[str]:
    """「怎么验证改动」——这是生成内容里最有价值的一节。"""
    lines: list[str] = []
    if d.rust_workspace:
        lines.append(
            "- Run Rust verification from `rust/`: `cargo fmt`, "
            "`cargo clippy --workspace --all-targets -- -D warnings`, "
            "`cargo test --workspace`"
        )
    elif d.rust_root:
        lines.append(
            "- Run Rust verification: `cargo fmt`, "
            "`cargo clippy --workspace --all-targets -- -D warnings`, "
            "`cargo test --workspace`"
        )
    if d.python:
        if _exists(cwd, "pyproject.toml"):
            lines.append(
                "- Run Python checks from `pyproject.toml` "
                "(e.g. `pytest`, `ruff check`, `mypy` when configured)."
            )
        else:
            lines.append(
                "- Run the repo's Python test/lint commands before shipping changes."
            )
    if d.package_json:
        lines.append(
            "- Run the JS/TS checks from `package.json` before shipping changes "
            "(`npm test`, `npm run lint`, `npm run build`)."
        )
    if d.tests_dir and d.src_dir:
        lines.append(
            "- `src/` and `tests/` are both present; update both surfaces together "
            "when behavior changes."
        )
    return lines


def repository_shape_lines(d: RepoDetection) -> list[str]:
    lines: list[str] = []
    if d.rust_dir:
        lines.append(
            "- `rust/` contains the Rust workspace and active CLI/runtime implementation."
        )
    if d.src_dir:
        lines.append(
            "- `src/` contains source files that should stay consistent with "
            "generated guidance and tests."
        )
    if d.tests_dir:
        lines.append(
            "- `tests/` contains validation surfaces that should be reviewed "
            "alongside code changes."
        )
    return lines


def framework_notes(d: RepoDetection) -> list[str]:
    lines: list[str] = []
    if d.nextjs:
        lines.append(
            "- Next.js detected: preserve routing/data-fetching conventions and "
            "verify production builds after changing app structure."
        )
    # React 的提示在 Next.js 项目里没有意义（Next 自带 React），因此排除。
    if d.react and not d.nextjs:
        lines.append(
            "- React detected: keep component behavior covered with focused tests "
            "and avoid unnecessary prop/API churn."
        )
    if d.vite:
        lines.append(
            "- Vite detected: validate the production bundle after changing "
            "build-sensitive configuration or imports."
        )
    if d.nest:
        lines.append(
            "- NestJS detected: keep module/provider boundaries explicit and verify "
            "controller/service wiring after refactors."
        )
    return lines


def render_init_mini_md(cwd: str) -> str:
    """生成 `MINI.md` 的正文。

    各节按需出现：探测不到就**整节不写**，而不是留一个空标题让人去填。
    空标题看起来像「这里本该有内容但没生成」，而实际上「没有内容」就是正确的。
    """
    detection = detect_repo(cwd)
    sections: list[list[str]] = []

    sections.append(
        [
            "# MINI.md",
            "",
            "This file provides guidance to MiniCode when working with code "
            "in this repository.",
            "",
        ]
    )

    stack = ["## Detected stack"]
    languages = detected_languages(detection)
    frameworks = detected_frameworks(detection)
    if not languages:
        stack.append(
            "- No specific language markers were detected yet; document the primary "
            "language and verification commands once the project structure settles."
        )
    else:
        stack.append(f"- Languages: {', '.join(languages)}.")
    if not frameworks:
        stack.append(
            "- Frameworks: none detected from the supported starter markers."
        )
    else:
        stack.append(f"- Frameworks/tooling: {', '.join(frameworks)}.")
    stack.append("")
    sections.append(stack)

    verify = verification_lines(cwd, detection)
    if verify:
        sections.append(["## Verification", *verify, ""])

    shape = repository_shape_lines(detection)
    if shape:
        sections.append(["## Repository shape", *shape, ""])

    notes = framework_notes(detection)
    if notes:
        sections.append(["## Framework notes", *notes, ""])

    sections.append(
        [
            "## Working agreement",
            "- Prefer small, reviewable changes and keep generated bootstrap files "
            "aligned with actual repo workflows.",
            "- Keep shared defaults in `~/.mini-code/settings.json`; reserve "
            "`.mini-code/settings.local.json` for project-local overrides.",
            "- Do not overwrite existing `MINI.md` content automatically; update it "
            "intentionally when repo workflows change.",
            "",
        ]
    )

    return "\n".join("\n".join(section) for section in sections)


def _ensure_dir(dir_path: str) -> InitStatus:
    if os.path.exists(dir_path):
        if not os.path.isdir(dir_path):
            raise NotADirectoryError(
                f"Cannot initialize: {dir_path} exists and is not a directory"
            )
        return "skipped"
    os.makedirs(dir_path, exist_ok=True)
    return "created"


def _write_file_if_missing(file_path: str, content: str) -> InitStatus:
    """**独占创建**：已存在就跳过，绝不覆盖。

    用 `open(..., "x")` 而不是「先判断存在再写」——后者有竞态，而且两个调用
    之间文件可能被创建。独占创建把判断和写入合成一个原子操作。
    """
    try:
        with open(file_path, "x", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return "created"
    except FileExistsError:
        return "skipped"
    except FileNotFoundError:
        # 父目录不存在：补建后重试一次。
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(file_path, "x", encoding="utf-8", newline="") as handle:
                handle.write(content)
            return "created"
        except FileExistsError:
            return "skipped"


def ensure_gitignore_entries(gitignore_path: str) -> InitStatus:
    """补齐 `.gitignore` 里缺失的条目。**只追加，不重写已有规则。**"""
    try:
        with open(gitignore_path, encoding="utf-8", newline="") as handle:
            existing = handle.read()
    except FileNotFoundError:
        lines = [GITIGNORE_COMMENT, *GITIGNORE_ENTRIES]
        with open(gitignore_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(lines) + "\n")
        return "created"

    # 原版对每一行做 `trimEnd()` 后再写回，因此已有的行尾空白会被清掉。
    # 这不是缺陷，但值得知道：这个函数会**触碰**已有行（只去空白）。
    current = [line.rstrip() for line in existing.split("\n")]
    changed = False

    if GITIGNORE_COMMENT not in current:
        current.append(GITIGNORE_COMMENT)
        changed = True

    for entry in GITIGNORE_ENTRIES:
        if entry not in current:
            current.append(entry)
            changed = True

    if not changed:
        return "skipped"

    with open(gitignore_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(current) + "\n")
    return "updated"


def initialize_repo(cwd: str) -> InitReport:
    """初始化仓库。**幂等**：重复调用只会得到「全部 skipped」。"""
    artifacts = [
        InitArtifact(".mini-code/", _ensure_dir(os.path.join(cwd, ".mini-code"))),
        InitArtifact(
            ".gitignore", ensure_gitignore_entries(os.path.join(cwd, ".gitignore"))
        ),
        InitArtifact(
            "MINI.md",
            _write_file_if_missing(
                os.path.join(cwd, "MINI.md"), render_init_mini_md(cwd)
            ),
        ),
    ]
    return InitReport(project_root=cwd, artifacts=artifacts)


def _status_label(status: InitStatus) -> str:
    if status == "created":
        return "created"
    if status == "updated":
        return "updated"
    return "skipped (already exists)"


def render_init_report(report: InitReport) -> str:
    """人可读的初始化报告。

    格式照抄原版 `init.ts:327-343`（含列宽 16 的对齐）——它是用户可见的输出。
    """
    lines = [
        "Init",
        f"  Project          {report.project_root}",
    ]
    for artifact in report.artifacts:
        lines.append(f"  {artifact.name:<16} {_status_label(artifact.status)}")
    lines.append("  Next step        Review and tailor the generated guidance")
    return "\n".join(lines)
