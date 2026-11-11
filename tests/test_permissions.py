"""权限管理器的行为测试。

权限是安全边界，所以这里测的重点不是「能通过」，而是**该拦的必须拦住**：
工作区外默认要征询、拒绝必须抛异常、拒绝要能持久化、回合级授权必须随回合结束失效。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from corecode.abort import AbortSignal
from corecode.permissions import (
    PermissionDenied,
    PermissionManager,
    PermissionPromptResult,
    PermissionRequest,
    PathIntent,
    classify_dangerous_command,
    is_within_directory,
    read_permission_store,
    write_permission_store,
)

_TMP_ROOT = os.path.join(_HERE, "_tmp")

class FakePrompt:
    """可编排的提示处理器：按顺序吐决定，并记录收到的请求。

    决定用尽后 **重复最后一个决定**。这样测试只需声明「用户会怎么回答」，
    而不必预先数清楚会被问几次——数错次数会让测试失败在错误的地方。
    """

    def __init__(self, *decisions: tuple[str, str | None]) -> None:
        self._decisions = list(decisions)
        self.requests: list[PermissionRequest] = []

    def __call__(self, request: PermissionRequest, signal=None) -> PermissionPromptResult:  # noqa: ANN001
        self.requests.append(request)
        if not self._decisions:
            return PermissionPromptResult(decision="deny_once")
        # 最后一个决定会被反复使用。
        decision, feedback = (
            self._decisions.pop(0) if len(self._decisions) > 1 else self._decisions[0]
        )
        return PermissionPromptResult(decision=decision, feedback=feedback)

class DangerousCommandTests(unittest.TestCase):
    def test_destructive_git_commands_are_flagged(self) -> None:
        cases = [
            (["reset", "--hard"]),
            (["clean", "-fd"]),
            (["checkout", "--", "file.txt"]),
            (["push", "--force", "origin", "main"]),
            (["push", "-f"]),
            (["restore", "--source=HEAD~1", "file.txt"]),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertIsNotNone(classify_dangerous_command("git", args))

    def test_harmless_git_commands_are_not_flagged(self) -> None:
        for args in (["status"], ["log", "--oneline", "-5"], ["diff"], ["add", "."]):
            with self.subTest(args=args):
                self.assertIsNone(classify_dangerous_command("git", args))

    def test_publish_and_interpreters_are_flagged(self) -> None:
        self.assertIsNotNone(classify_dangerous_command("npm", ["publish"]))
        self.assertIsNotNone(classify_dangerous_command("python3", ["-c", "x"]))
        self.assertIsNotNone(classify_dangerous_command("bash", ["-lc", "rm -rf /"]))

    def test_unknown_command_is_not_flagged(self) -> None:
        self.assertIsNone(classify_dangerous_command("ls", ["-la"]))
        self.assertIsNone(classify_dangerous_command("grep", ["-r", "x", "."]))

class PathHelpersTests(unittest.TestCase):
    def test_is_within_directory(self) -> None:
        root = os.path.join(_TMP_ROOT, "root")
        self.assertTrue(is_within_directory(root, os.path.join(root, "a", "b.txt")))
        self.assertTrue(is_within_directory(root, root))
        self.assertFalse(is_within_directory(root, os.path.join(root, "..", "out.txt")))
        self.assertFalse(is_within_directory(root, os.path.dirname(root)))

    def test_prefix_sharing_is_not_containment(self) -> None:
        """`root\\a` 不包含 `root\\ab\\x`——前缀相同不等于在目录内。"""
        root = os.path.join(_TMP_ROOT, "d")
        self.assertFalse(
            is_within_directory(os.path.join(root, "a"), os.path.join(root, "ab", "x"))
        )

    @unittest.skipUnless(os.name == "nt", "跨盘符是 Windows 特有的")
    def test_cross_drive_returns_false_instead_of_raising(self) -> None:
        """**跨盘符必须返回 `False`，不能抛 `ValueError`。**

        ## 这是用户报过来的真实故障

        症状：工作目录在 C: 时让 agent 读 D: 上的文件，工具层收到的是一个
        异常，而不是「不在 cwd 内、要不要批准？」——用户看到的是「D 盘不在
        授权的工作目录中」这种看不懂的拒绝。

        `os.path.relpath` 抛不抛取决于盘符是否存在，所以这里只断言
        **不抛、且结果为 False**。
        """
        self.assertFalse(is_within_directory(r"C:\Windows", r"D:\CoreCode"))
        self.assertFalse(is_within_directory(r"D:\CoreCode", r"C:\Windows"))

    @unittest.skipUnless(os.name == "nt", "跨盘符是 Windows 特有的")
    def test_cross_drive_target_can_still_be_read_with_approval(self) -> None:
        """跨盘符的目标**要能走到批准流程**，而不是在边界检查里崩掉。

        这条走 `PermissionManager` 的真实路径：给一个「永远同意」的提示器，
        跨盘符的读取应当被放行（并记进会话允许集合）。
        """
        from corecode.permissions import PathIntent, PermissionManager, PermissionPromptResult

        def always_allow(request, signal):  # noqa: ANN001, ARG001
            return PermissionPromptResult(decision="allow_once", feedback=None)

        manager = PermissionManager(r"C:\Windows", always_allow)
        target = r"D:\CoreCode\corecode\README.md"
        manager.ensure_path_access(target, PathIntent.READ)  # 不抛即通过
        self.assertIn(target, manager._session_allowed_paths)

class PermissionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(_TMP_ROOT, exist_ok=True)
        self.workspace = os.path.join(_TMP_ROOT, "workspace")
        self.outside = os.path.join(_TMP_ROOT, "outside")
        os.makedirs(self.workspace, exist_ok=True)
        os.makedirs(self.outside, exist_ok=True)
        self.store_path = Path(_TMP_ROOT) / "permissions.json"
        self.store_path.unlink(missing_ok=True)

    def _manager(self, *decisions: tuple[str, str | None]) -> tuple[PermissionManager, FakePrompt]:
        prompt = FakePrompt(*decisions)
        return (
            PermissionManager(self.workspace, prompt, store_path=self.store_path),
            prompt,
        )

    # -- 路径 ---------------------------------------------------------------

    def test_inside_workspace_needs_no_prompt(self) -> None:
        manager, prompt = self._manager()
        manager.ensure_path_access(os.path.join(self.workspace, "a.txt"), PathIntent.READ)
        self.assertEqual(prompt.requests, [], "工作区内不应触发征询")

    def test_outside_workspace_prompts(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        target = os.path.join(self.outside, "a.txt")
        manager.ensure_path_access(target, PathIntent.READ)
        self.assertEqual(len(prompt.requests), 1)
        self.assertEqual(prompt.requests[0].kind, "path")
        self.assertEqual(prompt.requests[0].scope, self.outside)

    def test_allow_once_remembers_only_for_session(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        target = os.path.join(self.outside, "a.txt")
        manager.ensure_path_access(target, PathIntent.READ)
        # 同一会话内第二次不应再问
        manager.ensure_path_access(target, PathIntent.READ)
        self.assertEqual(len(prompt.requests), 1)
        self.assertEqual(read_permission_store(self.store_path).allowed_directory_prefixes, [])

    def test_allow_always_persists_the_directory(self) -> None:
        manager, _ = self._manager(("allow_always", None))
        manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.READ)

        store = read_permission_store(self.store_path)
        self.assertIn(self.outside, store.allowed_directory_prefixes)

        # 新实例应当继承这条规则，不再询问
        fresh, fresh_prompt = self._manager()
        fresh.ensure_path_access(os.path.join(self.outside, "b.txt"), PathIntent.READ)
        self.assertEqual(fresh_prompt.requests, [], "持久化的目录授权未被继承")

    def test_deny_once_raises_and_is_remembered(self) -> None:
        manager, prompt = self._manager(("deny_once", None))
        target = os.path.join(self.outside, "a.txt")
        with self.assertRaises(PermissionDenied):
            manager.ensure_path_access(target, PathIntent.READ)
        # 第二次直接拒绝，不再询问
        with self.assertRaises(PermissionDenied):
            manager.ensure_path_access(target, PathIntent.READ)
        self.assertEqual(len(prompt.requests), 1)

    def test_deny_always_persists_and_blocks_siblings(self) -> None:
        manager, _ = self._manager(("deny_always", None))
        with self.assertRaises(PermissionDenied):
            manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.READ)

        fresh, fresh_prompt = self._manager()
        with self.assertRaises(PermissionDenied):
            fresh.ensure_path_access(os.path.join(self.outside, "sibling.txt"), PathIntent.READ)
        self.assertEqual(fresh_prompt.requests, [], "持久化的拒绝规则未被继承")

    def test_no_prompt_handler_denies_outside_access(self) -> None:
        """非交互模式下越界访问必须失败，而不是静默放行。"""
        manager = PermissionManager(self.workspace, None, store_path=self.store_path)
        with self.assertRaises(PermissionDenied):
            manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.READ)

    def test_list_intent_scopes_to_the_directory_itself(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_path_access(self.outside, PathIntent.LIST)
        self.assertEqual(prompt.requests[0].scope, self.outside)

    def test_write_intent_scopes_to_parent_directory(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.WRITE)
        self.assertEqual(prompt.requests[0].scope, self.outside)

    # -- 命令 ---------------------------------------------------------------

    def test_safe_command_needs_no_prompt(self) -> None:
        manager, prompt = self._manager()
        manager.ensure_command("ls", ["-la"], self.workspace)
        self.assertEqual(prompt.requests, [])

    def test_dangerous_command_prompts_with_reason(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_command("git", ["reset", "--hard"], self.workspace)
        self.assertEqual(len(prompt.requests), 1)
        self.assertEqual(prompt.requests[0].kind, "command")
        self.assertTrue(
            any("reset" in line for line in prompt.requests[0].details),
            "审批详情里必须写明危险原因",
        )

    def test_force_prompt_reason_prompts_even_for_safe_commands(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_command(
            "ls", [], self.workspace, force_prompt_reason="命令不在允许清单内"
        )
        self.assertEqual(len(prompt.requests), 1)

    def test_command_denied_always_persists(self) -> None:
        manager, _ = self._manager(("deny_always", None))
        with self.assertRaises(PermissionDenied):
            manager.ensure_command("git", ["clean", "-fd"], self.workspace)

        fresh, fresh_prompt = self._manager()
        with self.assertRaises(PermissionDenied):
            fresh.ensure_command("git", ["clean", "-fd"], self.workspace)
        self.assertEqual(fresh_prompt.requests, [])

    def test_command_signature_separates_different_args(self) -> None:
        """授权是按完整命令签名给的，不能因为同是 git 就互相放行。"""
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_command("git", ["reset", "--hard"], self.workspace)
        manager.ensure_command("git", ["clean", "-fd"], self.workspace)
        self.assertEqual(len(prompt.requests), 2)

    # -- 编辑 ---------------------------------------------------------------

    def _edit_target(self) -> str:
        return os.path.join(self.workspace, "file.py")

    def test_edit_inside_workspace_still_prompts(self) -> None:
        """编辑是最危险的操作：即使在 cwd 内也**必须**展示 diff 请求批准。"""
        manager, prompt = self._manager(("allow_once", None))
        manager.ensure_edit(self._edit_target(), "--- a\n+++ b\n+new line")
        self.assertEqual(len(prompt.requests), 1)
        self.assertEqual(prompt.requests[0].kind, "edit")
        self.assertIn("+new line", "\n".join(prompt.requests[0].details))

    def test_allow_turn_expires_at_end_of_turn(self) -> None:
        manager, prompt = self._manager(("allow_turn", None))
        target = self._edit_target()

        manager.begin_turn()
        manager.ensure_edit(target, "diff")
        manager.ensure_edit(target, "diff")  # 本回合内第二次不再问
        self.assertEqual(len(prompt.requests), 1)

        manager.end_turn()
        manager.begin_turn()
        manager.ensure_edit(target, "diff")  # 新回合必须重新征询
        self.assertEqual(len(prompt.requests), 2)

    def test_allow_all_turn_covers_every_file_in_the_turn(self) -> None:
        manager, prompt = self._manager(("allow_all_turn", None))
        manager.begin_turn()
        manager.ensure_edit(self._edit_target(), "diff")
        manager.ensure_edit(
            os.path.join(self.workspace, "other.py"), "diff"
        )
        self.assertEqual(len(prompt.requests), 1, "allow_all_turn 应覆盖本回合所有文件")

        manager.end_turn()
        manager.begin_turn()
        manager.ensure_edit(os.path.join(self.workspace, "third.py"), "diff")
        self.assertEqual(len(prompt.requests), 2)

    def test_deny_with_feedback_passes_guidance_to_the_model(self) -> None:
        manager, _ = self._manager(("deny_with_feedback", "请改用 dataclass"))
        with self.assertRaises(PermissionDenied) as ctx:
            manager.ensure_edit(self._edit_target(), "diff")
        message = str(ctx.exception)
        self.assertIn("denied", message)
        self.assertIn("请改用 dataclass", message, "用户反馈必须回传给模型")

    def test_deny_without_feedback_still_denies(self) -> None:
        manager, _ = self._manager(("deny_with_feedback", None))
        with self.assertRaises(PermissionDenied) as ctx:
            manager.ensure_edit(self._edit_target(), "diff")
        self.assertNotIn("guidance", str(ctx.exception).lower())

    def test_allow_always_edit_persists(self) -> None:
        manager, _ = self._manager(("allow_always", None))
        manager.ensure_edit(self._edit_target(), "diff")

        fresh, fresh_prompt = self._manager()
        fresh.ensure_edit(self._edit_target(), "diff")
        self.assertEqual(fresh_prompt.requests, [])

    def test_deny_always_edit_persists(self) -> None:
        manager, _ = self._manager(("deny_always", None))
        with self.assertRaises(PermissionDenied):
            manager.ensure_edit(self._edit_target(), "diff")

        fresh, fresh_prompt = self._manager()
        with self.assertRaises(PermissionDenied):
            fresh.ensure_edit(self._edit_target(), "diff")
        self.assertEqual(fresh_prompt.requests, [])

    def test_no_prompt_handler_blocks_edits(self) -> None:
        manager = PermissionManager(self.workspace, None, store_path=self.store_path)
        with self.assertRaises(PermissionDenied):
            manager.ensure_edit(self._edit_target(), "diff")

    # -- 回合生命周期与取消 --------------------------------------------------

    def test_begin_turn_clears_turn_scoped_edits(self) -> None:
        manager, _ = self._manager(("allow_all_turn", None))
        manager.begin_turn()
        manager.ensure_edit(self._edit_target(), "diff")
        self.assertTrue(manager._turn_allow_all_edits)

        manager.begin_turn()  # 直接重开回合，不调用 end_turn
        self.assertFalse(manager._turn_allow_all_edits, "begin_turn 必须清空回合级授权")

    def test_aborted_turn_prevents_approval(self) -> None:
        manager, prompt = self._manager(("allow_once", None))
        signal = AbortSignal()
        signal.abort("用户取消")
        manager.begin_turn(signal)
        with self.assertRaises(Exception):
            manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.READ)
        self.assertEqual(prompt.requests, [], "已取消的回合不应弹出审批")

    # -- 摘要 ---------------------------------------------------------------

    def test_summary_reports_workspace_and_rules(self) -> None:
        manager, _ = self._manager(("allow_always", None))
        manager.ensure_path_access(os.path.join(self.outside, "a.txt"), PathIntent.READ)

        summary = "\n".join(manager.get_summary())
        self.assertIn(self.workspace, summary)
        self.assertIn(self.outside, summary)

    def test_summary_without_extra_rules_says_none(self) -> None:
        manager, _ = self._manager()
        summary = "\n".join(manager.get_summary())
        self.assertIn("extra allowed dirs: none", summary)
        self.assertIn("dangerous allowlist: none", summary)

    # -- 存储健壮性 ----------------------------------------------------------

    def test_corrupt_store_falls_back_to_safe_defaults(self) -> None:
        self.store_path.write_text("{ 这不是 JSON", encoding="utf-8")
        store = read_permission_store(self.store_path)
        self.assertEqual(store.allowed_directory_prefixes, [])
        self.assertEqual(store.denied_directory_prefixes, [])

    def test_store_round_trip(self) -> None:
        from corecode.permissions import PermissionStore

        write_permission_store(
            PermissionStore(
                allowed_directory_prefixes=["/a"],
                denied_command_patterns=["rm -rf"],
            ),
            self.store_path,
        )
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["allowedDirectoryPrefixes"], ["/a"])
        self.assertEqual(raw["deniedCommandPatterns"], ["rm -rf"])

        store = read_permission_store(self.store_path)
        self.assertEqual(store.allowed_directory_prefixes, ["/a"])
        self.assertEqual(store.denied_command_patterns, ["rm -rf"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
