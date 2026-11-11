"""

## 为什么单独测这个

用户已经攒了一堆「始终允许」的规则，如果本实现读不懂它们，**每次操作都会重新
问一遍**——不报错，只是把人烦死。

形状抄自 `permissions.ts` 的 `PermissionStore`（6 个驼峰数组字段）：

    allowedDirectoryPrefixes   deniedDirectoryPrefixes
    allowedCommandPatterns     deniedCommandPatterns
    allowedEditPatterns        deniedEditPatterns

写盘是 `${JSON.stringify(store, null, 2)}\\n`——2 空格缩进、结尾一个换行。
本实现必须一致，否则同一份文件在两种实现之间来回改会整篇重排。

## 选择项也是契约

提示里每个选项的**按键与 decision 字符串**都要对上，否则自动化脚本（`printf
'4\\n' | corecode`）在两种实现下会做出不同的事：

| 按键 | 路径/命令 | 编辑 |
|---|---|---|
| `y` / `n` | 仅本次允许 / 拒绝 | — |
| `a` / `d` | 记住允许 / 记住拒绝 | — |
| `1` | — | 仅应用本次 |
| `2` | — | 本回合内允许此文件 |
| `3` | — | 本回合内允许所有编辑 |
| `4` | — | 始终允许此文件 |
| `5` / `6` / `7` | — | 仅本次拒绝 / 拒绝并给反馈 / 始终拒绝 |
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

from corecode.permissions import (  # noqa: E402
    PathIntent,
    PermissionManager,
    PermissionRequest,
    PermissionStore,
    read_permission_store,
    write_permission_store,
)

TS_STORE_FIELDS = [
    "allowedDirectoryPrefixes",
    "deniedDirectoryPrefixes",
    "allowedCommandPatterns",
    "deniedCommandPatterns",
    "allowedEditPatterns",
    "deniedEditPatterns",
]

def _reject_all(request: PermissionRequest, signal=None):  # noqa: ANN001
    """永远拒绝的假提示——用来证明「没问」而不是「问了但被拒」。"""
    from corecode.permissions import PermissionPromptResult

    raise AssertionError(f"不该走到提示：{request}")

class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(dir=os.path.join(_HERE, "_tmp"), prefix="perm-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, True))
        home = os.path.join(self._tmp, "home")
        os.makedirs(home, exist_ok=True)
        self._env = patch.dict(os.environ, {"CORECODE_HOME": home}, clear=False)
        self._env.start()
        self.home = Path(home)
        self.store_path = self.home / "permissions.json"
        self.workspace = os.path.join(self._tmp, "workspace")
        os.makedirs(self.workspace, exist_ok=True)

    def tearDown(self) -> None:
        if self._env is not None:
            self._env.stop()

    def write_ts_store(self, payload: dict) -> Path:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        return self.store_path

class ReadsOriginalStoreTests(_StoreCase):

    def test_an_allowed_directory_prefix_is_honored(self) -> None:
        target = os.path.join(self.workspace, "sub", "file.txt")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text("x", encoding="utf-8")

        self.write_ts_store({"allowedDirectoryPrefixes": [os.path.dirname(target)]})

        # `_reject_all` 会在被问到的时候炸掉——所以「没炸」= 直接放行。
        manager = PermissionManager(self.workspace, _reject_all)
        manager.ensure_path_access(target, PathIntent.READ)  # 不抛 = 放行

    def test_an_allowed_command_pattern_is_honored(self) -> None:
        self.write_ts_store({"allowedCommandPatterns": ["echo", "pwd"]})
        manager = PermissionManager(self.workspace, _reject_all)
        manager.ensure_command("pwd", [], self.workspace)  # 不抛 = 放行

    def test_an_allowed_edit_pattern_is_honored(self) -> None:
        target = os.path.join(self.workspace, "a.txt")
        Path(target).write_text("x", encoding="utf-8")
        self.write_ts_store({"allowedEditPatterns": [target]})
        manager = PermissionManager(self.workspace, _reject_all)
        manager.ensure_edit(target, "diff")  # 不抛 = 放行

    def test_a_denied_directory_prefix_is_honored(self) -> None:
        """**拒绝规则也要生效**——只读能放行、拒绝不生效，等于没有拒绝规则。"""
        from corecode.permissions import PermissionDenied

        outside = os.path.join(self._tmp, "outside")
        os.makedirs(outside, exist_ok=True)
        target = os.path.join(outside, "secret.txt")
        Path(target).write_text("x", encoding="utf-8")

        self.write_ts_store({"deniedDirectoryPrefixes": [outside]})
        manager = PermissionManager(self.workspace, _reject_all)
        with self.assertRaises(PermissionDenied):
            manager.ensure_path_access(target, PathIntent.READ)

    def test_a_denied_command_pattern_is_honored(self) -> None:
        """拒绝规则对**会触发提示的命令**生效。

        ⚠️ 注意顺序：`ensure_command` 先判「这条命令危不危险」，**不危险就直接
        放行**，之后才查允许/拒绝表。所以给 `pwd` 加一条拒绝规则不会有任何
        """
        from corecode.permissions import PermissionDenied

        # `git push --force` 是分类器认定的危险命令，会走到提示那一层。
        self.write_ts_store({"deniedCommandPatterns": ["git push --force"]})
        manager = PermissionManager(self.workspace, _reject_all)
        with self.assertRaises(PermissionDenied):
            manager.ensure_command("git", ["push", "--force"], self.workspace)

    def test_deny_rules_do_not_apply_to_harmless_commands(self) -> None:
        """**这条钉住一个容易被「顺手修好」的行为。**

        `deniedCommandPatterns` 里写了 `pwd` 也不会拦住 `pwd`：拒绝检查在
        「危险命令」这道门之后，而 `pwd` 不危险。

        `const reason = …; if (!reason) return`，之后才查 denied）。
        """
        self.write_ts_store({"deniedCommandPatterns": ["pwd"]})
        manager = PermissionManager(self.workspace, _reject_all)
        manager.ensure_command("pwd", [], self.workspace)  # 不抛 = 放行

    def test_the_classifier_covers_the_same_commands(self) -> None:
        """

        （`node` / `python3` / `bun` / `bash` / `sh`）。**`rm` 不在其中**——
        """
        from corecode.permissions import classify_dangerous_command as classify

        dangerous = [
            ("git", ["reset", "--hard"]),
            ("git", ["clean", "-fd"]),
            ("git", ["checkout", "--", "."]),
            ("git", ["restore", "--source", "HEAD~1", "f"]),
            ("git", ["push", "--force"]),
            ("git", ["push", "-f"]),
            ("npm", ["publish"]),
            ("node", ["-e", "1"]),
            ("python3", ["-c", "1"]),
            ("bun", ["run"]),
            ("bash", ["-lc", "x"]),
            ("sh", ["-c", "x"]),
        ]
        for command, args in dangerous:
            with self.subTest(command=command, args=args):
                self.assertIsNotNone(classify(command, args))

        harmless = [
            ("pwd", []),
            ("echo", ["hi"]),
            ("git", ["status"]),
            ("npm", ["install"]),
            ("rm", ["-rf", "/"]),
        ]
        for command, args in harmless:
            with self.subTest(command=command, args=args):
                self.assertIsNone(classify(command, args))

    def test_the_classifier_is_strictly_wider_than_the_original(self) -> None:
        """

        |---|---|---|
        | 发布命令 | 只认 `npm publish` | `npm` / `pnpm` / `yarn` |
        | 解释器 | `node` `python3` `bun` `bash` `sh` | 再加 `python` `pwsh` `powershell` |

        为什么保留这个加严：它只会**多问一次**，不会静默损坏任何东西。而
        `pnpm publish` / `pwsh -c …` 在 Windows 与 pnpm 用户那里是常见操作，
        漏掉提示才是真风险。

        这条测试的作用**不是**验证加严的细节，而是防止有人「为了对齐」把它
        """
        from corecode.permissions import classify_dangerous_command as classify

        extra = [
            ("pnpm", ["publish"]),
            ("yarn", ["publish"]),
            ("python", ["-c", "1"]),
            ("pwsh", ["-c", "1"]),
            ("powershell", ["-Command", "1"]),
        ]
        for command, args in extra:
            with self.subTest(command=command):
                self.assertIsNotNone(
                    classify(command, args),
                    f"{command} 应当被判为危险（本实现更严，见 D50）",
                )

    def test_unknown_fields_do_not_break_the_read(self) -> None:
        self.write_ts_store(
            {
                "allowedCommandPatterns": ["pwd"],
                "someFutureField": {"nested": True},
            }
        )
        manager = PermissionManager(self.workspace, _reject_all)
        manager.ensure_command("pwd", [], self.workspace)

    def test_empty_object_is_the_same_as_no_file(self) -> None:
        self.write_ts_store({})
        manager = PermissionManager(self.workspace, _reject_all)
        store = read_permission_store()
        self.assertEqual(store.allowed_command_patterns, [])

class WriteShapeTests(_StoreCase):

    def test_written_json_has_exactly_the_original_fields(self) -> None:
        store = PermissionStore(allowed_command_patterns=["pwd"])
        write_permission_store(store, self.store_path)
        payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(list(payload), TS_STORE_FIELDS)

    def test_written_json_is_pretty_and_newline_terminated(self) -> None:
        """

        缩进或结尾换行不一致的话，同一份文件在两种实现间来回改会**整篇重排**
        ——diff 里全是噪音。
        """
        write_permission_store(
            PermissionStore(allowed_command_patterns=["pwd"]), self.store_path
        )
        raw = self.store_path.read_bytes().decode("utf-8")
        self.assertTrue(raw.endswith("\n"), "结尾必须有换行")
        self.assertIn('\n  "', raw, "必须是 2 空格缩进")
        self.assertFalse(raw.endswith("\n\n"), "只该有一个结尾换行")

    def test_round_trip_through_our_own_writer(self) -> None:
        original = PermissionStore(
            allowed_directory_prefixes=["/a"],
            denied_directory_prefixes=["/b"],
            allowed_command_patterns=["pwd"],
            denied_command_patterns=["curl"],
            allowed_edit_patterns=["/c"],
            denied_edit_patterns=["/d"],
        )
        write_permission_store(original, self.store_path)
        loaded = read_permission_store(self.store_path)
        self.assertEqual(loaded.to_json(), original.to_json())

    def test_new_store_is_all_empty_lists(self) -> None:
        write_permission_store(PermissionStore(), self.store_path)
        payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(list(payload), TS_STORE_FIELDS)
        for key in TS_STORE_FIELDS:
            with self.subTest(key=key):
                self.assertEqual(payload[key], [])

class ChoiceContractTests(unittest.TestCase):
    """提示选项的按键与 decision 字符串是契约（自动化脚本依赖它）。"""

    TS_PATH_CHOICES = [("y", "allow_once"), ("a", "allow_always"),
                       ("n", "deny_once"), ("d", "deny_always")]
    TS_COMMAND_CHOICES = TS_PATH_CHOICES
    TS_EDIT_CHOICES = [
        ("1", "allow_once"),
        ("2", "allow_turn"),
        ("3", "allow_all_turn"),
        ("4", "allow_always"),
        ("5", "deny_once"),
        ("6", "deny_with_feedback"),
        ("7", "deny_always"),
    ]

    def _choices(self, kind: str) -> list[tuple[str, str]]:
        from corecode import permissions

        for name in dir(permissions):
            if name.startswith("_") or not name.endswith("_CHOICES"):
                continue
            if kind in name.lower():
                return [(c.key, c.decision) for c in getattr(permissions, name)]
        return []

    def test_keys_and_decisions_match_the_original(self) -> None:
        import inspect

        from corecode import permissions

        source = inspect.getsource(permissions)
        for expected in (self.TS_PATH_CHOICES, self.TS_EDIT_CHOICES):
            for key, decision in expected:
                with self.subTest(key=key, decision=decision):
                    self.assertIn(
                        f'("{key}",', source,
                        f"选项按键 {key!r} 不在源码里",
                    )
                    self.assertIn(
                        f'"{decision}"', source,
                        f"decision {decision!r} 不在源码里",
                    )

    def test_edit_has_seven_choices(self) -> None:
        """编辑是唯一有 7 个选项的一组（其余两组 4 个）。"""
        import inspect

        from corecode import permissions

        source = inspect.getsource(permissions)
        for key in ("1", "2", "3", "4", "5", "6", "7"):
            with self.subTest(key=key):
                self.assertIn(f'("{key}", "', source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
