"""权限管理器——项目的安全边界。

所有路径访问、命令执行、文件写入都必须先经过这里。设计上有三个要点：

1. **决策是「注入的提示函数」的结果，不是硬编码的。**
   `PermissionManager` 接收一个 `prompt_handler(request) -> decision`。
   全屏 TUI 用交互面板实现它，测试用自动批准实现它，行模式用简单问答实现它。
   权限逻辑本身完全不知道 UI 长什么样。

2. **授权有四级作用域**，这是「不要反复问同一件事」的关键：
   - `once`：只放行这一个目标（会话内记忆）
   - `turn`：本回合内放行（只对编辑有意义，由 `begin_turn` / `end_turn` 界定）
   - `always`：写进 `~/.mini-code/permissions.json` 持久化
   - `deny`：对应同样的三级拒绝

3. **拒绝通过抛异常表达**，由 `ToolRegistry.execute` 捕获成
   `ToolResult(ok=False, fatal=True)`。因此**拒绝不会终结回合**，
   只是这一步工具失败，错误文本作为 tool_result 回给模型，模型可据此调整。
   真正终结回合的是取消信号。

安全默认：工作目录内**无需询问**（这是日常工作路径，每次问会让工具不可用）；
只有越界访问、危险命令、文件修改才需要批准。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .abort import throw_if_aborted
from .config import permissions_path

# ---------------------------------------------------------------------------
# 决策与请求
# ---------------------------------------------------------------------------

PermissionDecision = str
"""allow_once | allow_turn | allow_all_turn | allow_always | deny_once | deny_with_feedback | deny_always"""


@dataclass(slots=True)
class PermissionChoice:
    key: str
    label: str
    decision: PermissionDecision


@dataclass(slots=True)
class PermissionPromptResult:
    decision: PermissionDecision
    feedback: str | None = None


@dataclass(slots=True)
class PermissionRequest:
    kind: str  # path | command | edit
    summary: str
    details: list[str]
    scope: str
    choices: list[PermissionChoice]


#: 提示函数的契约。传入请求，返回用户的决定。
PermissionPromptHandler = Callable[[PermissionRequest, Any], PermissionPromptResult]


class PermissionDenied(Exception):
    """访问被拒绝。会被工具层转成模型可读的失败结果。"""


class PathIntent:
    READ = "read"
    WRITE = "write"
    LIST = "list"
    SEARCH = "search"
    COMMAND_CWD = "command_cwd"


# ---------------------------------------------------------------------------
# 危险命令识别
# ---------------------------------------------------------------------------

#: 能执行任意本地代码的解释器。
_ARBITRARY_CODE_INTERPRETERS = frozenset(
    {"node", "python3", "python", "bun", "bash", "sh", "pwsh", "powershell"}
)


def classify_dangerous_command(command: str, args: list[str]) -> str | None:
    """返回危险原因；不是危险命令则返回 None。

    识别策略是**保守的枚举**：只列举已知会不可逆丢数据、或会影响本机之外
    的命令。没被列举的命令（例如 `rm -rf`）不在这里拦截——那类由
    `run_command` 的允许清单负责。权限层只管「必须征询用户」的那一类。
    """
    normalized = [arg.strip() for arg in args if arg.strip()]
    signature = " ".join([command, *normalized]).strip()

    if command == "git":
        if "reset" in normalized and "--hard" in normalized:
            return f"git reset --hard 会丢弃本地改动 ({signature})"
        if "clean" in normalized:
            return f"git clean 会删除未跟踪文件 ({signature})"
        if "checkout" in normalized and "--" in normalized:
            return f"git checkout -- 会覆盖工作区文件 ({signature})"
        if "restore" in normalized and any(a.startswith("--source") for a in normalized):
            return f"git restore --source 会覆盖本地文件 ({signature})"
        if "push" in normalized and any(a in ("--force", "-f") for a in normalized):
            return f"git push --force 会改写远端历史 ({signature})"
        return None

    if command in ("npm", "pnpm", "yarn") and "publish" in normalized:
        return f"{command} publish 会影响本机之外的注册表 ({signature})"

    if command in _ARBITRARY_CODE_INTERPRETERS:
        return f"{command} 可以执行任意本地代码 ({signature})"

    return None


def format_command_signature(command: str, args: list[str]) -> str:
    return " ".join([command, *args]).strip()


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


@dataclass
class PermissionStore:
    allowed_directory_prefixes: list[str] = field(default_factory=list)
    denied_directory_prefixes: list[str] = field(default_factory=list)
    allowed_command_patterns: list[str] = field(default_factory=list)
    denied_command_patterns: list[str] = field(default_factory=list)
    allowed_edit_patterns: list[str] = field(default_factory=list)
    denied_edit_patterns: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, list[str]]:
        return {
            "allowedDirectoryPrefixes": self.allowed_directory_prefixes,
            "deniedDirectoryPrefixes": self.denied_directory_prefixes,
            "allowedCommandPatterns": self.allowed_command_patterns,
            "deniedCommandPatterns": self.denied_command_patterns,
            "allowedEditPatterns": self.allowed_edit_patterns,
            "deniedEditPatterns": self.denied_edit_patterns,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PermissionStore:
        def pick(key: str) -> list[str]:
            value = data.get(key)
            return [str(item) for item in value] if isinstance(value, list) else []

        return cls(
            allowed_directory_prefixes=pick("allowedDirectoryPrefixes"),
            denied_directory_prefixes=pick("deniedDirectoryPrefixes"),
            allowed_command_patterns=pick("allowedCommandPatterns"),
            denied_command_patterns=pick("deniedCommandPatterns"),
            allowed_edit_patterns=pick("allowedEditPatterns"),
            denied_edit_patterns=pick("deniedEditPatterns"),
        )


def read_permission_store(path: Any | None = None) -> PermissionStore:
    # 默认走**动态**访问器，不用模块级 `PERMISSIONS_PATH`（导入期快照）。
    # 快照在 `MINICODE_HOME` 变化后会指向错误位置，导致「用户的授权规则
    # 突然全部失效」——而程序不会报错，只是每次都问一遍。
    target = path or permissions_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PermissionStore()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 配置文件损坏时退回安全默认，而不是让整个程序起不来。
        return PermissionStore()
    return PermissionStore.from_json(parsed if isinstance(parsed, dict) else {})


def write_permission_store(store: PermissionStore, path: Any | None = None) -> None:
    target = path or permissions_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(store.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


def normalize_path(target: str) -> str:
    return os.path.abspath(target)


def is_within_directory(root: str, target: str) -> bool:
    root_abs = os.path.abspath(root)
    relative = os.path.relpath(target, root_abs)
    return not (
        relative == ".."
        or relative.startswith(f"..{os.sep}")
        or os.path.isabs(relative)
    )


def matches_directory_prefix(target: str, directories: Any) -> bool:
    return any(is_within_directory(directory, target) for directory in directories)


class PermissionManager:
    """路径/命令/编辑三类操作的权限判定。"""

    def __init__(
        self,
        workspace_root: str,
        prompt: PermissionPromptHandler | None = None,
        *,
        store_path: Any | None = None,
    ) -> None:
        self.workspace_root = normalize_path(workspace_root)
        self._prompt = prompt
        self._store_path = store_path

        # 来自持久化存储的规则
        self._allowed_dirs: set[str] = set()
        self._denied_dirs: set[str] = set()
        self._allowed_commands: set[str] = set()
        self._denied_commands: set[str] = set()
        self._allowed_edits: set[str] = set()
        self._denied_edits: set[str] = set()

        # 仅本次会话生效的规则
        self._session_allowed_paths: set[str] = set()
        self._session_denied_paths: set[str] = set()
        self._session_allowed_commands: set[str] = set()
        self._session_denied_commands: set[str] = set()
        self._session_allowed_edits: set[str] = set()
        self._session_denied_edits: set[str] = set()

        # 仅本回合生效的规则（由 begin_turn / end_turn 界定）
        self._turn_allowed_edits: set[str] = set()
        self._turn_allow_all_edits = False

        self._signal: Any | None = None
        self._load()

    # -- 初始化与回合生命周期 ------------------------------------------------

    def _load(self) -> None:
        store = read_permission_store(self._store_path)
        self._allowed_dirs = {normalize_path(p) for p in store.allowed_directory_prefixes}
        self._denied_dirs = {normalize_path(p) for p in store.denied_directory_prefixes}
        self._allowed_commands = set(store.allowed_command_patterns)
        self._denied_commands = set(store.denied_command_patterns)
        self._allowed_edits = {normalize_path(p) for p in store.allowed_edit_patterns}
        self._denied_edits = {normalize_path(p) for p in store.denied_edit_patterns}

    def begin_turn(self, signal: Any | None = None) -> None:
        """标记一个新回合开始。回合级授权在这里被清空。"""
        self._signal = signal
        self._turn_allowed_edits.clear()
        self._turn_allow_all_edits = False

    def end_turn(self) -> None:
        self._signal = None
        self._turn_allowed_edits.clear()
        self._turn_allow_all_edits = False

    def get_summary(self) -> list[str]:
        """给系统提示看的权限摘要，让模型知道当前边界在哪。"""
        summary = [f"cwd: {self.workspace_root}"]
        if self._allowed_dirs:
            summary.append(
                "extra allowed dirs: " + ", ".join(sorted(self._allowed_dirs)[:4])
            )
        else:
            summary.append("extra allowed dirs: none")
        if self._allowed_commands:
            summary.append(
                "dangerous allowlist: " + ", ".join(sorted(self._allowed_commands)[:4])
            )
        else:
            summary.append("dangerous allowlist: none")
        if self._allowed_edits:
            summary.append(
                "trusted edit targets: " + ", ".join(sorted(self._allowed_edits)[:2])
            )
        return summary

    def _persist(self) -> None:
        write_permission_store(
            PermissionStore(
                allowed_directory_prefixes=sorted(self._allowed_dirs),
                denied_directory_prefixes=sorted(self._denied_dirs),
                allowed_command_patterns=sorted(self._allowed_commands),
                denied_command_patterns=sorted(self._denied_commands),
                allowed_edit_patterns=sorted(self._allowed_edits),
                denied_edit_patterns=sorted(self._denied_edits),
            ),
            self._store_path,
        )

    def _request_approval(self, request: PermissionRequest) -> PermissionPromptResult:
        throw_if_aborted(self._signal)
        if self._prompt is None:
            raise PermissionDenied(
                f"{request.summary}。当前不是交互模式，无法请求批准。"
            )
        result = self._prompt(request, self._signal)
        throw_if_aborted(self._signal)
        return result

    # -- 路径 ---------------------------------------------------------------

    def ensure_path_access(self, target_path: str, intent: str = PathIntent.READ) -> None:
        throw_if_aborted(self._signal)
        target = normalize_path(target_path)

        # 工作目录内直接放行：这是日常路径，每次都问会让工具不可用。
        if is_within_directory(self.workspace_root, target):
            return

        if target in self._session_denied_paths or matches_directory_prefix(
            target, self._denied_dirs
        ):
            raise PermissionDenied(f"Access denied for path outside cwd: {target}")

        if target in self._session_allowed_paths or matches_directory_prefix(
            target, self._allowed_dirs
        ):
            return

        # 授权的粒度：列目录/作为命令 cwd 时按目录授权，读写时按文件所在目录授权。
        scope = (
            target
            if intent in (PathIntent.LIST, PathIntent.COMMAND_CWD)
            else os.path.dirname(target)
        )

        result = self._request_approval(
            PermissionRequest(
                kind="path",
                summary=f"mini-code 请求 {intent.replace('_', ' ')} 权限（在当前 cwd 之外）",
                details=[
                    f"cwd: {self.workspace_root}",
                    f"target: {target}",
                    f"scope directory: {scope}",
                ],
                scope=scope,
                choices=[
                    PermissionChoice("y", "仅本次允许", "allow_once"),
                    PermissionChoice("a", "允许此目录（记住）", "allow_always"),
                    PermissionChoice("n", "仅本次拒绝", "deny_once"),
                    PermissionChoice("d", "拒绝此目录（记住）", "deny_always"),
                ],
            )
        )

        if result.decision == "allow_once":
            self._session_allowed_paths.add(target)
            return
        if result.decision == "allow_always":
            self._allowed_dirs.add(scope)
            self._persist()
            throw_if_aborted(self._signal)
            return
        if result.decision == "deny_always":
            self._denied_dirs.add(scope)
            self._persist()
        else:
            self._session_denied_paths.add(target)

        raise PermissionDenied(f"Access denied for path outside cwd: {target}")

    # -- 命令 ---------------------------------------------------------------

    def ensure_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        *,
        force_prompt_reason: str | None = None,
    ) -> None:
        throw_if_aborted(self._signal)
        self.ensure_path_access(command_cwd, PathIntent.COMMAND_CWD)

        dangerous_reason = classify_dangerous_command(command, args)
        reason = (force_prompt_reason or "").strip() or dangerous_reason
        if not reason:
            return

        signature = format_command_signature(command, args)

        if signature in self._session_denied_commands or signature in self._denied_commands:
            raise PermissionDenied(f"Command denied: {signature}")
        if signature in self._session_allowed_commands or signature in self._allowed_commands:
            return

        result = self._request_approval(
            PermissionRequest(
                kind="command",
                summary=(
                    "mini-code 请求批准执行此命令"
                    if force_prompt_reason
                    else "mini-code 请求执行一条危险命令"
                ),
                details=[
                    f"cwd: {command_cwd}",
                    f"command: {signature}",
                    f"reason: {reason}",
                ],
                scope=signature,
                choices=[
                    PermissionChoice("y", "仅本次允许", "allow_once"),
                    PermissionChoice("a", "始终允许此命令", "allow_always"),
                    PermissionChoice("n", "仅本次拒绝", "deny_once"),
                    PermissionChoice("d", "始终拒绝此命令", "deny_always"),
                ],
            )
        )

        if result.decision == "allow_once":
            self._session_allowed_commands.add(signature)
            return
        if result.decision == "allow_always":
            self._allowed_commands.add(signature)
            self._persist()
            throw_if_aborted(self._signal)
            return
        if result.decision == "deny_always":
            self._denied_commands.add(signature)
            self._persist()
        else:
            self._session_denied_commands.add(signature)

        raise PermissionDenied(f"Command denied: {signature}")

    # -- 编辑 ---------------------------------------------------------------

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        throw_if_aborted(self._signal)
        target = normalize_path(target_path)

        if target in self._session_denied_edits or target in self._denied_edits:
            raise PermissionDenied(f"Edit denied: {target}")

        if (
            target in self._session_allowed_edits
            or target in self._turn_allowed_edits
            or self._turn_allow_all_edits
            or target in self._allowed_edits
        ):
            return

        result = self._request_approval(
            PermissionRequest(
                kind="edit",
                summary="mini-code 请求应用一次文件修改",
                details=[f"target: {target}", "", diff_preview],
                scope=target,
                choices=[
                    PermissionChoice("1", "仅应用本次", "allow_once"),
                    PermissionChoice("2", "本回合内允许此文件", "allow_turn"),
                    PermissionChoice("3", "本回合内允许所有编辑", "allow_all_turn"),
                    PermissionChoice("4", "始终允许此文件", "allow_always"),
                    PermissionChoice("5", "仅本次拒绝", "deny_once"),
                    PermissionChoice("6", "拒绝并给模型反馈", "deny_with_feedback"),
                    PermissionChoice("7", "始终拒绝此文件", "deny_always"),
                ],
            )
        )

        if result.decision == "allow_once":
            self._session_allowed_edits.add(target)
            return
        if result.decision == "allow_turn":
            self._turn_allowed_edits.add(target)
            return
        if result.decision == "allow_all_turn":
            self._turn_allow_all_edits = True
            return
        if result.decision == "allow_always":
            self._allowed_edits.add(target)
            self._persist()
            throw_if_aborted(self._signal)
            return
        if result.decision == "deny_with_feedback":
            guidance = (result.feedback or "").strip()
            if guidance:
                # 用户的反馈会作为 tool_result 回给模型，因此必须带上。
                raise PermissionDenied(
                    f"Edit denied: {target}\nUser guidance: {guidance}"
                )
            self._session_denied_edits.add(target)
            raise PermissionDenied(f"Edit denied: {target}")
        if result.decision == "deny_always":
            self._denied_edits.add(target)
            self._persist()
        else:
            self._session_denied_edits.add(target)

        raise PermissionDenied(f"Edit denied: {target}")


# ---------------------------------------------------------------------------
# 非交互模式的自动批准（供测试与 --yes 模式使用）
# ---------------------------------------------------------------------------


def auto_approve_prompt(
    request: PermissionRequest, signal: Any | None = None
) -> PermissionPromptResult:
    """总是批准「这一次」。用于测试与显式的无人值守模式。"""
    return PermissionPromptResult(decision="allow_once")


def auto_deny_prompt(
    request: PermissionRequest, signal: Any | None = None
) -> PermissionPromptResult:
    return PermissionPromptResult(decision="deny_once")
