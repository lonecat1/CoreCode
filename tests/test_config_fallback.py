"""配置来源合并与 Claude Code 兼容回退。

对应原版 `config.ts:173-188` 的 `loadEffectiveSettings()`。

## 这个缺口为什么值得单独一个文件

原版会读 `~/.claude/settings.json` 作为**最低优先级**的配置来源。本实现此前
完全没有这一层——从 Claude Code 迁移过来的用户会发现他已经配好的模型名、
端点、密钥**全部失效**，而程序不会给出任何提示（它会报「未配置模型」）。

这类「静默不生效」的缺口最难自己发现：配置项看起来都在，只是来源少了一处。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.config import (  # noqa: E402
    ConfigError,
    load_effective_settings,
    load_runtime_config,
    merge_settings,
    read_settings_file,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="minicode-cfg-"))
        self.home = self.root / "mini-home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.own = self.home / "settings.json"
        self.claude = self.root / ".claude" / "settings.json"
        self.claude.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def effective(self) -> dict:
        with patch.dict(os.environ, {"MINICODE_HOME": str(self.home)}):
            return load_effective_settings(
                settings_path=self.own, claude_path=self.claude
            )


class MergeSettingsTests(unittest.TestCase):
    """`merge_settings` 的语义——原版 `config.ts:162-171`。"""

    def test_override_wins_on_scalars(self) -> None:
        self.assertEqual(merge_settings({"a": 1}, {"a": 2})["a"], 2)

    def test_env_is_deep_merged(self) -> None:
        """`env` 必须**深合并**：用户常常只想补一个变量。"""
        merged = merge_settings({"env": {"A": "1"}}, {"env": {"B": "2"}})
        self.assertEqual(merged["env"], {"A": "1", "B": "2"})

    def test_env_override_wins_on_same_key(self) -> None:
        merged = merge_settings({"env": {"A": "1"}}, {"env": {"A": "2"}})
        self.assertEqual(merged["env"]["A"], "2")

    def test_missing_env_does_not_add_key(self) -> None:
        self.assertNotIn("env", merge_settings({"a": 1}, {"b": 2}))

    def test_mcp_servers_are_merged_by_name(self) -> None:
        merged = merge_settings(
            {"mcpServers": {"x": {"command": "a"}}},
            {"mcpServers": {"y": {"command": "b"}}},
        )
        self.assertEqual(sorted(merged["mcpServers"]), ["x", "y"])

    def test_mcp_server_env_is_deep_merged(self) -> None:
        merged = merge_settings(
            {"mcpServers": {"x": {"command": "a", "env": {"K": "1"}}}},
            {"mcpServers": {"x": {"env": {"J": "2"}}}},
        )
        self.assertEqual(merged["mcpServers"]["x"]["env"], {"K": "1", "J": "2"})
        self.assertEqual(merged["mcpServers"]["x"]["command"], "a")

    def test_non_dict_server_replaces(self) -> None:
        merged = merge_settings(
            {"mcpServers": {"x": {"command": "a"}}}, {"mcpServers": {"x": "bad"}}
        )
        self.assertEqual(merged["mcpServers"]["x"], "bad")


class EffectiveSettingsTests(ConfigTestCase):
    def test_claude_fallback_provides_missing_keys(self) -> None:
        """**核心回归**：自有配置没写的键应当从 Claude 配置继承。"""
        self.write_json(self.claude, {"model": "from-claude"})
        self.write_json(self.own, {"maxOutputTokens": 100})
        merged = self.effective()
        self.assertEqual(merged["model"], "from-claude")
        self.assertEqual(merged["maxOutputTokens"], 100)

    def test_own_settings_win_over_claude(self) -> None:
        """自有配置是**最高**优先级。"""
        self.write_json(self.claude, {"model": "from-claude"})
        self.write_json(self.own, {"model": "from-own"})
        self.assertEqual(self.effective()["model"], "from-own")

    def test_env_is_deep_merged_across_sources(self) -> None:
        self.write_json(self.claude, {"env": {"A": "1", "B": "claude"}})
        self.write_json(self.own, {"env": {"B": "own", "C": "3"}})
        self.assertEqual(
            self.effective()["env"], {"A": "1", "B": "own", "C": "3"}
        )

    def test_missing_claude_file_is_not_an_error(self) -> None:
        self.write_json(self.own, {"model": "x"})
        self.assertEqual(self.effective()["model"], "x")

    def test_both_missing_yields_no_real_settings(self) -> None:
        """两处都没有时不该冒出任何**真实**配置。

        注意结果里会有一个空的 `mcpServers` 键——这是原版也有的行为
        （`loadEffectiveSettings` 无条件传 `{ mcpServers: globalMcpConfig }`，
        浅合并会把它带进来）。它无害：下游读 `mcpServers` 得到空 dict。
        断言写成「没有真实键」而不是「等于 {}」，避免把忠实行为当成缺陷。
        """
        merged = self.effective()
        self.assertEqual(set(merged) - {"mcpServers"}, set())
        self.assertEqual(merged.get("mcpServers"), {})

    def test_corrupt_claude_file_raises(self) -> None:
        """坏 JSON 要报错，而不是静默当成空配置。

        静默吞掉会让用户面对「我明明配了却不生效」而没有任何线索。
        """
        self.claude.write_text("{ 这不是 JSON", encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.effective()

    def test_global_mcp_json_is_merged(self) -> None:
        """`<MINICODE_HOME>/mcp.json` 也参与合并（原版 `config.ts:177`）。"""
        self.write_json(self.home / "mcp.json", {"mcpServers": {"s": {"command": "c"}}})
        merged = self.effective()
        self.assertIn("s", merged["mcpServers"])

    def test_own_settings_win_over_global_mcp_json(self) -> None:
        self.write_json(self.home / "mcp.json", {"mcpServers": {"s": {"command": "a"}}})
        self.write_json(
            self.own, {"mcpServers": {"s": {"command": "b"}}}
        )
        self.assertEqual(self.effective()["mcpServers"]["s"]["command"], "b")


class ClaudePathTests(unittest.TestCase):
    def test_claude_path_is_not_under_minicode_home(self) -> None:
        """**它必须独立于 `MINICODE_HOME`。**

        跟随 `MINICODE_HOME` 会让「兼容回退」指向本项目自己的家目录，
        形成自我引用——测试里还会造成隔离失效（读到真实用户目录或读到自己）。
        """
        from minicode.config import claude_settings_path

        with patch.dict(os.environ, {"MINICODE_HOME": r"C:\totally\elsewhere"}):
            path = claude_settings_path()
        self.assertNotIn("totally", str(path))
        self.assertEqual(path, Path.home() / ".claude" / "settings.json")

    def test_import_time_constant_matches_the_accessor(self) -> None:
        from minicode.config import CLAUDE_SETTINGS_PATH, claude_settings_path

        self.assertEqual(CLAUDE_SETTINGS_PATH, claude_settings_path())


class RuntimeConfigFallbackTests(ConfigTestCase):
    """回退必须**一路走到** `load_runtime_config`，而不只是合并函数里对。

    注意 `load_runtime_config` 只接受 `settings_path`，Claude 路径取的是
    `claude_settings_path()`（真实用户目录）。测试必须把它 patch 掉——
    否则读的是开发者本机的 `~/.claude/settings.json`，测试结果随机器而变。
    """

    def _load(self, **env_overrides):  # noqa: ANN003
        """在隔离环境里装载配置。返回 RuntimeConfig 或抛 ConfigError。"""
        clean = {
            "MINICODE_HOME": str(self.home),
            **env_overrides,
        }
        with patch.dict(os.environ, clean, clear=False):
            for key in (
                "MINICODE_MODEL",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
            ):
                os.environ.pop(key, None)
            with patch("minicode.config.claude_settings_path", return_value=self.claude):
                return load_runtime_config(self.own)

    def test_model_and_credentials_come_from_claude(self) -> None:
        self.write_json(
            self.claude,
            {
                "model": "claude-model",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                    "ANTHROPIC_API_KEY": "sk-from-claude",
                },
            },
        )
        self.write_json(self.own, {})
        config = self._load()
        self.assertEqual(config.model, "claude-model")
        self.assertEqual(config.api_key, "sk-from-claude")
        self.assertEqual(config.base_url, "https://example.invalid")

    def test_own_settings_override_claude_credentials(self) -> None:
        self.write_json(
            self.claude,
            {"model": "m", "env": {"ANTHROPIC_API_KEY": "sk-claude"}},
        )
        self.write_json(
            self.own,
            {"model": "m", "env": {"ANTHROPIC_API_KEY": "sk-own"}},
        )
        self.assertEqual(self._load().api_key, "sk-own")

    def test_no_credentials_anywhere_still_raises(self) -> None:
        self.write_json(self.own, {"model": "m"})
        with self.assertRaises(ConfigError):
            self._load()

    def test_real_claude_path_is_not_used_by_default(self) -> None:
        """**护栏**：不 patch 时读的必须是真实用户目录。

        如果哪天有人把 Claude 路径改成跟随 `MINICODE_HOME`，这条会失败——
        那会造成「兼容回退读到自己」，也会破坏测试隔离。
        """
        from minicode.config import claude_settings_path

        with patch.dict(os.environ, {"MINICODE_HOME": str(self.home)}):
            self.assertNotIn(str(self.home), str(claude_settings_path()))


class ReadSettingsFileTests(ConfigTestCase):
    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(read_settings_file(self.root / "nope.json"), {})

    def test_non_dict_json_is_empty(self) -> None:
        self.own.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(read_settings_file(self.own), {})

    def test_corrupt_json_raises(self) -> None:
        self.own.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ConfigError):
            read_settings_file(self.own)


if __name__ == "__main__":
    unittest.main(verbosity=2)
