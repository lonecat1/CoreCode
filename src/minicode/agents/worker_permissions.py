"""Worktree worker 的非交互权限策略。

worktree 内可以写文件、跑验证命令，但没有用户可询问：所有请求必须在
进入工具前被静态判定。这里采用**保守允许清单**：

  - 路径必须位于该 worker 的 worktree；
  - 只允许常见测试、构建、类型检查和只读 Git 子命令；
  - 未知命令、shell 片段、解释器任意脚本运行一律拒绝。

这不是操作系统沙箱。它约束的是 Agent 通过工具发起的动作；主 Agent 仍应
在合并前检查 diff，并在根工作区运行最终验证。
"""

from __future__ import annotations

import os
from typing import Any

from ..permissions import (
    PermissionDenied,
    classify_dangerous_command,
    is_within_directory,
)

_GIT_SAFE = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep"}
)
_PYTHON_TEST_MODULES = frozenset({"pytest", "unittest", "compileall"})
_PACKAGE_ACTIONS = frozenset(
    {"test", "build", "lint", "typecheck", "check"}
)
_PACKAGE_SCRIPT_NAMES = frozenset({"test", "build", "lint", "typecheck", "check"})
_CARGO_ACTIONS = frozenset({"test", "check", "build", "clippy", "fmt"})
_GO_ACTIONS = frozenset({"test", "build", "vet"})
_DOTNET_ACTIONS = frozenset({"test", "build"})
_DIRECT_VERIFIERS = frozenset(
    {
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "eslint",
        "jest",
        "vitest",
    }
)


def _first_nonflag(args: list[str]) -> str:
    for item in args:
        if not item.startswith("-"):
            return item
    return ""


def _argument_paths_are_bounded(root: str, args: list[str]) -> bool:
    """拒绝通过参数把读写目标指向工作树之外。"""
    root_real = os.path.realpath(root)
    for raw in args:
        value = raw
        if raw.startswith("-") and "=" in raw:
            value = raw.split("=", 1)[1]
        elif raw.startswith("-"):
            continue
        if not value:
            continue
        if os.path.isabs(value) or value.startswith(("/", "\\", "~")):
            if not is_within_directory(root_real, os.path.realpath(value)):
                return False
        elif ".." in value.split("/") or ".." in value.split("\\"):
            resolved = os.path.realpath(os.path.join(root_real, value))
            if not is_within_directory(root_real, resolved):
                return False
    return True


def _allowed_command(command: str, args: list[str]) -> bool:
    command = os.path.basename(command).lower()
    normalized = list(args)

    if command in _DIRECT_VERIFIERS:
        return True
    if command in {"python", "python3"}:
        if "--version" in normalized or "-V" in normalized:
            return True
        if "-m" in normalized:
            position = normalized.index("-m")
            module = normalized[position + 1] if position + 1 < len(normalized) else ""
            return module in _PYTHON_TEST_MODULES
        return False
    if command == "node":
        return "--test" in normalized or "--version" in normalized
    if command in {"npm", "pnpm", "yarn"}:
        action = _first_nonflag(normalized)
        if action in _PACKAGE_ACTIONS:
            return True
        if action != "run":
            return False
        try:
            position = normalized.index("run")
        except ValueError:
            return False
        script = next(
            (item for item in normalized[position + 1 :] if not item.startswith("-")),
            "",
        )
        return script in _PACKAGE_SCRIPT_NAMES
    if command == "cargo":
        return _first_nonflag(normalized) in _CARGO_ACTIONS
    if command == "go":
        return _first_nonflag(normalized) in _GO_ACTIONS
    if command == "dotnet":
        return _first_nonflag(normalized) in _DOTNET_ACTIONS
    if command in {"mvn", "gradle", "gradlew"}:
        action = _first_nonflag(normalized)
        return action in _PACKAGE_ACTIONS
    if command == "git":
        return _first_nonflag(normalized) in _GIT_SAFE
    if command == "make":
        action = _first_nonflag(normalized)
        return action in {"test", "check", "lint"}
    return False


class WorkerPermissionManager:
    """一个 worktree 的固定边界；没有交互提示。"""

    def __init__(self, worktree_root: str) -> None:
        self.worktree_root = os.path.abspath(worktree_root)

    def begin_turn(self, signal: Any | None = None) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def ensure_path_access(self, target_path: str, intent: str = "read") -> None:
        target = os.path.realpath(target_path)
        root = os.path.realpath(self.worktree_root)
        if not is_within_directory(root, target):
            raise PermissionDenied(
                f"Worktree worker 不能访问工作树之外的路径：{target_path}"
            )

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        self.ensure_path_access(target_path, "write")

    def ensure_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        *,
        force_prompt_reason: str | None = None,
    ) -> None:
        self.ensure_path_access(command_cwd, "command_cwd")
        if not _argument_paths_are_bounded(self.worktree_root, args):
            raise PermissionDenied("Worktree worker 的命令参数指向了工作树之外。")
        if _allowed_command(command, args):
            return
        if force_prompt_reason:
            raise PermissionDenied(
                f"Worktree worker 不允许未知命令 {command!r}：{force_prompt_reason}"
            )
        reason = classify_dangerous_command(command, args)
        detail = f"（{reason}）" if reason else ""
        raise PermissionDenied(
            f"Worktree worker 的命令不在验证白名单中：{command} {args}{detail}"
        )


__all__ = ["WorkerPermissionManager"]
