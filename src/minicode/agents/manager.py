"""SubAgentManager —— 中心化多 Agent 的生命周期与质量门禁。

## 四个教学要点

这个 MVP 刻意保持小的状态面，但包含多 Agent 系统的四个本质概念：

  1. **隔离的上下文**：research 只拿到任务；fork 拿到一份文本快照；
     worktree 在独立 Git 工作树里运行。worker 之间不共享实时状态。
  2. **受限的工具**：research/fork 使用只读子集；worktree 使用读写子集，
     但路径、命令和合并都由额外门禁约束。
  3. **后台并发**：worker 在自己的线程里跑，`spawn` 立即返回句柄，
     根 Agent 继续掌握控制流，之后用 `wait_agent` 收最小报告。
  4. **质量门禁**：worktree 必须带 acceptance；主 Agent 必须先
     `inspect_changes`，用户审批后才允许 `apply_changes`。

## 一个容易做错的地方

`close` 必须能覆盖 `completed`：worker 正在跑的时候被关闭，它的收尾逻辑
**不能**再把状态改回 completed。所以 `_run` 在写状态前先检查「是不是已经被关了」。
少了这一条，`close_agent` 会在报告里看到一个「已完成」的 agent——
而它实际上是被中断的。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..abort import AbortSignal
from ..agent_loop import IdentityContextPipeline, run_agent_turn
from ..skill_routing import SkillRouter
from ..types import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    ChatMessage,
    ModelAdapter,
)

if TYPE_CHECKING:  # pragma: no cover - 仅在类型检查时需要
    # 运行时不导入：`tools/__init__.py` 会导入本模块，
    # 这里再反向导入 `..tooling` 只是类型需求，不必参与运行时依赖图。
    from ..tooling import ToolRegistry
from .types import (
    AGENT_MODES,
    AGENT_ROLES,
    MAX_CODER_AGENT_STEPS,
    MAX_SUB_AGENTS,
    MAX_SUB_AGENT_STEPS,
    MODE_FORK,
    MODE_RESEARCH,
    MODE_WORKTREE,
    ROLE_INVESTIGATOR,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SubAgentSnapshot,
    WaitResult,
)
from .worker_prompt import build_subagent_prompt
from .worker_permissions import WorkerPermissionManager
from .worktree import GitWorktreeManager, WorktreeError, WorktreeInfo


class SubAgentError(Exception):
    """spawn / close 的参数或状态不合法。"""


@dataclass
class _Record:
    id: str
    task: str
    status: str
    started_at: float
    signal: AbortSignal
    thread: threading.Thread | None = None
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None
    mode: str = MODE_RESEARCH
    role: str = ROLE_INVESTIGATOR
    team_id: str = ""
    team_name: str = ""
    acceptance: str = ""
    fork_messages: list[ChatMessage] = field(default_factory=list)
    worktree: WorktreeInfo | None = None
    patch: str = ""
    patch_stat: str = ""
    changed_files: list[str] = field(default_factory=list)
    inspected: bool = False
    applied: bool = False
    worktree_removed: bool = False

    def snapshot(self) -> SubAgentSnapshot:
        return SubAgentSnapshot(
            id=self.id,
            task=self.task,
            status=self.status,
            started_at=self.started_at,
            finished_at=self.finished_at,
            result=self.result,
            error=self.error,
            mode=self.mode,
            role=self.role,
            team_id=self.team_id,
            team_name=self.team_name,
            acceptance=self.acceptance,
            worktree_path=self.worktree.path if self.worktree is not None else "",
            base_commit=(
                self.worktree.base_commit if self.worktree is not None else ""
            ),
            patch_stat=self.patch_stat,
            changed_files=list(self.changed_files),
            patch_available=bool(self.patch.strip()),
            inspected=self.inspected,
            applied=self.applied,
            worktree_removed=self.worktree_removed,
        )


class SubAgentManager:
    """进程内的一组只读 worker。"""

    max_concurrent = MAX_SUB_AGENTS

    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        coder_tools: ToolRegistry | None = None,
        cwd: str,
        memory_provider: Callable[[str], str] | None = None,
        worktrees: GitWorktreeManager | None = None,
        skill_router: SkillRouter | None = None,
    ) -> None:
        self._model = model
        # 这里收到的应当是**已经裁剪过的只读子集**；再裁一次是双保险。
        self._tools = tools
        self._coder_tools = coder_tools
        self._cwd = cwd
        self._memory_provider = memory_provider
        self._worktrees = worktrees or GitWorktreeManager(cwd)
        self._skill_router = skill_router or SkillRouter(
            cwd, tool_definitions=tools.list()
        )
        self._fork_context_provider: Callable[[], list[ChatMessage]] | None = None

        self._records: dict[str, _Record] = {}
        self._lock = threading.RLock()
        self._listeners: set[Callable[[], None]] = set()
        self._next_id = 1
        self._next_team_id = 1

    def set_fork_context_provider(
        self, provider: Callable[[], list[ChatMessage]] | None
    ) -> None:
        """主 Agent 注入 fork 快照来源。"""
        self._fork_context_provider = provider

    def set_skill_router(self, router: SkillRouter) -> None:
        """与宿主共享同一个 Skill 路由缓存。"""
        self._skill_router = router

    # -- 查询 ---------------------------------------------------------------

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.status == STATUS_RUNNING)

    def list(self) -> list[SubAgentSnapshot]:
        with self._lock:
            return [r.snapshot() for r in self._records.values()]

    def get(self, agent_id: str) -> SubAgentSnapshot:
        with self._lock:
            return self._require(agent_id).snapshot()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅状态变化（TUI 用它刷新页脚）。

        监听器存在 `set` 里，所以调用方传进来的必须是可哈希对象
        （函数、闭包、绑定方法都可以；`dataclass` 实例不行）。
        """
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    # -- 生命周期 -----------------------------------------------------------

    def spawn(
        self,
        task: str,
        parent_signal: AbortSignal | None = None,
        *,
        mode: str = MODE_RESEARCH,
        role: str = ROLE_INVESTIGATOR,
        team_id: str = "",
        team_name: str = "",
        acceptance: str = "",
    ) -> SubAgentSnapshot:
        """创建一个后台 worker 并立即返回它的句柄。"""
        normalized = (task or "").strip()
        if not normalized:
            raise SubAgentError("子 Agent 的任务不能为空。")
        if mode not in AGENT_MODES:
            raise SubAgentError(f"未知子 Agent 模式：{mode!r}")
        if role not in AGENT_ROLES:
            raise SubAgentError(f"未知子 Agent 角色：{role!r}")
        acceptance = (acceptance or "").strip()
        if mode == MODE_WORKTREE and not acceptance:
            raise SubAgentError("Worktree 子 Agent 必须提供 acceptance 验收标准。")
        if mode == MODE_WORKTREE and self._coder_tools is None:
            raise SubAgentError("当前 manager 没有配置 worktree 编码工具集。")
        if mode == MODE_FORK and self._fork_context_provider is None:
            raise SubAgentError("Fork 模式需要主 Agent 的对话快照，但当前没有可用上下文。")
        if parent_signal is not None:
            parent_signal.raise_if_aborted()

        with self._lock:
            if self.running_count >= self.max_concurrent:
                raise SubAgentError(
                    f"最多同时运行 {self.max_concurrent} 个子 Agent。"
                )
            agent_id = f"agent_{self._next_id}"
            self._next_id += 1
            fork_messages: list[ChatMessage] = []
            if mode == MODE_FORK:
                fork_messages = self._snapshot_fork_messages()

            worktree: WorktreeInfo | None = None
            if mode == MODE_WORKTREE:
                try:
                    worktree = self._worktrees.create(agent_id)
                except WorktreeError as error:
                    raise SubAgentError(str(error)) from error

            record = _Record(
                id=agent_id,
                task=normalized,
                status=STATUS_RUNNING,
                started_at=_now(),
                signal=AbortSignal(),
                mode=mode,
                role=role,
                team_id=team_id,
                team_name=team_name,
                acceptance=acceptance,
                fork_messages=fork_messages,
                worktree=worktree,
            )
            self._records[record.id] = record

        self._launch(record, parent_signal)
        self._notify()
        return record.snapshot()

    def spawn_team(
        self,
        name: str,
        members: list[dict[str, Any]],
        parent_signal: AbortSignal | None = None,
    ) -> tuple[str, list[SubAgentSnapshot]]:
        """创建一组扁平角色 worker；成员间没有消息通道。"""
        team_name = (name or "").strip()
        if not team_name:
            raise SubAgentError("Agent Team 必须有非空名称。")
        if not isinstance(members, list) or not members:
            raise SubAgentError("Agent Team 至少需要一个成员。")
        if len(members) > self.max_concurrent:
            raise SubAgentError(
                f"Agent Team 最多 {self.max_concurrent} 个成员。"
            )
        with self._lock:
            available = self.max_concurrent - self.running_count
            if len(members) > available:
                raise SubAgentError(
                    f"当前只能再启动 {available} 个 worker，"
                    f"不足以容纳 {len(members)} 人的 Agent Team。"
                )
            team_id = f"team_{self._next_team_id}"
            self._next_team_id += 1

        created: list[SubAgentSnapshot] = []
        try:
            for item in members:
                if not isinstance(item, dict):
                    raise SubAgentError("Agent Team 成员必须是对象。")
                snapshot = self.spawn(
                    str(item.get("task") or ""),
                    parent_signal,
                    mode=str(item.get("mode") or MODE_RESEARCH),
                    role=str(item.get("role") or ROLE_INVESTIGATOR),
                    team_id=team_id,
                    team_name=team_name,
                    acceptance=str(item.get("acceptance") or ""),
                )
                created.append(snapshot)
        except Exception:
            for snapshot in created:
                try:
                    self.close(snapshot.id, delete_worktree=True)
                except SubAgentError:
                    pass
            raise
        return team_id, created

    def wait(self, agent_ids: list[str], timeout_ms: int = 30_000) -> WaitResult:
        """等指定 worker 结束。

        超时**不关闭**它们——只返回当前状态，让根 Agent 决定继续等还是关掉。
        """
        with self._lock:
            records = [self._require(agent_id) for agent_id in agent_ids]

        deadline = _now() + max(0.0, timeout_ms / 1000.0)
        for record in records:
            remaining = deadline - _now()
            if remaining <= 0:
                break
            thread = record.thread
            if thread is not None:
                thread.join(remaining)

        with self._lock:
            timed_out = any(r.status == STATUS_RUNNING for r in records)
            return WaitResult(timed_out=timed_out, agents=[r.snapshot() for r in records])

    def close(
        self, agent_id: str, *, delete_worktree: bool = False
    ) -> SubAgentSnapshot:
        """中断一个 worker 并等它真正停下；可选删除其隔离工作树。"""
        with self._lock:
            record = self._require(agent_id)
            if record.status == STATUS_RUNNING:
                # 先改状态再 abort：`_run` 的收尾逻辑靠这个状态判断「不该覆盖」。
                record.status = STATUS_CLOSED
                record.signal.abort("Sub-agent closed by root agent")

        thread = record.thread
        if thread is not None:
            # **带超时地等 worker 停下（D43）。**
            #
            # 原版是 `await record.completion`——无条件等。在 JS 里那是有界的：
            # abort 会让 await 中的 Promise 走 catch 分支结束。
            #
            # Python 线程**不能从外部取消**：abort 只是把 signal 置位，worker
            # 要自己检查到才会退出。如果它正卡在不检查 signal 的阻塞调用里
            # （网络、子进程、死循环），无条件 join 会让 `/new`、`/exit`、
            # 以及 `close_all` **一起永久挂住**。
            #
            # 5 秒是经验值：正常收尾在毫秒级。超时后我们仍然把状态标成
            # `closed` 并返回——**用户能继续操作**比「保证线程真的停了」更重要，
            # 而残留线程因为 signal 已置位，会在下一个检查点自行退出。
            thread.join(5.0)

        with self._lock:
            if record.finished_at is None:
                record.finished_at = _now()
            worktree = record.worktree
        thread_stopped = thread is None or not thread.is_alive()
        if (
            delete_worktree
            and thread_stopped
            and worktree is not None
            and not record.worktree_removed
        ):
            try:
                self._worktrees.remove(worktree)
                with self._lock:
                    record.worktree_removed = True
            except WorktreeError:
                # 保留原状态并允许用户手工清理；不能让关闭动作本身崩掉会话。
                pass

        with self._lock:
            self._notify()
            return record.snapshot()

    def close_all(self) -> None:
        with self._lock:
            running_ids = [
                r.id for r in self._records.values() if r.status == STATUS_RUNNING
            ]
        for agent_id in running_ids:
            try:
                self.close(agent_id)
            except SubAgentError:
                continue

    # -- 主 Agent 的质量门禁 ------------------------------------------------

    def inspect_changes(self, agent_id: str) -> dict[str, Any]:
        """返回 patch 供主 Agent 审阅，并记录“已检查”状态。"""
        with self._lock:
            record = self._require(agent_id)
            if record.status == STATUS_RUNNING:
                raise SubAgentError(f"{agent_id} 仍在运行，暂时无法审阅改动。")
            if record.mode != MODE_WORKTREE:
                raise SubAgentError(f"{agent_id} 不是 worktree worker，没有待审 patch。")
            if not record.patch.strip():
                raise SubAgentError(f"{agent_id} 没有产生代码改动。")
            record.inspected = True
            return {
                "agent": record.snapshot().to_dict(),
                "patch": record.patch,
            }

    def apply_changes(self, agent_id: str) -> dict[str, Any]:
        """把已审阅的 patch 应用到根工作区。"""
        with self._lock:
            record = self._require(agent_id)
            if record.mode != MODE_WORKTREE or record.worktree is None:
                raise SubAgentError(f"{agent_id} 不是 worktree worker。")
            if record.status != STATUS_COMPLETED:
                raise SubAgentError(
                    f"只有成功完成的 worktree worker 才能合并；当前状态 {record.status}。"
                )
            if not record.acceptance:
                raise SubAgentError("缺少 acceptance 验收标准，拒绝合并。")
            if not record.inspected:
                raise SubAgentError("必须先调用 inspect_agent_changes 审阅 patch。")
            if record.applied:
                return {
                    "agent": record.snapshot().to_dict(),
                    "applied": True,
                    "already_applied": True,
                }
            patch = record.patch
            repo_root = record.worktree.repo_root

        try:
            self._worktrees.apply_to_root(patch, repo_root=repo_root)
        except WorktreeError as error:
            raise SubAgentError(str(error)) from error

        with self._lock:
            record.applied = True
            return {
                "agent": record.snapshot().to_dict(),
                "applied": True,
                "already_applied": False,
            }

    def remove_worktree(self, agent_id: str) -> SubAgentSnapshot:
        return self.close(agent_id, delete_worktree=True)

    # -- 内部 ---------------------------------------------------------------

    def _launch(self, record: _Record, parent_signal: AbortSignal | None) -> None:
        if parent_signal is not None:
            def cancel() -> None:
                with self._lock:
                    if record.status == STATUS_RUNNING:
                        record.status = STATUS_CLOSED
                record.signal.abort(f"父回合被取消：{parent_signal.reason}")

            thread = threading.Thread(
                target=lambda: (parent_signal.wait(), cancel()), daemon=True
            )
            thread.start()

        worker = threading.Thread(
            target=self._run,
            args=(record,),
            name=f"minicode-{record.id}",
            daemon=True,
        )
        record.thread = worker
        worker.start()

    def _snapshot_fork_messages(self) -> list[ChatMessage]:
        provider = self._fork_context_provider
        if provider is None:
            return []
        try:
            raw = provider()
        except Exception:
            return []
        selected: list[ChatMessage] = []
        total = 0
        for message in reversed(list(raw)[-30:]):
            role = message.get("role")
            if role not in (ROLE_USER, ROLE_ASSISTANT):
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            text = content[:4_000]
            if total + len(text) > 16_000:
                break
            selected.append({"role": str(role), "content": text})
            total += len(text)
        selected.reverse()
        return selected

    def _require(self, agent_id: str) -> _Record:
        record = self._records.get(agent_id)
        if record is None:
            raise SubAgentError(f"未知的子 Agent：{agent_id}")
        return record

    def _run(self, record: _Record) -> None:
        try:
            memory_text = ""
            if self._memory_provider is not None:
                try:
                    memory_text = self._memory_provider(self._cwd) or ""
                except Exception:  # noqa: BLE001 - 记忆加载失败不该让 worker 起不来
                    memory_text = ""

            worker_cwd = (
                record.worktree.path
                if record.worktree is not None
                else self._cwd
            )
            tools = (
                self._coder_tools
                if record.mode == MODE_WORKTREE
                else self._tools
            )
            if tools is None:
                raise SubAgentError("worktree worker 缺少编码工具集。")
            permissions = (
                WorkerPermissionManager(worker_cwd)
                if record.mode == MODE_WORKTREE
                else None
            )
            prompt = build_subagent_prompt(
                worker_cwd,
                memory_text,
                mode=record.mode,
                role=record.role,
                acceptance=record.acceptance,
            )
            skill_route = self._skill_router.route(record.task)
            skill_context = self._skill_router.format_context(skill_route)
            if skill_context:
                prompt = f"{prompt}\n\n{skill_context}"
            messages: list[ChatMessage] = [
                {"role": ROLE_SYSTEM, "content": prompt},
                *(record.fork_messages if record.mode == MODE_FORK else []),
                {"role": ROLE_USER, "content": record.task},
            ]
            max_steps = (
                MAX_CODER_AGENT_STEPS
                if record.mode == MODE_WORKTREE
                else MAX_SUB_AGENT_STEPS
            )

            result = run_agent_turn(
                model=self._model,
                tools=tools,
                messages=messages,
                cwd=worker_cwd,
                permissions=permissions,
                signal=record.signal,
                max_steps=max_steps,
                context_pipeline=IdentityContextPipeline(),
            )

            changes = None
            if record.mode == MODE_WORKTREE and record.worktree is not None:
                changes = self._worktrees.snapshot(record.worktree)

            with self._lock:
                # 被关闭过的 worker 状态不再改写——否则 close_agent 会看到「已完成」。
                if record.status != STATUS_CLOSED:
                    if changes is not None:
                        record.patch = changes.patch
                        record.patch_stat = changes.stat
                        record.changed_files = changes.files
                    if result.outcome in ("failed", "aborted"):
                        # `run_agent_turn` 把异常转成了 outcome，而不是抛出来。
                        # 不检查这里，失败会被当成成功——那是假的报告。
                        record.status = STATUS_FAILED
                        record.error = (
                            result.error or f"worker 回合以 {result.outcome} 结束"
                        )
                    elif result.outcome == "max_steps":
                        record.status = STATUS_FAILED
                        record.error = (
                            f"worker 达到 {max_steps} 步上限仍未给出结论"
                        )
                    else:
                        record.status = STATUS_COMPLETED
                        record.result = _last_assistant_message(result.messages)
        except Exception as error:  # noqa: BLE001 - worker 失败必须变成状态而不是崩溃
            with self._lock:
                if record.status != STATUS_CLOSED:
                    record.status = STATUS_FAILED
                    record.error = str(error)
        finally:
            with self._lock:
                record.finished_at = _now()
            self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _last_assistant_message(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.get("role") == ROLE_ASSISTANT:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return "Sub-agent finished without an assistant report."


def _now() -> float:
    import time

    return time.time()
