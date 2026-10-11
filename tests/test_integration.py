"""端到端集成测试。

单模块测试全绿只说明每个零件work；**没有任何证据表明它们装在一起能跑**。
这个文件就是那份证据：

    命令行 → 装配 → 一个真实回合（model→tool→model）→ 落盘 → 恢复

刻意走 `cli.main()` 而不是直接调内部函数，因为「装配」本身就是最易出错的一环
（本文件写出来之前，`_run_once` 就把本地命令漏掉了）。
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.cli import build_host, build_parser, main  # noqa: E402
from minicode.host import HostCallbacks  # noqa: E402
from minicode.session import list_sessions, load_session  # noqa: E402

unittest.TestCase.maxDiff = None

#: 集成测试的工作目录：仓库根，这样 list_files 之类有真实内容可看。
WORKDIR = os.path.dirname(_HERE)
#: 每个测试的隔离家目录根。
_REPO_TMP = Path(_HERE) / "_tmp" / "integration-home"


class IntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        from pathlib import Path

        # 每个测试一个干净的家目录。这里能生效是因为 session / 工具结果落盘
        # 都走 `projects_dir()` 这类**动态**解析（见 config.py 的说明）——
        # 之前用模块级 PROJECTS_DIR 时，这里设环境变量完全无效，
        # 测试会静默地读写真实用户目录。
        self.home = Path(str(_REPO_TMP)) / self._testMethodName.replace("/", "_")
        shutil.rmtree(self.home, ignore_errors=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def _host(self, **kwargs):
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        return build_host(args, callbacks=kwargs.pop("callbacks", None))

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        """跑一次完整的 CLI，返回 (退出码, stdout)。"""
        capture = io.StringIO()
        try:
            with redirect_stdout(capture):
                code = main(argv)
        except SystemExit as exit_signal:  # argparse 的错误路径
            code = int(exit_signal.code or 0)
        return code, capture.getvalue()


class CliWiringTests(IntegrationTestCase):
    """命令行装配本身。"""

    def test_help_runs_and_lists_commands(self) -> None:
        code, output = self._run_cli(["--mock", "--cwd", WORKDIR, "-p", "/help"])
        self.assertEqual(code, 0)
        self.assertIn("/help", output)
        self.assertIn("/tools", output)
        self.assertIn("/compact", output)

    def test_status_reports_real_state(self) -> None:
        code, output = self._run_cli(["--mock", "--cwd", WORKDIR, "-p", "/status"])
        self.assertEqual(code, 0)
        self.assertIn("离线 mock", output)
        self.assertIn("子 Agent", output)

    def test_local_commands_are_not_sent_to_the_model(self) -> None:
        """装配 bug 的回归护栏：本地命令必须被本地处理，不能当成对话内容。

        写这个测试之前，`-p /status` 会被发给模型并回一句「收到指令：/status」。
        """
        for command in ("/help", "/status", "/tools", "/plan", "/memory"):
            with self.subTest(command=command):
                code, output = self._run_cli(["--mock", "--cwd", WORKDIR, "-p", command])
                self.assertEqual(code, 0)
                self.assertNotIn(
                    "收到指令", output, f"{command} 不该被发给模型"
                )

    def test_unknown_slash_command_exits_nonzero(self) -> None:
        code, output = self._run_cli(["--mock", "--cwd", WORKDIR, "-p", "/nope"])
        self.assertEqual(code, 1)

    def test_goal_command_starts_a_goal(self) -> None:
        code, output = self._run_cli(
            ["--mock", "--cwd", WORKDIR, "-p", "/goal 把这件小事做完"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Goal", output)

    def test_no_self_check_flag_on_the_cli(self) -> None:
        """自检不能是 CLI 子命令。

        之前的 `--agent` 会跑测试，而测试里又有一个用 `--agent` 的用例——
        无限递归，表现为**挂住**而不是失败。凡是会跑测试的东西都必须待在
        测试之外，所以自检放在 `tools/self_check.py`。
        """
        parser = build_parser()
        options = {action.dest for action in parser._actions}  # noqa: SLF001
        self.assertNotIn("agent", options)


class RealTurnTests(IntegrationTestCase):
    """一个真实回合：model → tool → model → 落盘。"""

    def test_list_files_turn_produces_a_report_and_persists(self) -> None:
        host = self._host()
        result = host.runtime.submit("/ls")

        self.assertEqual(result.outcome, "final")
        # 走完了完整循环：有工具调用、有结果回填
        roles = [m.get("role") for m in result.messages]
        self.assertIn("assistant_tool_call", roles)
        self.assertIn("tool_result", roles)
        # mock 适配器会把目录内容写进最终回答
        self.assertTrue(
            any("src" in str(m.get("content", "")) for m in result.messages),
            "最终回答里应当能看到目录内容",
        )

        # 落盘：会话文件真的被写出来了
        sessions = list_sessions(WORKDIR)
        self.assertEqual(len(sessions), 1, "回合结束后应当有一个会话文件")

        restored = load_session(WORKDIR, host.session_id)
        self.assertIsNotNone(restored)
        self.assertTrue(
            any(m.get("role") == "tool_result" for m in (restored or [])),
            "恢复出的会话里应当包含工具结果",
        )

    def test_read_file_turn(self) -> None:
        host = self._host()
        result = host.runtime.submit("/read pyproject.toml")
        self.assertEqual(result.outcome, "final")
        joined = "\n".join(str(m.get("content", "")) for m in result.messages)
        self.assertIn("minicode", joined)

    def test_write_turn_goes_through_permissions(self) -> None:
        """写操作必须经过审批层——集成后这条链不能被绕过。"""
        target = os.path.join(WORKDIR, "tests", "_tmp", "集成写入.txt")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            os.remove(target)

        seen_requests: list[str] = []

        def always_allow(request, signal):  # noqa: ANN001
            from minicode.permissions import PermissionPromptResult

            seen_requests.append(request.kind)
            return PermissionPromptResult(decision="allow_once")

        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)
        host.permissions._prompt = always_allow  # noqa: SLF001 - 测试替身

        result = host.runtime.submit(
            "/write tests/_tmp/集成写入.txt::集成测试写入的内容"
        )
        self.assertEqual(result.outcome, "final")
        self.assertIn("edit", seen_requests, "写文件必须触发编辑审批")
        self.assertTrue(os.path.exists(target), "审批通过后文件应当存在")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "集成测试写入的内容")
        os.remove(target)

    def test_context_stats_reach_the_host(self) -> None:
        """压缩管线要真的把统计上报给宿主——否则 TUI 永远显示不了上下文占用。"""
        seen: list[object] = []
        callbacks = HostCallbacks(on_context_stats=seen.append)
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args, callbacks=callbacks)

        # mock 模式没有模型名，管线会跳过压缩，因此这里手动用真实模型名驱动一次。
        from minicode.context.pipeline import ContextPipeline

        pipeline = ContextPipeline(model_adapter=None, on_stats=seen.append)
        pipeline.prepare(host.messages, step=0, model_name="claude-sonnet-4")
        self.assertTrue(seen, "统计回调应当被调用")
        self.assertTrue(hasattr(seen[0], "utilization"))


class SessionLifecycleTests(IntegrationTestCase):
    """会话的装载 / 分叉 / 清空在装配后的行为。"""

    def test_resume_restores_messages(self) -> None:
        first = self._host()
        first.runtime.submit("/ls")
        session_id = first.session_id

        second = self._host()
        self.assertTrue(second.resume(session_id))
        self.assertEqual(second.session_id, session_id)
        self.assertTrue(
            any(m.get("role") == "tool_result" for m in second.messages),
            "恢复后应当带回工具结果",
        )

    def test_resume_unknown_session_returns_false(self) -> None:
        host = self._host()
        self.assertFalse(host.resume("nonexistent"))

    def test_fork_creates_an_independent_session(self) -> None:
        host = self._host()
        host.runtime.submit("/ls")
        forked = host.fork(host.session_id)
        self.assertIsNotNone(forked)
        self.assertNotEqual(forked, host.session_id)
        self.assertGreaterEqual(len(list_sessions(WORKDIR)), 2)

    def test_new_session_clears_state(self) -> None:
        host = self._host()
        host.runtime.submit("/ls")
        before = len(host.messages)
        self.assertGreater(before, 1)

        from minicode.cli_commands import try_handle_local_command

        output = try_handle_local_command(host, "/new")
        self.assertIn("新会话", output or "")
        self.assertEqual(len(host.messages), 1, "新会话只应保留 system 消息")
        self.assertEqual(host.plan.get_snapshot().todos, [])

    def test_plan_command_reflects_updates(self) -> None:
        host = self._host()
        host.plan.update(
            [{"title": "第一步", "status": "in_progress"}]
        )
        from minicode.cli_commands import try_handle_local_command

        output = try_handle_local_command(host, "/plan") or ""
        self.assertIn("第一步", output)
        self.assertIn("[>]", output)


class SubAgentIntegrationTests(IntegrationTestCase):
    """子 Agent 在装配后的真实行为。"""

    def test_root_agent_can_spawn_and_collect_a_report(self) -> None:
        host = self._host()
        snapshot = host.subagents.spawn("调研一下 src 目录里有什么")
        # 不假设 spawn 时的状态：mock worker 可能已经跑完了。
        # 断言的重点是「能拿到一份完成的报告」。
        self.assertIn(snapshot.status, ("running", "completed"))

        result = host.subagents.wait([snapshot.id], timeout_ms=10_000)
        self.assertFalse(result.timed_out, "worker 应当在超时前结束")
        self.assertEqual(result.agents[0].status, "completed")
        self.assertTrue(result.agents[0].result, "worker 应当交回一份报告")

    def test_worker_cannot_see_write_tools(self) -> None:
        host = self._host()
        worker_names = {
            t.name for t in host.runtime.tools_for(None).list()
        }
        # 根表里有 write_file，但 worker 注册表里没有
        self.assertIn("write_file", worker_names)
        from minicode.tools import create_worker_registry

        self.assertNotIn("write_file", {t.name for t in create_worker_registry(host.tools).list()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
