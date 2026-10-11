"""配置装载的优先级与思考开关。

这几条是**真实故障**逼出来的，不是想象出来的用例：

  1. 机器上早就存在 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（用户为别的
     工具配的）。若环境变量压过 settings.json，写在本工具配置文件里的端点与
     密钥会被静默作废——配好了却连到别处，且毫无提示。
  2. 阿里云百炼的 `qwen3.8-max` **默认开启思考**，思考 token 与正文共享
     `max_tokens`。预算被思考吃光时模型返回**空内容**，症状是「模型不回答」，
     极难定位。因此需要能显式关闭思考。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.config import (  # noqa: E402
    ConfigError,
    RuntimeConfig,
    load_runtime_config,
    resolve_env_value,
    resolve_max_output_tokens,
)

unittest.TestCase.maxDiff = None

_TMP = Path(_HERE) / "_tmp" / "config"


def write_settings(payload: dict) -> Path:
    _TMP.mkdir(parents=True, exist_ok=True)
    target = _TMP / "settings.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _env_without(*names: str) -> dict[str, str]:
    """复制当前环境，去掉指定变量。

    刻意**不用** `patch.dict(..., clear=True)`：那会把 `MINICODE_HOME` 也删掉，
    而 `tests/__init__.py` 正是靠它把家目录钉在仓库内。清空它会让后续测试
    去读写真实用户目录——这个坑在本项目真实发生过。
    """
    return {k: v for k, v in os.environ.items() if k not in names}


class PrecedenceTests(unittest.TestCase):
    """settings.json 必须压过进程环境变量。"""

    def setUp(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def tearDown(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_settings_env_beats_ambient_variable(self) -> None:
        """核心断言：环境里有 ANTHROPIC_BASE_URL，settings 里也有——后者生效。"""
        path = write_settings(
            {
                "model": "qwen3.8-max-0902",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://wanted.example/anthropic",
                    "ANTHROPIC_API_KEY": "sk-wanted",
                },
            }
        )
        ambient = {
            "ANTHROPIC_BASE_URL": "https://unwanted.example/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "sk-unwanted",
        }
        with patch.dict(os.environ, ambient, clear=False):
            config = load_runtime_config(path)

        self.assertEqual(config.base_url, "https://wanted.example/anthropic")
        self.assertNotIn("unwanted", config.base_url)
        self.assertEqual(config.api_key, "sk-wanted")
        # 环境里那个 auth_token 不该被采纳（headers 会优先用它）
        self.assertIsNone(config.auth_token)

    def test_ambient_used_when_settings_is_silent(self) -> None:
        """settings 没写的项仍然从环境取——不要因为修优先级而切断回退。"""
        path = write_settings({"model": "m", "env": {"ANTHROPIC_API_KEY": "sk-fromfile"}})
        with patch.dict(
            os.environ,
            {"ANTHROPIC_BASE_URL": "https://ambient.example"},
            clear=False,
        ):
            config = load_runtime_config(path)
        self.assertEqual(config.base_url, "https://ambient.example")
        self.assertEqual(config.api_key, "sk-fromfile")

    def test_minicode_prefixed_variable_beats_settings(self) -> None:
        """`MINICODE_*` 是本工具专有前缀，允许临时覆盖配置文件。"""
        path = write_settings(
            {
                "model": "from-file",
                "env": {"MINICODE_MODEL": "from-file-env", "ANTHROPIC_API_KEY": "sk-x"},
            }
        )
        with patch.dict(os.environ, {"MINICODE_MODEL": "from-process"}, clear=False):
            config = load_runtime_config(path)
        self.assertEqual(config.model, "from-process")

    def test_settings_model_key_beats_ambient_anthropic_model(self) -> None:
        """`settings.model` 压过环境的 `ANTHROPIC_MODEL`。

        注意排除 `MINICODE_MODEL`：按设计它是本工具专有前缀、**始终优先**，
        用于临时覆盖（`MINICODE_MODEL=x minicode`）。它若在场会遮蔽本用例要
        验证的那条规则，所以这里显式去掉。
        """
        path = write_settings(
            {"model": "qwen3.8-max-0902", "env": {"ANTHROPIC_API_KEY": "sk-x"}}
        )
        ambient = _env_without("MINICODE_MODEL")
        ambient["ANTHROPIC_MODEL"] = "deepseek-v4-pro"
        with patch.dict(os.environ, ambient, clear=True):
            config = load_runtime_config(path)
        self.assertEqual(config.model, "qwen3.8-max-0902")

    def test_missing_credentials_raises(self) -> None:
        path = write_settings({"model": "m"})
        with patch.dict(
            os.environ,
            _env_without("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                load_runtime_config(path)

    def test_missing_model_raises(self) -> None:
        path = write_settings({"env": {"ANTHROPIC_API_KEY": "sk-x"}})
        with patch.dict(
            os.environ,
            _env_without("ANTHROPIC_MODEL", "MINICODE_MODEL"),
            clear=True,
        ):
            with self.assertRaises(ConfigError):
                load_runtime_config(path)


class ResolveEnvValueTests(unittest.TestCase):
    def test_file_wins_for_anthropic_keys(self) -> None:
        settings = {"env": {"ANTHROPIC_BASE_URL": "from-file"}}
        self.assertEqual(
            resolve_env_value("ANTHROPIC_BASE_URL", settings, {"ANTHROPIC_BASE_URL": "from-env"}),
            "from-file",
        )

    def test_process_wins_for_minicode_keys(self) -> None:
        """`MINICODE_*` 用于临时覆盖，例如 `MINICODE_MODEL=x minicode`。"""
        settings = {"env": {"MINICODE_MODEL": "from-file"}}
        self.assertEqual(
            resolve_env_value("MINICODE_MODEL", settings, {"MINICODE_MODEL": "from-env"}),
            "from-env",
        )

    def test_falls_back_to_process(self) -> None:
        self.assertEqual(
            resolve_env_value("ANTHROPIC_MODEL", {}, {"ANTHROPIC_MODEL": "from-env"}),
            "from-env",
        )

    def test_empty_when_nowhere(self) -> None:
        self.assertEqual(resolve_env_value("ANTHROPIC_MODEL", {}, {}), "")


class ThinkingTests(unittest.TestCase):
    """思考开关：百炼的 qwen3.8-max 默认开启思考，会把正文挤成空。"""

    def setUp(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def tearDown(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_default_is_unspecified(self) -> None:
        """默认**不发** thinking 字段，以保持 Anthropic 官方端点行为不变。"""
        config = RuntimeConfig(model="claude-sonnet-4")
        self.assertIsNone(config.thinking)
        self.assertIsNone(config.thinking_payload())

    def test_disabled_produces_payload(self) -> None:
        self.assertEqual(
            RuntimeConfig(model="m", thinking="disabled").thinking_payload(),
            {"type": "disabled"},
        )

    def test_enabled_produces_payload(self) -> None:
        self.assertEqual(
            RuntimeConfig(model="m", thinking="enabled").thinking_payload(),
            {"type": "enabled"},
        )

    def test_garbage_value_is_ignored(self) -> None:
        """非法值当作未配置，而不是把垃圾发到线上。"""
        self.assertIsNone(RuntimeConfig(model="m", thinking="maybe").thinking_payload())

    def test_loaded_from_settings(self) -> None:
        path = write_settings(
            {
                "model": "qwen3.8-max-0902",
                "thinking": "disabled",
                "env": {"ANTHROPIC_API_KEY": "sk-x"},
            }
        )
        with patch.dict(os.environ, {}, clear=False):
            config = load_runtime_config(path)
        self.assertEqual(config.thinking, "disabled")

    def test_loaded_from_env(self) -> None:
        path = write_settings(
            {"model": "m", "env": {"ANTHROPIC_API_KEY": "sk-x"}}
        )
        with patch.dict(os.environ, {"MINICODE_THINKING": "disabled"}, clear=False):
            config = load_runtime_config(path)
        self.assertEqual(config.thinking, "disabled")


class QwenTokenBudgetTests(unittest.TestCase):
    def test_qwen_gets_generous_budget(self) -> None:
        """Qwen 的思考与正文共享 max_tokens，兜底值不能太小。"""
        self.assertGreaterEqual(resolve_max_output_tokens("qwen3.8-max-0902"), 16_000)

    def test_qwen_configured_value_is_clamped(self) -> None:
        self.assertEqual(resolve_max_output_tokens("qwen3.8-max-0902", 999_999), 32_768)

    def test_claude_rules_unaffected(self) -> None:
        """新增 qwen 规则不能串到别的模型上。"""
        self.assertEqual(resolve_max_output_tokens("claude-3-5-sonnet"), 8_192)
        self.assertEqual(resolve_max_output_tokens("unknown-model"), 8_192)


if __name__ == "__main__":
    unittest.main(verbosity=2)
