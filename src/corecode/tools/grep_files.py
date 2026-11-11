"""grep_files —— 在文件里搜索文本。

优先用 `rg`（ripgrep）：它快、尊重 `.gitignore`、输出格式统一。
但**不假设它一定存在**——没有 rg 时退回纯 Python 扫描，因此这个工具
在任何机器上都能用。这是本项目「零第三方运行时依赖」取向的延伸：
外部二进制可以**增强**能力，但不应该成为**前提**。

纯 Python 回退刻意保守：跳过常见的大目录与二进制文件，并给结果设上限，
避免把整个仓库塞进上下文。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Iterator

from ..tooling import ToolContext, ToolDefinition, ToolResult
from ..workspace import resolve_tool_path

#: 最多返回多少条匹配行。超限时截断并注明。
MAX_MATCHES = 200

#: 单行输出长度上限，避免压缩后的单行文件炸掉上下文。
MAX_LINE_CHARS = 500

#: 纯 Python 回退时跳过的目录名。
#:
#: `__pycache__` 在这里是**必需**的：文本搜索靠二进制后缀过滤能挡掉 .pyc，
#: 但递归列文件（list_files 的 glob 模式）会把它列出来，导致「有多少个
#: .py 文件」这类统计偏大。
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        "target",
        ".tox",
        ".eggs",
    }
)

#: 这些扩展名视为二进制，直接跳过。
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf",
        ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo",
        ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi",
    }
)

#: 单文件大小上限（字节）——更大的文件在回退扫描里跳过。
MAX_FILE_BYTES = 2_000_000


def _format_match(path: str, line_number: int, line: str) -> str:
    text = line.rstrip("\n")[:MAX_LINE_CHARS]
    return f"{path}:{line_number}:{text}"


def _search_with_ripgrep(
    pattern: str, target: str, cwd: str
) -> tuple[bool, str]:
    """用 rg 搜索。返回 `(是否可用, 输出)`。"""
    executable = shutil.which("rg")
    if executable is None:
        return False, ""

    try:
        completed = subprocess.run(
            [executable, "-n", "--no-heading", "--color", "never", pattern, target],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        # rg 存在但跑不起来（权限、超时）→ 交给回退实现。
        return False, ""

    # rg 退出码 1 表示「没有匹配」，不是错误。
    if completed.returncode not in (0, 1):
        return False, ""
    raw = (completed.stdout or completed.stderr or "").strip()
    return True, raw


def _iter_candidate_files(target: str) -> Iterator[str]:
    """产出待扫描的文件路径。

    ## 为什么不能直接 `os.walk(target)`

    `path` 可以是**文件**也可以是目录（工具描述如此，`/grep <pattern>::<file>`
    这个文档化用法就是传文件）。而 `os.walk()` 遇到文件路径时**什么都不产出**
    ——不报错，只是静默返回空。

    结果是「搜一个明确的文件」永远得到 `(no matches)`，即使用户能看到那行
    文字就在文件里。这个失败方式特别糟：它看起来像「确实没有匹配」，而不是
    「我根本没搜」。

    因此这里对文件路径单独处理。
    """
    if os.path.isfile(target):
        yield target
        return

    for directory, subdirectories, files in os.walk(target):
        subdirectories[:] = [
            name for name in subdirectories if name not in SKIP_DIRECTORIES
        ]
        for name in files:
            yield os.path.join(directory, name)


def _search_with_python(pattern: str, target_root: str, cwd: str) -> str:
    """纯 Python 回退扫描。"""
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        return f"正则表达式非法：{error}"

    matches: list[str] = []
    truncated = False

    for full in _iter_candidate_files(target_root):
        if len(matches) >= MAX_MATCHES:
            truncated = True
            break

        _, suffix = os.path.splitext(full)
        if suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue

        try:
            with open(full, encoding="utf-8", errors="strict") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeDecodeError):
            # 二进制或不可读：跳过即可，不必报错。
            continue

        relative = os.path.relpath(full, cwd)
        for number, line in enumerate(lines, start=1):
            if compiled.search(line):
                matches.append(_format_match(relative, number, line))
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break

    if not matches:
        return "(no matches)"
    if truncated:
        matches.append(f"… 已达到 {MAX_MATCHES} 条上限，结果被截断。")
    return "\n".join(matches)


def _run_grep(data: Any, context: ToolContext) -> ToolResult:
    pattern = str((data or {}).get("pattern", ""))
    if not pattern:
        return ToolResult.failure("pattern 不能为空。")

    raw_path = (data or {}).get("path")
    if raw_path:
        target = resolve_tool_path(context, str(raw_path), "search")
        display = str(raw_path)
    else:
        target = context.cwd
        display = "."

    available, output = _search_with_ripgrep(pattern, target, context.cwd)
    if available:
        return ToolResult.success(output or "(no matches)")

    body = _search_with_python(pattern, target, context.cwd)
    if body.startswith("正则表达式非法"):
        return ToolResult.failure(body)
    # 注明回退路径，便于排查「为什么结果和 rg 不一样」。
    return ToolResult.success(f"{body}\n\n（未找到 rg，已使用内置扫描器；搜索 {display}）")


grep_files_tool = ToolDefinition(
    name="grep_files",
    description=(
        "在文件里搜索文本，返回 `路径:行号:内容` 形式的匹配行。"
        "优先使用 ripgrep；不可用时退回内置扫描器。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索路径，默认工作目录"},
        },
        "required": ["pattern"],
    },
    validator=lambda data: {
        "pattern": str((data or {}).get("pattern", "")),
        **(
            {"path": str((data or {}).get("path"))}
            if (data or {}).get("path")
            else {}
        ),
    },
    run=_run_grep,
)
