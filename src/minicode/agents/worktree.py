"""Git worktree 隔离与变更回收。

Worktree 子 Agent 不直接改主工作区。每个 worker 得到一份独立的 detached
worktree，完成后把「相对基准提交的完整 patch」交回主 Agent。主 Agent 先查看
patch，再通过权限审批把它应用到根工作区。

这里不自动 commit、rebase 或解决冲突。冲突会以 `git apply --check` 的失败
显式返回，而不是留下半合并状态。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class WorktreeError(RuntimeError):
    """Git worktree 无法创建、采集或应用。"""


@dataclass(slots=True)
class WorktreeInfo:
    path: str
    repo_root: str
    base_commit: str
    base_ref: str = "HEAD"


@dataclass(slots=True)
class WorktreeChanges:
    patch: str = ""
    stat: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.patch.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "stat": self.stat,
            "files": list(self.files),
            "has_changes": self.has_changes,
        }


def _project_key(cwd: str) -> str:
    normalized = os.path.normcase(os.path.abspath(cwd))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    leaf = Path(normalized).name or "workspace"
    safe = "".join(
        char if char.isalnum() or char in "._-" else "-" for char in leaf
    ).strip("-.") or "workspace"
    return f"{safe[:32]}-{digest}"


def _run_git(
    args: list[str],
    *,
    cwd: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
    except OSError as error:
        raise WorktreeError(f"无法启动 git：{error}") from error
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeError(
            f"git {' '.join(args)} 失败（exit={result.returncode}）：{detail}"
        )
    return result


class GitWorktreeManager:
    """为 worker 创建隔离工作树并采集 patch。"""

    def __init__(self, cwd: str, *, home: Path | None = None) -> None:
        self.cwd = os.path.abspath(cwd)
        if home is not None:
            root = Path(home)
        else:
            from ..config import minicode_home

            root = minicode_home()
        self.root = root / "agent-worktrees" / _project_key(self.cwd)

    def repository_root(self) -> str:
        result = _run_git(
            ["rev-parse", "--show-toplevel"],
            cwd=self.cwd,
        )
        root = result.stdout.strip()
        if not root:
            raise WorktreeError("当前目录不是 Git 仓库，无法创建 worktree。")
        return os.path.abspath(root)

    def head_commit(self, repo_root: str) -> str:
        result = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        return result.stdout.strip()

    def create(self, agent_id: str, *, base_ref: str = "HEAD") -> WorktreeInfo:
        repo_root = self.repository_root()
        if base_ref == "HEAD":
            status = _run_git(
                ["status", "--porcelain"], cwd=repo_root
            ).stdout.strip()
            if status:
                raise WorktreeError(
                    "根工作区有未提交改动；worktree 从 HEAD 创建会漏掉这些改动。"
                    "请先提交或清理工作区后再启动 worktree worker。"
                )
        base_commit = _run_git(
            ["rev-parse", base_ref], cwd=repo_root
        ).stdout.strip()
        if not base_commit:
            raise WorktreeError(f"无法解析基准提交：{base_ref!r}")

        target = (self.root / f"{agent_id}-{uuid.uuid4().hex[:8]}").resolve()
        expected_root = self.root.resolve()
        try:
            target.relative_to(expected_root)
        except ValueError as error:
            raise WorktreeError("worktree 目标路径越出管理目录。") from error
        if target.exists():
            raise WorktreeError(f"worktree 目标已存在：{target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["worktree", "add", "--detach", str(target), base_commit],
            cwd=repo_root,
        )
        return WorktreeInfo(
            path=str(target),
            repo_root=repo_root,
            base_commit=base_commit,
            base_ref=base_ref,
        )

    def snapshot(self, worktree: WorktreeInfo) -> WorktreeChanges:
        """把工作树当前状态（含未跟踪文件）变成可检查的二进制 patch。"""
        _run_git(["add", "-A"], cwd=worktree.path)
        patch = _run_git(
            ["diff", "--cached", "--binary", worktree.base_commit],
            cwd=worktree.path,
        ).stdout
        stat = _run_git(
            ["diff", "--cached", "--stat", worktree.base_commit],
            cwd=worktree.path,
        ).stdout.strip()
        files_raw = _run_git(
            ["diff", "--cached", "--name-only", worktree.base_commit],
            cwd=worktree.path,
        ).stdout
        files = [line.strip() for line in files_raw.splitlines() if line.strip()]
        return WorktreeChanges(patch=patch, stat=stat, files=files)

    def apply_to_root(self, patch: str, *, repo_root: str | None = None) -> None:
        """先 check 再 apply；check 失败时根工作区完全不变。"""
        if not patch.strip():
            return
        root = repo_root or self.repository_root()
        check = _run_git(
            ["apply", "--check", "--binary", "--whitespace=nowarn", "-"],
            cwd=root,
            input_text=patch,
            check=False,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout or "").strip()
            raise WorktreeError(f"patch 无法干净应用到根工作区：{detail}")
        _run_git(
            ["apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=root,
            input_text=patch,
        )

    def remove(self, worktree: WorktreeInfo) -> None:
        path = Path(worktree.path).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as error:
            raise WorktreeError("拒绝删除管理目录之外的 worktree。") from error
        if not path.exists():
            return
        _run_git(
            ["worktree", "remove", "--force", str(path)],
            cwd=worktree.repo_root,
            check=False,
        )
        _run_git(["worktree", "prune"], cwd=worktree.repo_root, check=False)


__all__ = [
    "GitWorktreeManager",
    "WorktreeChanges",
    "WorktreeError",
    "WorktreeInfo",
]
