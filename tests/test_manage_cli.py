"""管理子命令（`minicode mcp ...` / `minicode skills ...`）的测试。

对应原版 `manage-cli.ts`（290 行）。

## 为什么这些测试重要

这是**唯一会改配置文件**的入口。斜杠命令（`/mcp`、`/skills`）只能看，
要改配置必须走这里。因此它出错的方式是「用户的配置被改坏了」——比崩溃更糟。

三类要钉住的性质：

**① 作用域不会串。** `--project` 写 `<cwd>/.mcp.json`，默认写
`<MINICODE_HOME>/mcp.json`。写错位置意味着项目配置进了用户目录（或反过来），
两者都会被静默地当成「配置没生效」。

**② `--` 之后不是本命令的参数。** `mcp add fs -- npx -y pkg` 里的 `-y` 属于
要启动的命令，不属于 `minicode`。解析器必须在这里切换语义。

**③ 失败要明确。** 同时给 `--url` 和本地命令、给了 `--protocol streamable-http`
却没给 `--url`——这些组合一定起不来，提前挡住比让它连不上再排查好。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.config import (  # noqa: E402
    load_scoped_mcp_servers,
    project_mcp_path,
    read_mcp_tokens_file,
)
from minicode.manage_cli import (  # noqa: E402
    USAGE,
    ManageError,
    handle_mcp_command,
    handle_skills_command,
    maybe_handle_management_command,
    parse_env_pairs,
    parse_scope,
    print_usage,
    take_option,
    take_repeat_option,
)


class ManageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="minicode-manage-home-"))
        self.cwd = Path(tempfile.mkdtemp(prefix="minicode-manage-cwd-"))
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.cwd, ignore_errors=True)

    def run_cmd(self, func, *args) -> tuple[bool, str]:
        """跑一个子命令，捕获 stdout。"""
        capture = io.StringIO()
        with redirect_stdout(capture):
            handled = func(str(self.cwd), list(args))
        return handled, capture.getvalue()

    def user_mcp_file(self) -> Path:
        return self.home / "mcp.json"

    def project_mcp_file(self) -> Path:
        return project_mcp_path(str(self.cwd))


class ScopeParsingTests(unittest.TestCase):
    def test_default_scope_is_user(self) -> None:
        scope, rest = parse_scope(["list"])
        self.assertEqual(scope, "user")
        self.assertEqual(rest, ["list"])

    def test_project_flag_switches_scope(self) -> None:
        scope, rest = parse_scope(["--project", "list"])
        self.assertEqual(scope, "project")
        self.assertEqual(rest, ["list"])

    def test_project_flag_is_removed_from_rest(self) -> None:
        """**就地移除**，否则子命令会把它当成一个多余的位置参数。"""
        _scope, rest = parse_scope(["add", "--project", "fs"])
        self.assertNotIn("--project", rest)
        self.assertEqual(rest, ["add", "fs"])

    def test_no_args(self) -> None:
        self.assertEqual(parse_scope([]), ("user", []))


class OptionParsingTests(unittest.TestCase):
    def test_take_option_removes_it(self) -> None:
        args = ["--url", "https://x", "left"]
        self.assertEqual(take_option(args, "--url"), "https://x")
        self.assertEqual(args, ["left"])

    def test_take_option_absent_returns_none(self) -> None:
        args = ["a"]
        self.assertIsNone(take_option(args, "--url"))
        self.assertEqual(args, ["a"])

    def test_take_option_missing_value_raises(self) -> None:
        with self.assertRaises(ManageError):
            take_option(["--url"], "--url")

    def test_take_option_empty_value_raises(self) -> None:
        """`--url ""` 是明显的用法错误，不该被当成「给了空 URL」。"""
        with self.assertRaises(ManageError):
            take_option(["--url", ""], "--url")

    def test_take_repeat_option_collects_all(self) -> None:
        args = ["--env", "A=1", "--env", "B=2", "tail"]
        self.assertEqual(take_repeat_option(args, "--env"), ["A=1", "B=2"])
        self.assertEqual(args, ["tail"])

    def test_take_repeat_option_none(self) -> None:
        self.assertEqual(take_repeat_option(["a"], "--env"), [])

    def test_take_repeat_option_missing_value_raises(self) -> None:
        with self.assertRaises(ManageError):
            take_repeat_option(["--env"], "--env")


class EnvPairTests(unittest.TestCase):
    def test_parses_pairs(self) -> None:
        self.assertEqual(parse_env_pairs(["A=1", "B=2"]), {"A": 1 and "1", "B": "2"})

    def test_splits_on_first_equals_only(self) -> None:
        """**值里可能含 `=`**（连接串、base64）。按所有 `=` 分会静默截断值。"""
        self.assertEqual(
            parse_env_pairs(["TOKEN=a=b=c"]), {"TOKEN": "a=b=c"}
        )

    def test_value_may_be_empty(self) -> None:
        self.assertEqual(parse_env_pairs(["KEY="]), {"KEY": ""})

    def test_key_is_trimmed(self) -> None:
        self.assertEqual(parse_env_pairs(["  KEY  =v"]), {"KEY": "v"})

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ManageError):
            parse_env_pairs(["NOEQUALS"])

    def test_empty_key_raises(self) -> None:
        with self.assertRaises(ManageError):
            parse_env_pairs(["=v"])

    def test_empty_list(self) -> None:
        self.assertEqual(parse_env_pairs([]), {})


class UsageTests(unittest.TestCase):
    def test_usage_lists_all_subcommands(self) -> None:
        for fragment in (
            "minicode mcp list",
            "minicode mcp add",
            "minicode mcp login",
            "minicode mcp logout",
            "minicode mcp remove",
            "minicode skills list",
            "minicode skills add",
            "minicode skills remove",
        ):
            self.assertIn(fragment, USAGE)

    def test_print_usage_writes_it(self) -> None:
        capture = io.StringIO()
        with redirect_stdout(capture):
            print_usage()
        self.assertIn("minicode mcp list", capture.getvalue())


class McpListTests(ManageTestCase):
    def test_empty_names_the_config_path(self) -> None:
        """报「没有服务器」时**必须带上路径**。

        不带路径时，用户看到「没有配置」却不知道该去哪个文件里加。
        """
        _, output = self.run_cmd(handle_mcp_command, "list")
        self.assertIn("No MCP servers configured", output)
        self.assertIn(str(self.user_mcp_file()), output)

    def test_lists_url_server_with_protocol(self) -> None:
        self.run_cmd(
            handle_mcp_command,
            "add",
            "remote",
            "--url",
            "https://example.com/mcp",
            "--protocol",
            "streamable-http",
        )
        _, output = self.run_cmd(handle_mcp_command, "list")
        self.assertIn("remote: https://example.com/mcp", output)
        self.assertIn("protocol=streamable-http", output)

    def test_lists_command_server_with_args(self) -> None:
        self.run_cmd(
            handle_mcp_command,
            "add",
            "fs",
            "--",
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "/tmp",
        )
        _, output = self.run_cmd(handle_mcp_command, "list")
        self.assertIn(
            "fs: npx -y @modelcontextprotocol/server-filesystem /tmp", output
        )

    def test_command_server_without_args(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "solo", "--", "my-server")
        _, output = self.run_cmd(handle_mcp_command, "list")
        self.assertIn("solo: my-server", output)

    def test_project_scope_lists_from_project_file(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "p", "--", "cmd", "--project")
        _, output = self.run_cmd(handle_mcp_command, "list", "--project")
        self.assertIn("p: cmd", output)

    def test_empty_list_names_the_project_path(self) -> None:
        """**只在空列表时**才报配置路径（原版如此）。

        有内容时逐条列出条目即可——路径是固定的，每条都印一遍是噪音。
        空列表时路径才是关键信息：用户要知道该去哪个文件里加。
        """
        _, output = self.run_cmd(handle_mcp_command, "list", "--project")
        self.assertIn("No MCP servers configured", output)
        self.assertIn(str(self.project_mcp_file()), output)

    def test_scopes_are_independent(self) -> None:
        """**作用域不会串。** 用户级与项目级各自独立。"""
        self.run_cmd(handle_mcp_command, "add", "user-only", "--", "a")
        self.run_cmd(handle_mcp_command, "add", "proj-only", "--", "b", "--project")

        _, user_out = self.run_cmd(handle_mcp_command, "list")
        _, proj_out = self.run_cmd(handle_mcp_command, "list", "--project")

        self.assertIn("user-only", user_out)
        self.assertNotIn("proj-only", user_out)
        self.assertIn("proj-only", proj_out)
        self.assertNotIn("user-only", proj_out)


class McpAddTests(ManageTestCase):
    def test_add_url_server_writes_to_user_path(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "r", "--url", "https://x/mcp")
        self.assertTrue(self.user_mcp_file().exists())
        self.assertFalse(self.project_mcp_file().exists())
        served = json.loads(self.user_mcp_file().read_text(encoding="utf-8"))
        self.assertEqual(served["mcpServers"]["r"]["url"], "https://x/mcp")

    def test_add_project_server_writes_to_project_path(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "r", "--url", "https://x", "--project")
        self.assertTrue(self.project_mcp_file().exists())
        self.assertFalse(self.user_mcp_file().exists())

    def test_add_command_server(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "fs", "--", "npx", "-y", "pkg", "/tmp")
        servers = load_scoped_mcp_servers("user", str(self.cwd))
        self.assertEqual(servers["fs"]["command"], "npx")
        self.assertEqual(servers["fs"]["args"], ["-y", "pkg", "/tmp"])

    def test_empty_values_are_omitted(self) -> None:
        """可选字段为空时省略：配置文件是给人看的，一堆空字段掩盖真正有意义的那几个。

        **例外是 `command`**：远端型服务器也会写出 `"command": ""`。这看着冗余，
        但与**原版一致**且无害——transport 的选择是「有 `url` 就走 HTTP」，
        空 command 永远走不到 stdio 分支（那一分支另外会抛明确的错误）。
        """
        self.run_cmd(handle_mcp_command, "add", "fs", "--", "cmd")
        entry = load_scoped_mcp_servers("user", str(self.cwd))["fs"]
        self.assertNotIn("args", entry)
        self.assertNotIn("env", entry)
        self.assertNotIn("url", entry)
        self.assertNotIn("headers", entry)
        self.assertNotIn("protocol", entry)

    def test_url_server_writes_empty_command(self) -> None:
        """与原版一致：远端型服务器留一个空 `command` 字段。"""
        self.run_cmd(handle_mcp_command, "add", "r", "--url", "https://x")
        entry = load_scoped_mcp_servers("user", str(self.cwd))["r"]
        self.assertEqual(entry["command"], "")
        self.assertEqual(entry["url"], "https://x")

    def test_add_with_env_and_headers(self) -> None:
        self.run_cmd(
            handle_mcp_command,
            "add",
            "s",
            "--url",
            "https://x",
            "--env",
            "A=1",
            "--env",
            "B=2",
            "--header",
            "Authorization=Bearer t",
        )
        entry = load_scoped_mcp_servers("user", str(self.cwd))["s"]
        self.assertEqual(entry["env"], {"A": "1", "B": "2"})
        self.assertEqual(entry["headers"], {"Authorization": "Bearer t"})

    def test_existing_servers_are_preserved(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "one", "--", "a")
        self.run_cmd(handle_mcp_command, "add", "two", "--", "b")
        servers = load_scoped_mcp_servers("user", str(self.cwd))
        self.assertEqual(set(servers), {"one", "two"})

    def test_adding_same_name_overwrites(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "s", "--", "old")
        self.run_cmd(handle_mcp_command, "add", "s", "--", "new")
        servers = load_scoped_mcp_servers("user", str(self.cwd))
        self.assertEqual(servers["s"]["command"], "new")

    def test_missing_name_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "add")

    def test_url_and_command_together_raises(self) -> None:
        """**两者互斥**：远端端点与本地命令是两种不同的服务器。"""
        with self.assertRaises(ManageError) as caught:
            self.run_cmd(handle_mcp_command, "add", "s", "--url", "https://x", "--", "cmd")
        self.assertIn("Cannot set both", str(caught.exception))

    def test_neither_url_nor_command_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "add", "s")

    def test_streamable_http_requires_url(self) -> None:
        """这个组合**一定起不来**，提前挡住比让它连不上再排查好。"""
        with self.assertRaises(ManageError) as caught:
            self.run_cmd(
                handle_mcp_command, "add", "s", "--protocol", "streamable-http", "--", "cmd"
            )
        self.assertIn("requires --url", str(caught.exception))

    def test_unknown_protocol_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(
                handle_mcp_command, "add", "s", "--protocol", "nonsense", "--url", "https://x"
            )

    def test_bogus_option_is_rejected(self) -> None:
        """**就地移除已知选项**让这一步可靠：剩下的就是真正多余的参数。

        用户拼错选项名时若不报错，他会以为设置生效了。
        """
        with self.assertRaises(ManageError) as caught:
            self.run_cmd(handle_mcp_command, "add", "s", "--url", "https://x", "--typo", "v")
        self.assertIn("Unknown arguments", str(caught.exception))

    def test_double_dash_stops_option_parsing(self) -> None:
        """`--` 之后是**要启动的命令**，不是本命令的选项。

        没有这个切换，`-y` 之类的参数会被当成 `minicode` 自己的选项——
        于是 `--url` 出现在命令参数里也会被误认为本命令的选项。
        """
        self.run_cmd(handle_mcp_command, "add", "s", "--", "cmd", "--url", "not-mine")
        entry = load_scoped_mcp_servers("user", str(self.cwd))["s"]
        self.assertNotIn("url", entry)
        self.assertEqual(entry["args"], ["--url", "not-mine"])

    def test_written_json_is_pretty_and_newline_terminated(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "s", "--", "cmd")
        raw = self.user_mcp_file().read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertIn("\n  ", raw)


class McpRemoveTests(ManageTestCase):
    def test_removes_existing(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "s", "--", "cmd")
        _, output = self.run_cmd(handle_mcp_command, "remove", "s")
        self.assertIn("Removed MCP server s", output)
        self.assertEqual(load_scoped_mcp_servers("user", str(self.cwd)), {})

    def test_removing_missing_is_not_an_error(self) -> None:
        """删一个不存在的**不算错误**：用户的意图（让它不存在）已经满足。"""
        handled, output = self.run_cmd(handle_mcp_command, "remove", "nope")
        self.assertTrue(handled)
        self.assertIn("not found", output)

    def test_missing_name_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "remove")

    def test_remove_respects_scope(self) -> None:
        self.run_cmd(handle_mcp_command, "add", "s", "--", "cmd")
        self.run_cmd(handle_mcp_command, "remove", "s", "--project")
        # 项目级本来就没有它，用户级的必须还在。
        self.assertIn("s", load_scoped_mcp_servers("user", str(self.cwd)))


class McpTokenTests(ManageTestCase):
    def tokens_file(self) -> Path:
        return self.home / "mcp-tokens.json"

    def test_login_stores_token(self) -> None:
        _, output = self.run_cmd(handle_mcp_command, "login", "s", "--token", "secret")
        self.assertIn("Stored MCP token for s", output)
        self.assertEqual(read_mcp_tokens_file()["s"], "secret")

    def test_token_file_is_0600_friendly_json(self) -> None:
        self.run_cmd(handle_mcp_command, "login", "s", "--token", "t")
        self.assertTrue(self.tokens_file().exists())
        self.assertTrue(self.tokens_file().read_text(encoding="utf-8").endswith("\n"))

    def test_login_without_token_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "login", "s")

    def test_login_without_name_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "login")

    def test_login_rejects_extra_arguments(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_mcp_command, "login", "s", "--token", "t", "extra")

    def test_logout_removes_token(self) -> None:
        self.run_cmd(handle_mcp_command, "login", "s", "--token", "t")
        _, output = self.run_cmd(handle_mcp_command, "logout", "s")
        self.assertIn("Removed MCP token for s", output)
        self.assertNotIn("s", read_mcp_tokens_file())

    def test_logout_missing_is_not_an_error(self) -> None:
        _, output = self.run_cmd(handle_mcp_command, "logout", "nope")
        self.assertIn("No token found", output)

    def test_multiple_tokens_coexist(self) -> None:
        self.run_cmd(handle_mcp_command, "login", "a", "--token", "1")
        self.run_cmd(handle_mcp_command, "login", "b", "--token", "2")
        tokens = read_mcp_tokens_file()
        self.assertEqual(tokens, {"a": "1", "b": "2"})

    def test_login_overwrites_existing(self) -> None:
        self.run_cmd(handle_mcp_command, "login", "a", "--token", "old")
        self.run_cmd(handle_mcp_command, "login", "a", "--token", "new")
        self.assertEqual(read_mcp_tokens_file()["a"], "new")


class McpDispatchTests(ManageTestCase):
    def test_no_subcommand_prints_usage(self) -> None:
        handled, output = self.run_cmd(handle_mcp_command)
        self.assertTrue(handled)
        self.assertIn("minicode mcp list", output)

    def test_unknown_subcommand_prints_usage(self) -> None:
        handled, output = self.run_cmd(handle_mcp_command, "frobnicate")
        self.assertTrue(handled)
        self.assertIn("minicode mcp list", output)


class SkillsCommandTests(ManageTestCase):
    def make_skill(self, name: str = "demo") -> Path:
        directory = self.cwd / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A {name} skill.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        return directory

    def test_list_empty(self) -> None:
        _, output = self.run_cmd(handle_skills_command, "list")
        self.assertIn("No skills discovered", output)

    def test_list_after_add(self) -> None:
        self.make_skill("demo")
        self.run_cmd(handle_skills_command, "add", str(self.cwd / "demo"))
        _, output = self.run_cmd(handle_skills_command, "list")
        self.assertIn("demo", output)

    def test_add_reports_target_path(self) -> None:
        self.make_skill("demo")
        _, output = self.run_cmd(handle_skills_command, "add", str(self.cwd / "demo"))
        self.assertIn("Installed skill demo", output)

    def test_add_with_explicit_name(self) -> None:
        self.make_skill("demo")
        _, output = self.run_cmd(
            handle_skills_command, "add", str(self.cwd / "demo"), "--name", "renamed"
        )
        self.assertIn("Installed skill renamed", output)

    def test_add_missing_path_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_skills_command, "add")

    def test_remove(self) -> None:
        self.make_skill("demo")
        self.run_cmd(handle_skills_command, "add", str(self.cwd / "demo"))
        _, output = self.run_cmd(handle_skills_command, "remove", "demo")
        self.assertIn("Removed skill demo", output)

    def test_remove_missing_is_not_an_error(self) -> None:
        _, output = self.run_cmd(handle_skills_command, "remove", "nope")
        self.assertIn("not found", output)

    def test_remove_missing_name_raises(self) -> None:
        with self.assertRaises(ManageError):
            self.run_cmd(handle_skills_command, "remove")

    def test_no_subcommand_prints_usage(self) -> None:
        _, output = self.run_cmd(handle_skills_command)
        self.assertIn("minicode skills list", output)


class EntryPointTests(ManageTestCase):
    def test_non_management_argv_returns_false(self) -> None:
        """返回 `False` 时**必须什么都没做**，否则普通启动会被悄悄影响。"""
        capture = io.StringIO()
        with redirect_stdout(capture):
            handled = maybe_handle_management_command(str(self.cwd), ["--mock", "-p", "hi"])
        self.assertFalse(handled)
        self.assertEqual(capture.getvalue(), "")

    def test_empty_argv_returns_false(self) -> None:
        self.assertFalse(maybe_handle_management_command(str(self.cwd), []))

    def test_help_prints_usage_and_returns_true(self) -> None:
        for flag in ("help", "--help", "-h"):
            with self.subTest(flag=flag):
                capture = io.StringIO()
                with redirect_stdout(capture):
                    handled = maybe_handle_management_command(str(self.cwd), [flag])
                self.assertTrue(handled)
                self.assertIn("minicode mcp list", capture.getvalue())

    def test_mcp_returns_true(self) -> None:
        capture = io.StringIO()
        with redirect_stdout(capture):
            handled = maybe_handle_management_command(str(self.cwd), ["mcp", "list"])
        self.assertTrue(handled)

    def test_skills_returns_true(self) -> None:
        capture = io.StringIO()
        with redirect_stdout(capture):
            handled = maybe_handle_management_command(str(self.cwd), ["skills", "list"])
        self.assertTrue(handled)

    def test_error_exits_with_code_one(self) -> None:
        """子命令只负责抛 `ManageError`，这一层统一转成退出码。

        这样每个子命令不必各自处理 I/O 与退出——否则总有一个忘了。
        """
        capture_err = io.StringIO()
        with redirect_stderr(capture_err):
            with self.assertRaises(SystemExit) as caught:
                maybe_handle_management_command(str(self.cwd), ["mcp", "add"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("error:", capture_err.getvalue())

    def test_usage_goes_to_stdout_for_help(self) -> None:
        """`--help` 是**成功**路径，用法说明进 stdout。"""
        capture = io.StringIO()
        with redirect_stdout(capture):
            maybe_handle_management_command(str(self.cwd), ["mcp"])
        self.assertIn("minicode mcp list", capture.getvalue())


class CliIntegrationTests(ManageTestCase):
    """`cli.main` 必须在 argparse **之前**拦截管理子命令。"""

    def test_main_handles_mcp_list(self) -> None:
        from minicode.cli import main

        capture = io.StringIO()
        with redirect_stdout(capture):
            code = main(["mcp", "list"])
        self.assertEqual(code, 0)
        self.assertIn("No MCP servers configured", capture.getvalue())

    def test_main_handles_mcp_add_with_double_dash(self) -> None:
        """**这条是关键**：`--` 与 `-y` 对 argparse 是未知参数。

        如果拦截发生在 argparse 之后，这里会直接报错退出。
        """
        from minicode.cli import main

        capture = io.StringIO()
        with redirect_stdout(capture):
            code = main(["mcp", "add", "fs", "--", "npx", "-y", "pkg"])
        self.assertEqual(code, 0)
        self.assertIn("Added MCP server fs", capture.getvalue())
        servers = load_scoped_mcp_servers("user", str(self.cwd))
        self.assertEqual(servers["fs"]["args"], ["-y", "pkg"])

    def test_main_uses_cwd_option_for_project_scope(self) -> None:
        from minicode.cli import _cwd_from_argv, main

        capture = io.StringIO()
        with redirect_stdout(capture):
            code = main(["--cwd", str(self.cwd), "mcp", "add", "p", "--", "cmd", "--project"])
        self.assertEqual(code, 0)
        self.assertTrue(self.project_mcp_file().exists())

    def test_cwd_from_argv(self) -> None:
        from minicode.cli import _cwd_from_argv

        self.assertEqual(_cwd_from_argv(["--cwd", "/x"]), "/x")
        self.assertEqual(_cwd_from_argv(["--cwd=/y"]), "/y")
        self.assertEqual(_cwd_from_argv([]), os.getcwd())

    def test_normal_argv_still_reaches_argparse(self) -> None:
        """非管理命令必须**照常**走原来的路径。"""
        from minicode.cli import build_parser

        args = build_parser().parse_args(["--mock", "-p", "/help"])
        self.assertEqual(args.prompt, "/help")
        self.assertTrue(args.mock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
