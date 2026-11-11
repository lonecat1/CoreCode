"""中心化多 Agent 协作模式的行为测试。

覆盖新增的三条协作路径：

  - Fork：只读快照分叉，不共享实时状态；
  - Worktree：隔离 Git 工作树内编码，审阅后才允许合并；
  - Agent Team：扁平角色分组，成员之间没有消息通道。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.agents.manager import SubAgentError, SubAgentManager  # noqa: E402
from minicode.agents.types import (  # noqa: E402
    MODE_FORK,
    MODE_WORKTREE,
    ROLE_IMPLEMENTER,
    ROLE_INVESTIGATOR,
)
from minicode.agents.worker_permissions import WorkerPermissionManager  # noqa: E402
from minicode.permissions import PermissionDenied  # noqa: E402
from minicode.tooling import ToolContext  # noqa: E402
from minicode.tools import (  # noqa: E402
    create_coder_registry,
    create_root_registry,
    create_worker_registry,
)
from minicode.tools.subagents import create_subagent_tools  # noqa: E402
from minicode.types import AgentStep, ToolCall  # noqa: E402


class _WriteAdapter:
    def __init__(self, path: str = "agent.txt", content: str = "from worker\n") -> None:
        self.path = path
        self.content = content

    def next(self, messages, **_kwargs):
        if any(message.get("role") == "tool_result" for message in messages):
            return AgentStep(
                type="assistant",
                content="Changed agent.txt; verification: pending root review.",
                kind="final",
            )
        return AgentStep(
            type="tool_calls",
            calls=[
                ToolCall(
                    id="write-1",
                    toolName="write_file",
                    input={"path": self.path, "content": self.content},
                )
            ],
        )


class _ReportAdapter:
    def __init__(self, text: str = "brief report") -> None:
        self.text = text

    def next(self, messages, **_kwargs):
        return AgentStep(type="assistant", content=self.text, kind="final")


class _CaptureAdapter:
    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def next(self, messages, **_kwargs):
        self.seen.append([dict(message) for message in messages])
        return AgentStep(type="assistant", content="fork report", kind="final")


class _TaskWriteAdapter:
    """按任务里的文件名写不同文件，用于并行 worktree 团队。"""

    def next(self, messages, **_kwargs):
        if any(message.get("role") == "tool_result" for message in messages):
            return AgentStep(type="assistant", content="team member done", kind="final")
        user_text = next(
            (
                str(message.get("content", ""))
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        path = next(
            (
                token
                for token in user_text.split()
                if token.endswith(".txt")
            ),
            "team-member.txt",
        )
        return AgentStep(
            type="tool_calls",
            calls=[
                ToolCall(
                    id=f"write-{path}",
                    toolName="write_file",
                    input={"path": path, "content": f"created {path}\n"},
                )
            ],
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@unittest.skipUnless(shutil.which("git"), "需要安装 git")
class CollaborationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="minicode-collab-"))
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test User")
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(self.repo, "add", "app.py")
        _git(self.repo, "commit", "-m", "initial")
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _manager(self, model, *, fork_context=None) -> SubAgentManager:
        root = create_root_registry(cwd=str(self.repo))
        manager = SubAgentManager(
            model=model,
            tools=create_worker_registry(root),
            coder_tools=create_coder_registry(root),
            cwd=str(self.repo),
        )
        if fork_context is not None:
            manager.set_fork_context_provider(fork_context)
        return manager


class WorktreeModeTests(CollaborationTestCase):
    def test_worktree_is_isolated_until_inspected_and_applied(self) -> None:
        manager = self._manager(_WriteAdapter())
        snapshot = manager.spawn(
            "Add agent.txt",
            mode=MODE_WORKTREE,
            role=ROLE_IMPLEMENTER,
            acceptance="agent.txt exists and contains worker text",
        )
        result = manager.wait([snapshot.id], timeout_ms=15_000)
        self.assertFalse(result.timed_out)
        done = result.agents[0]
        self.assertEqual(done.status, "completed", done.error)
        self.assertTrue(done.worktree_path)
        self.assertTrue(done.patch_available)
        self.assertFalse((self.repo / "agent.txt").exists(), "合并前根工作区不能变化")

        inspected = manager.inspect_changes(snapshot.id)
        self.assertIn("agent.txt", inspected["patch"])
        manager.apply_changes(snapshot.id)
        self.assertEqual(
            (self.repo / "agent.txt").read_text(encoding="utf-8"),
            "from worker\n",
        )
        repeated = manager.apply_changes(snapshot.id)
        self.assertTrue(repeated["already_applied"])

        cleaned = manager.remove_worktree(snapshot.id)
        self.assertTrue(cleaned.worktree_removed)
        self.assertFalse(Path(cleaned.worktree_path).exists())

    def test_apply_requires_a_prior_inspection(self) -> None:
        manager = self._manager(_WriteAdapter("agent.txt", "x\n"))
        snapshot = manager.spawn(
            "Write file",
            mode=MODE_WORKTREE,
            role=ROLE_IMPLEMENTER,
            acceptance="file exists",
        )
        manager.wait([snapshot.id], timeout_ms=15_000)
        with self.assertRaises(SubAgentError) as caught:
            manager.apply_changes(snapshot.id)
        self.assertIn("inspect_agent_changes", str(caught.exception))
        manager.close(snapshot.id, delete_worktree=True)

    def test_conflicting_root_change_is_rejected_without_partial_apply(self) -> None:
        manager = self._manager(_WriteAdapter("app.py", "VALUE = 10\n"))
        snapshot = manager.spawn(
            "Change app.py",
            mode=MODE_WORKTREE,
            role=ROLE_IMPLEMENTER,
            acceptance="app.py contains worker value",
        )
        manager.wait([snapshot.id], timeout_ms=15_000)
        manager.inspect_changes(snapshot.id)

        (self.repo / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        with self.assertRaises(SubAgentError):
            manager.apply_changes(snapshot.id)
        self.assertEqual(
            (self.repo / "app.py").read_text(encoding="utf-8"),
            "VALUE = 99\n",
        )
        manager.close(snapshot.id, delete_worktree=True)

    def test_worktree_requires_acceptance(self) -> None:
        manager = self._manager(_ReportAdapter())
        with self.assertRaises(SubAgentError):
            manager.spawn("Change code", mode=MODE_WORKTREE)

    def test_worktree_rejects_a_dirty_root_to_avoid_stale_base(self) -> None:
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        manager = self._manager(_ReportAdapter())
        with self.assertRaises(SubAgentError) as caught:
            manager.spawn(
                "Change code",
                mode=MODE_WORKTREE,
                acceptance="tests pass",
            )
        self.assertIn("未提交", str(caught.exception))

    def test_worker_cannot_escape_or_run_arbitrary_shell(self) -> None:
        worktree = self.home / "fake-worktree"
        worktree.mkdir()
        outside = self.root / "outside.txt"
        permissions = WorkerPermissionManager(str(worktree))
        with self.assertRaises(PermissionDenied):
            permissions.ensure_path_access(str(outside))
        with self.assertRaises(PermissionDenied):
            permissions.ensure_command(
                "bash",
                ["-lc", "rm -rf ."],
                str(worktree),
            )
        permissions.ensure_command("pytest", ["-q"], str(worktree))
        permissions.ensure_command("python", ["-m", "pytest", "-q"], str(worktree))
        permissions.ensure_command("npm", ["run", "test"], str(worktree))
        with self.assertRaises(PermissionDenied):
            permissions.ensure_command("npm", ["run", "postinstall"], str(worktree))
        with self.assertRaises(PermissionDenied):
            permissions.ensure_command("pytest", [str(outside)], str(worktree))
        with self.assertRaises(PermissionDenied):
            permissions.ensure_command(
                "git",
                [f"--output={outside}", "diff"],
                str(worktree),
            )


class ForkModeTests(CollaborationTestCase):
    def test_fork_receives_immutable_text_snapshot(self) -> None:
        adapter = _CaptureAdapter()
        manager = self._manager(
            adapter,
            fork_context=lambda: [
                {"role": "system", "content": "SHOULD NOT FORK SYSTEM"},
                {"role": "user", "content": "EARLIER USER CONTEXT"},
                {"role": "assistant", "content": "EARLIER ASSISTANT CONTEXT"},
                {"role": "tool_result", "content": "SHOULD NOT FORK TOOL RESULT"},
            ],
        )
        snapshot = manager.spawn(
            "Analyze the earlier conversation",
            mode=MODE_FORK,
            role=ROLE_INVESTIGATOR,
        )
        manager.wait([snapshot.id], timeout_ms=10_000)
        self.assertTrue(adapter.seen)
        contents = "\n".join(
            str(message.get("content", "")) for message in adapter.seen[0]
        )
        self.assertIn("EARLIER USER CONTEXT", contents)
        self.assertIn("EARLIER ASSISTANT CONTEXT", contents)
        self.assertNotIn("SHOULD NOT FORK", contents)

    def test_fork_without_provider_is_rejected(self) -> None:
        manager = self._manager(_ReportAdapter())
        with self.assertRaises(SubAgentError):
            manager.spawn("fork me", mode=MODE_FORK)


class TeamModeTests(CollaborationTestCase):
    def test_team_is_a_flat_group_of_workers(self) -> None:
        manager = self._manager(_ReportAdapter())
        team_id, members = manager.spawn_team(
            "audit",
            [
                {
                    "task": "Inspect A",
                    "mode": "research",
                    "role": "investigator",
                },
                {
                    "task": "Inspect B",
                    "mode": "research",
                    "role": "reviewer",
                },
            ],
        )
        result = manager.wait([item.id for item in members], timeout_ms=10_000)
        self.assertFalse(result.timed_out)
        self.assertEqual({item.team_id for item in result.agents}, {team_id})
        self.assertEqual({item.team_name for item in result.agents}, {"audit"})
        self.assertEqual({item.status for item in result.agents}, {"completed"})

    def test_spawn_team_tool_reports_group_metadata(self) -> None:
        manager = self._manager(_ReportAdapter())
        tools = {tool.name: tool for tool in create_subagent_tools(manager)}
        result = tools["spawn_team"].run(
            {
                "name": "tool-team",
                "members": [
                    {"task": "one"},
                    {"task": "two", "role": "reviewer"},
                ],
            },
            ToolContext(cwd=str(self.repo)),
        )
        self.assertTrue(result.ok, result.output)
        self.assertIn("team_1", result.output)
        self.assertIn("tool-team", result.output)
        manager.close_all()

    def test_parallel_worktree_team_merges_independent_files(self) -> None:
        manager = self._manager(_TaskWriteAdapter())
        team_id, members = manager.spawn_team(
            "parallel coding",
            [
                {
                    "task": "write alpha.txt",
                    "mode": MODE_WORKTREE,
                    "role": ROLE_IMPLEMENTER,
                    "acceptance": "alpha.txt exists",
                },
                {
                    "task": "write beta.txt",
                    "mode": MODE_WORKTREE,
                    "role": ROLE_IMPLEMENTER,
                    "acceptance": "beta.txt exists",
                },
            ],
        )
        result = manager.wait([item.id for item in members], timeout_ms=20_000)
        self.assertFalse(result.timed_out)
        self.assertEqual({item.team_id for item in result.agents}, {team_id})
        self.assertFalse((self.repo / "alpha.txt").exists())
        self.assertFalse((self.repo / "beta.txt").exists())

        for item in result.agents:
            manager.inspect_changes(item.id)
            manager.apply_changes(item.id)
            manager.close(item.id, delete_worktree=True)
        self.assertTrue((self.repo / "alpha.txt").is_file())
        self.assertTrue((self.repo / "beta.txt").is_file())


class ApprovalGateTests(CollaborationTestCase):
    def test_apply_tool_fails_when_user_approval_is_denied(self) -> None:
        manager = self._manager(_WriteAdapter())
        tools = {tool.name: tool for tool in create_subagent_tools(manager)}
        snapshot = manager.spawn(
            "Write file",
            mode=MODE_WORKTREE,
            role=ROLE_IMPLEMENTER,
            acceptance="file exists",
        )
        manager.wait([snapshot.id], timeout_ms=15_000)
        inspected = tools["inspect_agent_changes"].run(
            {"agent_id": snapshot.id},
            ToolContext(cwd=str(self.repo)),
        )
        self.assertTrue(inspected.ok, inspected.output)

        class DenyPermissions:
            def ensure_command(self, *_args, **_kwargs):
                raise PermissionDenied("denied by user")

        denied = tools["apply_agent_changes"].run(
            {"agent_id": snapshot.id},
            ToolContext(cwd=str(self.repo), permissions=DenyPermissions()),
        )
        self.assertFalse(denied.ok)
        self.assertIn("审批未通过", denied.output)
        self.assertFalse((self.repo / "agent.txt").exists())
        manager.close(snapshot.id, delete_worktree=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
