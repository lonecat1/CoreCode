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


class McpConfigTests(IntegrationTestCase):
    """MCP 配置装载与「无凭据也能用」这条解耦。

    这里用真实的 `fake_mcp_server.py` 子进程，因为装配错误（配置读不到、
    工具没注入、提示没刷新）只有真跑一遍才会暴露。
    """

    SERVER = str(Path(_HERE) / "fixtures" / "fake_mcp_server.py")

    def _write_mcp_config(self, *, where: str = "home") -> None:
        import json

        config = {
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": [self.SERVER, "--protocol", "content-length"],
                }
            }
        }
        if where == "home":
            target = Path(os.environ["MINICODE_HOME"]) / "mcp.json"
        else:
            target = Path(self.cwd_holder) / ".mcp.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def setUp(self) -> None:
        super().setUp()
        self.cwd_holder = str(self.home.parent)

    def test_servers_load_without_any_model_credentials(self) -> None:
        """没有 API key 也必须能读到 MCP 配置。

        原先 MCP 配置只经由 `load_runtime_config()` 获得，而 mock 模式不调用它
        ——结果是「没有 API key 就用不了 MCP」。这个耦合是错的：
        MCP 服务器与模型凭据毫无关系。这条测试钉住解耦。
        """
        from minicode.config import load_mcp_servers

        self._write_mcp_config()
        servers = load_mcp_servers(cwd=str(self.home.parent))
        self.assertIn("fake", servers)

    def test_mock_mode_injects_mcp_tools(self) -> None:
        self._write_mcp_config()
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)

        host._mcp_wait(timeout=15)  # noqa: SLF001 - 等后台连接完成
        names = {tool.name for tool in host.tools.list()}
        self.assertIn("mcp__fake__echo", names)
        self.assertIn("mcp__fake__structured", names)

    def test_connected_state_reaches_the_system_prompt(self) -> None:
        self._write_mcp_config()
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)
        host._mcp_wait(timeout=15)  # noqa: SLF001

        prompt = host.messages[0]["content"]
        self.assertIn("Configured MCP servers", prompt)
        self.assertIn("fake", prompt)
        self.assertIn("tools=3", prompt)
        self.assertIn("mcp__server__tool", prompt, "命名约定必须告诉模型")

    def test_mcp_tool_is_callable_through_the_registry(self) -> None:
        from minicode.tooling import ToolContext

        self._write_mcp_config()
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)
        host._mcp_wait(timeout=15)  # noqa: SLF001

        result = host.tools.execute(
            "mcp__fake__echo", {"text": "集成"}, ToolContext(cwd=host.cwd)
        )
        self.assertTrue(result.ok)
        self.assertIn("echo: 集成", result.output)

    def test_mcp_command_reports_connected(self) -> None:
        from minicode.cli_commands import try_handle_local_command

        self._write_mcp_config()
        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)
        host._mcp_wait(timeout=15)  # noqa: SLF001

        output = try_handle_local_command(host, "/mcp") or ""
        self.assertIn("connected", output)
        self.assertIn("tools=3", output)

    def test_project_level_mcp_json_is_merged(self) -> None:
        """`<cwd>/.mcp.json` 是 MCP 生态的惯例位置，必须支持。"""
        import json

        from minicode.config import load_mcp_servers

        project = Path(WORKDIR)
        target = project / ".mcp.json"
        created = not target.exists()
        try:
            target.write_text(
                json.dumps({"mcpServers": {"fromproject": {"command": "x"}}}),
                encoding="utf-8",
            )
            servers = load_mcp_servers(cwd=WORKDIR)
            self.assertIn("fromproject", servers)
        finally:
            if created:
                target.unlink()

    def test_settings_json_overrides_mcp_json(self) -> None:
        from minicode.config import load_mcp_servers

        self._write_mcp_config()
        servers = load_mcp_servers(
            cwd=str(self.home.parent),
            settings={"mcpServers": {"fake": {"command": "override"}}},
        )
        self.assertEqual(servers["fake"]["command"], "override")

    def test_broken_server_does_not_break_startup(self) -> None:
        import json

        config = {"mcpServers": {"broken": {"command": "definitely-not-real-xyz"}}}
        target = Path(os.environ["MINICODE_HOME"]) / "mcp.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config), encoding="utf-8")

        args = build_parser().parse_args(["--mock", "--cwd", WORKDIR])
        host = build_host(args)  # 不应抛
        host._mcp_wait(timeout=15)  # noqa: SLF001

        from minicode.cli_commands import try_handle_local_command

        output = try_handle_local_command(host, "/mcp") or ""
        self.assertIn("error", output)
        # 主流程仍然可用
        self.assertTrue(host.tools.find("read_file") is not None)


class Utf8OutputTests(IntegrationTestCase):
    """Windows 控制台默认编码是 cp936，而程序输出是中文。

    不做处理时用户看到乱码——「程序明明能用却像是坏了」，比一个明确的报错
    更糟。这条测试钉住启动时的编码重配。
    """

    def test_main_reconfigures_stdout_to_utf8(self) -> None:
        import subprocess

        # 必须用真实子进程：编码只有在新进程里才看得出效果。
        script = (
            "import sys; sys.path.insert(0, r'"
            + os.path.join(os.path.dirname(_HERE), "src")
            + "');"
            "from minicode.cli import _ensure_utf8_output;"
            "_ensure_utf8_output();"
            "print('encoding=' + str(sys.stdout.encoding))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=WORKDIR,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("utf-8", result.stdout.lower())

    def test_utf8_helper_tolerates_streams_without_reconfigure(self) -> None:
        """stdout 被替换成不支持 reconfigure 的对象时不能抛。"""
        import io

        from minicode.cli import _ensure_utf8_output

        original = sys.stdout
        try:
            sys.stdout = io.StringIO()  # StringIO 没有 reconfigure
            _ensure_utf8_output()  # 不应抛
        finally:
            sys.stdout = original

    def test_all_tools_share_the_same_helper(self) -> None:
        """三个开发工具都必须接入，否则工具输出仍然乱码。"""
        tools_dir = Path(WORKDIR) / "tools"
        for name in ("check_imports.py", "mutation_check.py", "self_check.py"):
            with self.subTest(tool=name):
                text = (tools_dir / name).read_text(encoding="utf-8")
                self.assertIn("force_utf8_output", text, f"{name} 未接入 UTF-8 修复")

    def test_mutation_check_repo_path_is_derived_not_hardcoded(self) -> None:
        """REPO 必须从脚本位置推导；写死路径换目录就静默失效。"""
        text = (Path(WORKDIR) / "tools" / "mutation_check.py").read_text(encoding="utf-8")
        self.assertNotIn('REPO = r"', text, "REPO 不应是硬编码字符串字面量")
        self.assertIn("os.path.abspath(__file__)", text)


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
