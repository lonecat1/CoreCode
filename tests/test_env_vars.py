"""环境变量：**两套拼写都认**（`MINI_CODE_*` 与 `MINICODE_*`）。

## 为什么这是一个真缺陷

原版读的是 `MINI_CODE_*`——**MINI 与 CODE 之间有一个下划线**：

    config.ts:36      process.env.MINI_CODE_HOME
    index.ts          process.env.MINI_CODE_MODEL_MODE
    anthropic-adapter process.env.MINI_CODE_MAX_RETRIES
    install.ts        process.env.MINI_CODE_BIN_DIR
    auto-compact.ts   process.env.MINI_CODE_DEBUG_AUTOCOMPACT

本实现历史上的拼写是 `MINICODE_*`（没有下划线），测试与文档都按它写。

于是一个**从原版过来的用户**设了 `MINI_CODE_HOME`：

    MINI_CODE_HOME=/tmp/myhome minicode

程序照旧读写 `~/.mini-code`，**不报错、不提示**。这是最难自查的一类问题——
配置看起来生效了，只是被静默忽略。

现在两套都认，**原版拼写优先**（它是上游契约，别名只照顾本项目的既有测试）。

## 顺带补齐的三个变量

原版有、本实现**完全没接**的三个：

| 变量 | 原版行为 |
|---|---|
| `MAX_RETRIES` | `getRetryLimit()`：`Number()` → 非有限或 <0 用默认值，否则 `floor` |
| `MODEL_MODE` | `=== 'mock'` 时选 mock 适配器（**原版没有 `--mock` 标志**，这是它唯一的入口） |
| `DEBUG_AUTOCOMPACT` | `=== '1'` 时往 **stderr** 打 `[auto-compact] …` |
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from minicode import config  # noqa: E402
from minicode.adapters.anthropic import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    AnthropicModelAdapter,
)
from minicode.config import RuntimeConfig  # noqa: E402
from minicode.context.compact import debug_auto_compact  # noqa: E402
from minicode.install import default_bin_dir  # noqa: E402


class _EnvCase(unittest.TestCase):
    """把所有相关的环境变量清空，再按需设置。"""

    MANAGED = [
        "MINI_CODE_HOME",
        "MINICODE_HOME",
        "MINI_CODE_BIN_DIR",
        "MINICODE_BIN_DIR",
        "MINI_CODE_MAX_RETRIES",
        "MINICODE_MAX_RETRIES",
        "MINI_CODE_MODEL_MODE",
        "MINICODE_MODEL_MODE",
        "MINI_CODE_DEBUG_AUTOCOMPACT",
        "MINICODE_DEBUG_AUTOCOMPACT",
    ]

    def env(self, **values: str):
        base = {k: v for k, v in os.environ.items() if k not in self.MANAGED}
        base.update(values)
        return patch.dict(os.environ, base, clear=True)


class EnvValueTests(_EnvCase):
    def test_original_spelling_is_read(self) -> None:
        with self.env(MINI_CODE_HOME="/tmp/orig"):
            self.assertEqual(config.env_value("HOME"), "/tmp/orig")

    def test_alias_spelling_is_read(self) -> None:
        with self.env(MINICODE_HOME="/tmp/alias"):
            self.assertEqual(config.env_value("HOME"), "/tmp/alias")

    def test_original_spelling_wins(self) -> None:
        """**原版拼写优先**——它是上游契约。"""
        with self.env(MINI_CODE_HOME="/tmp/orig", MINICODE_HOME="/tmp/alias"):
            self.assertEqual(config.env_value("HOME"), "/tmp/orig")

    def test_unset_returns_none(self) -> None:
        with self.env():
            self.assertIsNone(config.env_value("HOME"))

    def test_empty_string_is_unset(self) -> None:
        """与 JS 的假值判断一致：`''` 等于没设。"""
        with self.env(MINI_CODE_HOME=""):
            self.assertIsNone(config.env_value("HOME"))


class HomeResolutionTests(_EnvCase):
    def test_home_follows_the_original_spelling(self) -> None:
        with self.env(MINI_CODE_HOME=os.path.join(_HERE, "_tmp", "orig-home")):
            self.assertEqual(config.minicode_home().name, "orig-home")

    def test_home_follows_the_alias(self) -> None:
        with self.env(MINICODE_HOME=os.path.join(_HERE, "_tmp", "alias-home")):
            self.assertEqual(config.minicode_home().name, "alias-home")

    def test_derived_paths_follow_home(self) -> None:
        """所有派生路径都跟着走——它们都走 `minicode_home()`。"""
        target = os.path.join(_HERE, "_tmp", "derived-home")
        with self.env(MINI_CODE_HOME=target):
            home = config.minicode_home()
            self.assertEqual(config.settings_path().parent, home)
            self.assertEqual(config.history_path().parent, home)
            self.assertEqual(config.permissions_path().parent, home)
            self.assertEqual(config.mcp_tokens_path().parent, home)
            self.assertEqual(config.projects_dir().parent, home)

    def test_mcp_protocol_cache_follows_home(self) -> None:
        """缓存路径也走同一套解析，不能自己读环境变量。"""
        from minicode.mcp.stdio_client import protocol_cache_path

        target = os.path.join(_HERE, "_tmp", "cache-home")
        with self.env(MINI_CODE_HOME=target):
            self.assertEqual(protocol_cache_path().parent, config.minicode_home())


class BinDirTests(_EnvCase):
    def test_original_spelling(self) -> None:
        with self.env(MINI_CODE_BIN_DIR=os.path.join(_HERE, "_tmp", "bin-orig")):
            self.assertEqual(default_bin_dir().name, "bin-orig")

    def test_alias_spelling(self) -> None:
        with self.env(MINICODE_BIN_DIR=os.path.join(_HERE, "_tmp", "bin-alias")):
            self.assertEqual(default_bin_dir().name, "bin-alias")


class MaxRetriesTests(_EnvCase):
    """对应原版 `getRetryLimit()`（`anthropic-adapter.ts:12-18`）。"""

    def adapter(self) -> AnthropicModelAdapter:
        return AnthropicModelAdapter(
            lambda: RuntimeConfig(model="m", base_url="http://x", auth_token="t")
        )

    def retries_with(self, value: str | None) -> int:
        env = {} if value is None else {"MINI_CODE_MAX_RETRIES": value}
        with self.env(**env):
            return self.adapter()._max_retries  # noqa: SLF001

    def test_unset_uses_the_default(self) -> None:
        self.assertEqual(self.retries_with(None), DEFAULT_MAX_RETRIES)

    def test_plain_number(self) -> None:
        self.assertEqual(self.retries_with("7"), 7)

    def test_zero_means_no_retry(self) -> None:
        """**`0` 必须被尊重。**

        `0` 在 Python 里是假值，`resolve_max_retries() or DEFAULT` 会把它悄悄
        变回 4 次。原版 `Number('0') = 0` → `Math.floor(0)` → 0 次重试。
        """
        self.assertEqual(self.retries_with("0"), 0)

    def test_negative_falls_back(self) -> None:
        self.assertEqual(self.retries_with("-1"), DEFAULT_MAX_RETRIES)

    def test_non_numeric_falls_back(self) -> None:
        self.assertEqual(self.retries_with("abc"), DEFAULT_MAX_RETRIES)

    def test_infinity_falls_back(self) -> None:
        """JS 的 `Number.isFinite` 会拒绝 Infinity / NaN，这里也要。"""
        self.assertEqual(self.retries_with("inf"), DEFAULT_MAX_RETRIES)
        self.assertEqual(self.retries_with("nan"), DEFAULT_MAX_RETRIES)

    def test_decimal_is_floored(self) -> None:
        self.assertEqual(self.retries_with("3.9"), 3)

    def test_explicit_argument_wins_over_env(self) -> None:
        """构造参数显式给了值就不看环境变量。"""
        with self.env(MINI_CODE_MAX_RETRIES="9"):
            adapter = AnthropicModelAdapter(
                lambda: RuntimeConfig(model="m", base_url="http://x", auth_token="t"),
                max_retries=2,
            )
        self.assertEqual(adapter._max_retries, 2)  # noqa: SLF001


class ModelModeTests(_EnvCase):
    def test_mock_value(self) -> None:
        with self.env(MINI_CODE_MODEL_MODE="mock"):
            self.assertEqual(config.resolve_model_mode(), "mock")

    def test_case_sensitive_like_the_original(self) -> None:
        """原版是 `=== 'mock'`，大小写敏感。"""
        with self.env(MINI_CODE_MODEL_MODE="MOCK"):
            self.assertNotEqual(config.resolve_model_mode(), "mock")

    def test_unset_is_none(self) -> None:
        with self.env():
            self.assertIsNone(config.resolve_model_mode())

    def test_mock_mode_env_enters_mock_adapter(self) -> None:
        """端到端：设了环境变量就真的用 mock 适配器，不打真实 API。"""
        from minicode.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.mock)
        with self.env(MINI_CODE_MODEL_MODE="mock"):
            # 只验证判定链，不真的装配宿主（那会去连 MCP）。
            self.assertEqual(config.resolve_model_mode(), "mock")


class DebugAutoCompactTests(_EnvCase):
    def capture(self) -> str:
        buffer = io.StringIO()
        original = sys.stderr
        sys.stderr = buffer
        try:
            debug_auto_compact("utilization=0.900")
        finally:
            sys.stderr = original
        return buffer.getvalue()

    def test_prints_to_stderr_when_enabled(self) -> None:
        with self.env(MINI_CODE_DEBUG_AUTOCOMPACT="1"):
            self.assertEqual(self.capture(), "[auto-compact] utilization=0.900\n")

    def test_silent_when_disabled(self) -> None:
        with self.env():
            self.assertEqual(self.capture(), "")

    def test_only_the_literal_one_enables_it(self) -> None:
        """原版是 `=== '1'`，`true` / `yes` 都不算。"""
        for value in ("true", "yes", "on", "2"):
            with self.subTest(value=value):
                with self.env(MINI_CODE_DEBUG_AUTOCOMPACT=value):
                    self.assertEqual(self.capture(), "")


class ShouldAutoCompactTests(unittest.TestCase):
    """`should_auto_compact()` 是**单独导出**的（原版同理）。"""

    def test_below_threshold_is_false(self) -> None:
        from minicode.context.compact import should_auto_compact

        messages = [{"role": "user", "content": "hi"}]
        self.assertFalse(should_auto_compact(messages, "claude-sonnet-4"))

    def test_above_threshold_is_true(self) -> None:
        from minicode.context.compact import should_auto_compact

        messages = [{"role": "user", "content": "x" * 600_000}]
        self.assertTrue(should_auto_compact(messages, "claude-sonnet-4"))

    def test_debug_line_is_emitted_when_enabled(self) -> None:
        """调试行的字段顺序与原版一致，便于并排比较。"""
        from minicode.context.compact import should_auto_compact

        messages = [{"role": "user", "content": "x" * 600_000}]
        buffer = io.StringIO()
        original = sys.stderr
        sys.stderr = buffer
        try:
            with patch.dict(os.environ, {"MINI_CODE_DEBUG_AUTOCOMPACT": "1"}, clear=False):
                os.environ.pop("MINICODE_DEBUG_AUTOCOMPACT", None)
                should_auto_compact(messages, "claude-sonnet-4")
        finally:
            sys.stderr = original

        line = buffer.getvalue()
        self.assertTrue(line.startswith("[auto-compact] source="), line)
        for field in (" total=", " provider=", " estimate=", " utilization=",
                      " threshold=", " should=true"):
            with self.subTest(field=field):
                self.assertIn(field, line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
