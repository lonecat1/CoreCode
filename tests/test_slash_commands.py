"""斜杠命令表与 Tab 补全。

## 为什么要单独钉住这张表

`SLASH_COMMANDS` 同时是**三个用户可见行为的唯一事实来源**：

  1. `/help` 的输出（`format_slash_commands()` 直接遍历它）；
  2. Tab 补全的候选集（`complete_slash_command()` 按 usage 前缀匹配）；
  3. 命令分派（每个 `usage.split(" ")[0]` 必须有 handler）。

## 曾经的两个缺陷

**① 补全函数写了但从未接线。** `find_matching_slash_commands()` 早就存在，
但没有任何调用点、也没有测试——`check_constants.py` 只检查**常量**的引用，
不检查函数，所以这类缺陷完全逃得过工具。

**② 表被「合并」成了 29 条。** 原版是 37 条：`/loop` 占三条、`/goal` 占六条。
合并成 `/loop [Nm|Nh] <prompt> | /loop stop` 之后同时坏掉两件事：

    /help        少列 8 行，用户看不到 /loop stop、/goal pause 这些子形式
    Tab 补全      敲 "/loop s" + Tab 一个都匹配不上，退化成列出全部命令

后者尤其难发现：补全「有反应」（列了一大堆），只是列的**不是**想要的那一条。
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from minicode.cli_commands import (  # noqa: E402
    SLASH_COMMANDS,
    all_slash_usages,
    complete_slash_command,
    find_matching_slash_commands,
    format_slash_commands,
    install_slash_completion,
)

#: 原版 `cli-commands.ts:21-172` 的 usage 列表，**顺序也一致**。
#: 这份字面量就是契约：改它等于改用户看到的 /help。
TS_USAGES = [
    "/loop",
    "/loop [Nm|Nh] <prompt>",
    "/loop stop",
    "/goal",
    "/goal <description>",
    "/goal status",
    "/goal pause [reason]",
    "/goal resume",
    "/goal clear",
    "/plan",
    "/help",
    "/tools",
    "/status",
    "/model",
    "/model <model-name>",
    "/config-paths",
    "/skills",
    "/mcp",
    "/resume",
    "/rename <name>",
    "/new",
    "/fork",
    "/permissions",
    "/exit",
    "/ls [path]",
    "/grep <pattern>::[path]",
    "/read <path>",
    "/write <path>::<content>",
    "/modify <path>::<content>",
    "/edit <path>::<search>::<replace>",
    "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...",
    "/cmd [cwd::]<command> [args...]",
    "/compact",
    "/collapse",
    "/snip",
    "/init",
    "/memory",
]


class SlashCommandTableTests(unittest.TestCase):
    def test_has_exactly_the_original_37_entries(self) -> None:
        self.assertEqual(len(SLASH_COMMANDS), 37)

    def test_usages_match_the_original_in_order(self) -> None:
        self.assertEqual([item.usage for item in SLASH_COMMANDS], TS_USAGES)

    def test_multi_form_commands_are_not_merged(self) -> None:
        """`/loop` 三条、`/goal` 六条——**合并会同时坏掉 /help 与补全**。"""
        loop_forms = [item.usage for item in SLASH_COMMANDS if item.name == "/loop"]
        goal_forms = [item.usage for item in SLASH_COMMANDS if item.name == "/goal"]
        self.assertEqual(
            loop_forms, ["/loop", "/loop [Nm|Nh] <prompt>", "/loop stop"]
        )
        self.assertEqual(len(goal_forms), 6)

    def test_every_entry_has_a_description(self) -> None:
        for item in SLASH_COMMANDS:
            with self.subTest(usage=item.usage):
                self.assertTrue(item.description.strip())

    def test_usage_starts_with_its_name(self) -> None:
        """`usage.split(" ")[0]` 被分派逻辑当成命令名用。"""
        for item in SLASH_COMMANDS:
            with self.subTest(usage=item.usage):
                self.assertEqual(item.usage.split(" ")[0], item.name)

    def test_names_are_unique_after_dedup(self) -> None:
        names = {item.name for item in SLASH_COMMANDS}
        self.assertEqual(len(names), 29, "29 个命令名 → 37 条 usage")


class HelpRenderingTests(unittest.TestCase):
    def test_help_lists_every_usage_on_its_own_line(self) -> None:
        lines = format_slash_commands().split("\n")
        self.assertEqual(len(lines), 37)
        for line, usage in zip(lines, TS_USAGES):
            self.assertTrue(line.startswith(usage), line)

    def test_help_shows_sub_forms(self) -> None:
        text = format_slash_commands()
        for usage in ("/loop stop", "/goal pause [reason]", "/goal clear"):
            with self.subTest(usage=usage):
                self.assertIn(usage, text)


class CompletionTests(unittest.TestCase):
    """对应原版 `completeSlashCommand`（`cli-commands.ts:313`）。"""

    def test_prefix_match_returns_hits(self) -> None:
        matches, echo = complete_slash_command("/co")
        self.assertEqual(matches, ["/config-paths", "/compact", "/collapse"])
        self.assertEqual(echo, "/co")

    def test_sub_form_completion_works(self) -> None:
        """**这条就是那个难发现的回归。**

        表被合并时，`/loop s` 一个都匹配不上，于是退化成「列出全部 37 条」——
        补全看起来有反应，只是不是想要的那条。
        """
        matches, _ = complete_slash_command("/loop s")
        self.assertEqual(matches, ["/loop stop"])

        matches, _ = complete_slash_command("/goal p")
        self.assertEqual(matches, ["/goal pause [reason]"])

    def test_no_hits_falls_back_to_everything(self) -> None:
        matches, echo = complete_slash_command("/zzz")
        self.assertEqual(matches, all_slash_usages())
        self.assertEqual(echo, "/zzz")

    def test_empty_line_lists_everything(self) -> None:
        matches, echo = complete_slash_command("")
        self.assertEqual(matches, all_slash_usages())
        self.assertEqual(echo, "")

    def test_echo_is_the_input_unchanged(self) -> None:
        for line in ("/lo", "/loop ", "", "  /x"):
            with self.subTest(line=line):
                self.assertEqual(complete_slash_command(line)[1], line)

    def test_exact_match_is_a_hit(self) -> None:
        self.assertEqual(complete_slash_command("/compact")[0], ["/compact"])

    def test_find_matching_is_pure_prefix(self) -> None:
        self.assertEqual(find_matching_slash_commands("/zzz"), [])
        self.assertEqual(find_matching_slash_commands(""), all_slash_usages())

    def test_matches_are_all_real_usages(self) -> None:
        pool = set(all_slash_usages())
        for prefix in ("/", "/g", "/goal", "/p", "/m"):
            for match in complete_slash_command(prefix)[0]:
                with self.subTest(prefix=prefix, match=match):
                    self.assertIn(match, pool)


class InstallCompleterTests(unittest.TestCase):
    """`install_slash_completion` 是**可选**的：装不上也不能影响启动。"""

    def test_returns_a_bool_and_never_raises(self) -> None:
        result = install_slash_completion()
        self.assertIsInstance(result, bool)

    def test_installing_twice_is_harmless(self) -> None:
        install_slash_completion()
        self.assertIsInstance(install_slash_completion(), bool)

    def test_completion_logic_works_without_readline(self) -> None:
        """补全的**判决逻辑**不依赖 readline——所以能脱离它单独测。

        readline 是可选 C 扩展（Windows 上没有），把逻辑和绑定分开之后，
        上面的 `CompletionTests` 在任何平台上都跑得通。
        """
        matches, _ = complete_slash_command("/co")
        self.assertTrue(matches)
        self.assertEqual(install_slash_completion(), install_slash_completion())


class FakeReadlineInstallTests(unittest.TestCase):
    """用一个**假的 readline 模块**把 `install_slash_completion` 的函数体跑起来。

    ## 为什么需要这个

    readline 在 Windows 上不存在，于是 `install_slash_completion()` 直接
    `return False`，**函数体一行都没执行**。结果是「清空 delims」「用整行做
    匹配」这两条都测不到——变异检查里它们正是两条存活项。

    注入假模块之后，这些语句真的被执行，因此可以被断言、也可以被变异杀死。
    """

    def setUp(self) -> None:
        import types

        self.calls: dict[str, object] = {}
        fake = types.ModuleType("readline")

        def set_completer_delims(value):  # noqa: ANN001
            self.calls["delims"] = value

        def set_completer(function):  # noqa: ANN001
            self.calls["completer"] = function

        def parse_and_bind(binding):  # noqa: ANN001
            self.calls["binding"] = binding

        def get_line_buffer():  # noqa: ANN201
            return self.calls.get("buffer", "")

        fake.set_completer_delims = set_completer_delims
        fake.set_completer = set_completer
        fake.parse_and_bind = parse_and_bind
        fake.get_line_buffer = get_line_buffer
        self.fake = fake
        self._saved = sys.modules.get("readline")
        sys.modules["readline"] = fake

    def tearDown(self) -> None:
        if self._saved is None:
            sys.modules.pop("readline", None)
        else:
            sys.modules["readline"] = self._saved

    def test_installs_and_returns_true(self) -> None:
        self.assertTrue(install_slash_completion())

    def test_binds_tab_to_complete(self) -> None:
        install_slash_completion()
        self.assertEqual(self.calls.get("binding"), "tab: complete")

    def test_clears_the_word_delimiters(self) -> None:
        """**必须清空 delims。**

        readline 默认按空格等字符切词，于是 `/loop st` 按 Tab 只补全 `st`，
        而且会把整行替换成一个 usage——与原版「按整行前缀匹配」不一致。

        不清空的话这条断言失败。这也是变异检查里的一条存活项。
        """
        install_slash_completion()
        self.assertEqual(self.calls.get("delims"), "")

    def test_completer_matches_on_the_whole_line(self) -> None:
        """**补全器必须用整行，而不是 readline 传来的词。**

        `readline` 传进来的 `text` 是被切好的词；我们要的是整行，否则
        `/loop s` 的匹配依据会变成 `s`。
        """
        install_slash_completion()
        completer = self.calls["completer"]

        # 整行是 "/loop s"，但 readline 传来的词是 "s"
        self.calls["buffer"] = "/loop s"
        self.assertEqual(completer("s", 0), "/loop stop")

        # 若用了 text（"s"），会退回「列出全部」的第一条，即 "/loop"
        self.assertNotEqual(completer("s", 0), "/loop")

    def test_completer_returns_none_past_the_end(self) -> None:
        install_slash_completion()
        completer = self.calls["completer"]
        self.calls["buffer"] = "/co"
        self.assertIsNone(completer("/co", 99))

    def test_completer_walks_all_matches(self) -> None:
        install_slash_completion()
        completer = self.calls["completer"]
        self.calls["buffer"] = "/co"
        seen = [completer("/co", i) for i in range(4)]
        self.assertEqual(seen[:3], ["/config-paths", "/compact", "/collapse"])
        self.assertIsNone(seen[3])

    def test_missing_get_line_buffer_falls_back_to_text(self) -> None:
        """某些 readline 实现没有 `get_line_buffer`——不能因此崩掉。"""
        del self.fake.get_line_buffer
        self.assertTrue(install_slash_completion())
        completer = self.calls["completer"]
        # 没有 get_line_buffer 时退回用 text
        self.assertEqual(completer("/loop s", 0), "/loop stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
