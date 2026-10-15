"""本地工具快捷方式的解析测试。

对应原版 `MiniCode/src/local-tool-shortcuts.ts`。

## 为什么要覆盖这么多边角

解析器的输入是**用户手敲的一行字**，格式错误的方式比正确的方式多得多。而且
它的失败是「静默交给模型」——用户以为自己在调工具，实际变成了一次普通提问，
模型可能给出完全无关的回答。因此每一种分隔符写法都要有一个明确的断言。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from minicode.local_tool_shortcuts import (  # noqa: E402
    SHORTCUT_USAGES,
    LocalToolShortcut,
    parse_local_tool_shortcut,
)


class ParseHelper:
    def parse(self, text: str) -> LocalToolShortcut | None:
        return parse_local_tool_shortcut(text)


class NotAShortcutTests(unittest.TestCase, ParseHelper):
    """不是快捷方式的输入必须原样返回 `None`，交给后面的分派。"""

    def test_plain_question(self) -> None:
        self.assertIsNone(self.parse("帮我看看这个项目"))

    def test_empty_string(self) -> None:
        self.assertIsNone(self.parse(""))

    def test_other_slash_commands(self) -> None:
        for command in ("/help", "/status", "/tools", "/plan", "/memory", "/exit"):
            self.assertIsNone(self.parse(command), command)

    def test_slash_command_with_argument(self) -> None:
        self.assertIsNone(self.parse("/resume ac73346d"))

    def test_ls_prefix_is_not_greedy(self) -> None:
        """**`/lsof` 不能被当成 `/ls`。**

        原版用 `input === '/ls' || input.startsWith('/ls ')`，靠那个空格把
        `/lsof` 挡在外面。少了空格判断，任何以 `/ls` 开头的词都会被吃掉。
        """
        self.assertIsNone(self.parse("/lsof"))
        self.assertIsNone(self.parse("/lswhatever"))

    def test_read_without_space_is_not_a_shortcut(self) -> None:
        self.assertIsNone(self.parse("/read"))
        self.assertIsNone(self.parse("/grep"))
        self.assertIsNone(self.parse("/write"))
        self.assertIsNone(self.parse("/edit"))

    def test_bare_command_words(self) -> None:
        for word in ("ls", "read a.txt", "cmd git status"):
            self.assertIsNone(self.parse(word), word)


class ListFilesTests(unittest.TestCase, ParseHelper):
    def test_bare_ls(self) -> None:
        result = self.parse("/ls")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "list_files")
        self.assertEqual(result.input, {})

    def test_ls_with_path(self) -> None:
        result = self.parse("/ls src")
        self.assertEqual(result.tool_name, "list_files")
        self.assertEqual(result.input, {"path": "src"})

    def test_ls_trims_whitespace(self) -> None:
        result = self.parse("/ls    src   ")
        self.assertEqual(result.input, {"path": "src"})

    def test_ls_with_only_spaces_has_no_path(self) -> None:
        result = self.parse("/ls   ")
        self.assertEqual(result.input, {})


class GrepTests(unittest.TestCase, ParseHelper):
    def test_pattern_only(self) -> None:
        result = self.parse("/grep TODO")
        self.assertEqual(result.tool_name, "grep_files")
        self.assertEqual(result.input, {"pattern": "TODO"})

    def test_pattern_and_path(self) -> None:
        result = self.parse("/grep TODO::src")
        self.assertEqual(result.input, {"pattern": "TODO", "path": "src"})

    def test_empty_pattern_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/grep "))
        self.assertIsNone(self.parse("/grep ::src"))

    def test_trailing_separator_yields_no_path(self) -> None:
        result = self.parse("/grep TODO::")
        self.assertEqual(result.input, {"pattern": "TODO"})

    def test_extra_separators_stay_in_the_path(self) -> None:
        """**D35**：原版会把第三段及以后整个丢掉。

        `/grep a::b::c` 在原版得到 `pattern='a', path='b'`，`c` 消失；
        本实现保留在 path 里。
        """
        result = self.parse("/grep a::b::c")
        self.assertEqual(result.input, {"pattern": "a", "path": "b::c"})


class ReadTests(unittest.TestCase, ParseHelper):
    def test_read(self) -> None:
        result = self.parse("/read pyproject.toml")
        self.assertEqual(result.tool_name, "read_file")
        self.assertEqual(result.input, {"path": "pyproject.toml"})

    def test_empty_path_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/read "))
        self.assertIsNone(self.parse("/read    "))

    def test_path_is_trimmed(self) -> None:
        self.assertEqual(self.parse("/read  a.txt  ").input, {"path": "a.txt"})


class WriteAndModifyTests(unittest.TestCase, ParseHelper):
    def test_write(self) -> None:
        result = self.parse("/write a.txt::hello")
        self.assertEqual(result.tool_name, "write_file")
        self.assertEqual(result.input, {"path": "a.txt", "content": "hello"})

    def test_write_keeps_separators_in_content(self) -> None:
        """内容里的 `::` 必须完整保留。"""
        result = self.parse("/write a.txt::x::y::z")
        self.assertEqual(result.input["content"], "x::y::z")

    def test_write_empty_content_is_allowed(self) -> None:
        result = self.parse("/write a.txt::")
        self.assertEqual(result.input, {"path": "a.txt", "content": ""})

    def test_write_without_separator_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/write a.txt"))

    def test_write_with_empty_path_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/write ::content"))

    def test_modify(self) -> None:
        result = self.parse("/modify a.txt::new body")
        self.assertEqual(result.tool_name, "modify_file")
        self.assertEqual(result.input, {"path": "a.txt", "content": "new body"})

    def test_modify_keeps_separators_in_content(self) -> None:
        result = self.parse("/modify a.txt::a::b")
        self.assertEqual(result.input["content"], "a::b")


class EditTests(unittest.TestCase, ParseHelper):
    def test_edit(self) -> None:
        result = self.parse("/edit a.txt::old::new")
        self.assertEqual(result.tool_name, "edit_file")
        self.assertEqual(
            result.input, {"path": "a.txt", "search": "old", "replace": "new"}
        )

    def test_edit_allows_empty_replace(self) -> None:
        """`replace` 为空 = 删除，是合法用法。"""
        result = self.parse("/edit a.txt::old::")
        self.assertEqual(result.input["replace"], "")

    def test_edit_allows_empty_search_here(self) -> None:
        """解析层不挡空 search——那是工具校验层的职责。

        解析层只管「这行输入的形状对不对」，值域校验交给工具的 validator。
        两处都挡会让错误信息变得含糊（用户不知道是命令写错了还是参数非法）。
        """
        result = self.parse("/edit a.txt::::new")
        self.assertEqual(result.input, {"path": "a.txt", "search": "", "replace": "new"})

    def test_edit_requires_three_parts(self) -> None:
        self.assertIsNone(self.parse("/edit a.txt::old"))
        self.assertIsNone(self.parse("/edit a.txt"))

    def test_edit_requires_path(self) -> None:
        self.assertIsNone(self.parse("/edit ::old::new"))

    def test_edit_keeps_separators_in_replace(self) -> None:
        """**D35 的核心用例。**

        原版 `split('::')` 解构三段，`/edit a::b::c::d` 的 `d` 会被静默丢掉。
        替换成含 `::` 的内容很常见（Python 切片 `a[1::2]`、URL、命名空间），
        静默截断属于悄悄改坏数据。
        """
        result = self.parse("/edit a::b::c::d")
        self.assertEqual(
            result.input, {"path": "a", "search": "b", "replace": "c::d"}
        )

    def test_edit_slice_syntax_case(self) -> None:
        """真实场景：把 `x[1:2]` 改成 `x[1::2]`。"""
        result = self.parse("/edit m.py::x[1:2]::x[1::2]")
        self.assertEqual(result.input["search"], "x[1:2]")
        self.assertEqual(result.input["replace"], "x[1::2]")


class PatchTests(unittest.TestCase, ParseHelper):
    def test_single_pair(self) -> None:
        result = self.parse("/patch a.txt::old::new")
        self.assertEqual(result.tool_name, "patch_file")
        self.assertEqual(
            result.input,
            {"path": "a.txt", "replacements": [{"search": "old", "replace": "new"}]},
        )

    def test_multiple_pairs(self) -> None:
        result = self.parse("/patch a.txt::s1::r1::s2::r2")
        self.assertEqual(
            result.input["replacements"],
            [
                {"search": "s1", "replace": "r1"},
                {"search": "s2", "replace": "r2"},
            ],
        )

    def test_odd_pair_count_is_rejected(self) -> None:
        """奇数个片段说明漏写了一段——宁可拒绝，也不要猜。"""
        self.assertIsNone(self.parse("/patch a.txt::s1::r1::s2"))

    def test_too_few_parts_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/patch a.txt::s1"))
        self.assertIsNone(self.parse("/patch a.txt"))

    def test_empty_path_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/patch ::s1::r1"))

    def test_replacements_have_no_replace_all_key(self) -> None:
        """原版不设 `replaceAll`，因此一律是「只替换第一处」。

        凭空塞一个键会改变工具行为（全部替换），那是超出用户输入的意思。
        """
        result = self.parse("/patch a.txt::s::r")
        self.assertNotIn("replaceAll", result.input["replacements"][0])


class CommandTests(unittest.TestCase, ParseHelper):
    def test_command_with_args(self) -> None:
        result = self.parse("/cmd git status --short")
        self.assertEqual(result.tool_name, "run_command")
        self.assertEqual(
            result.input, {"command": "git", "args": ["status", "--short"]}
        )

    def test_command_with_cwd(self) -> None:
        """**这就是「在另一个目录里跑命令」的入口。**

        格式是 `[cwd::]<command>`：`::` **之前**是工作目录。
        """
        result = self.parse("/cmd src::git status")
        self.assertEqual(
            result.input, {"command": "git", "args": ["status"], "cwd": "src"}
        )

    def test_command_without_args(self) -> None:
        result = self.parse("/cmd pwd")
        self.assertEqual(result.input, {"command": "pwd", "args": []})

    def test_empty_command_is_rejected(self) -> None:
        self.assertIsNone(self.parse("/cmd "))
        self.assertIsNone(self.parse("/cmd ::"))

    def test_cwd_is_omitted_when_not_given(self) -> None:
        """不给 cwd 时不该塞一个空字符串进去——那会让工具去解析空路径。"""
        result = self.parse("/cmd git status")
        self.assertNotIn("cwd", result.input)

    def test_quotes_are_not_parsed(self) -> None:
        """原版用朴素空白切分，不做引号解析。保持一致。

        需要复杂引号时应当直接用 `run_command` 工具（它的 `args` 数组不做拆分）。
        """
        result = self.parse('/cmd git commit -m "a b"')
        self.assertEqual(result.input["args"], ["commit", "-m", '"a', 'b"'])


class UsageListTests(unittest.TestCase):
    def test_all_eight_shortcuts_are_listed(self) -> None:
        usages = [usage for usage, _ in SHORTCUT_USAGES]
        self.assertEqual(len(usages), 8)
        for expected in (
            "/ls [path]",
            "/grep <pattern>::[path]",
            "/read <path>",
            "/write <path>::<content>",
            "/modify <path>::<content>",
            "/edit <path>::<search>::<replace>",
            "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...",
            "/cmd [cwd::]<command> [args...]",
        ):
            self.assertIn(expected, usages, expected)

    def test_descriptions_are_not_empty(self) -> None:
        for usage, description in SHORTCUT_USAGES:
            self.assertTrue(description.strip(), usage)

    def test_cmd_description_mentions_another_directory(self) -> None:
        """`/cmd` 的说明必须点出「可以在别的目录跑」——这是它的关键能力。"""
        for usage, description in SHORTCUT_USAGES:
            if usage.startswith("/cmd"):
                self.assertIn("another directory", description)
                return
        self.fail("/cmd 不在用法列表里")

    def test_every_listed_usage_actually_parses(self) -> None:
        """**用法列表与解析器必须一致。**

        帮助里列出但解析不出来的用法，是对用户的直接误导。
        """
        samples = {
            "/ls [path]": "/ls src",
            "/grep <pattern>::[path]": "/grep TODO::src",
            "/read <path>": "/read a.txt",
            "/write <path>::<content>": "/write a.txt::x",
            "/modify <path>::<content>": "/modify a.txt::x",
            "/edit <path>::<search>::<replace>": "/edit a.txt::s::r",
            "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...": (
                "/patch a.txt::s1::r1::s2::r2"
            ),
            "/cmd [cwd::]<command> [args...]": "/cmd src::git status",
        }
        for usage, _ in SHORTCUT_USAGES:
            sample = samples[usage]
            result = parse_local_tool_shortcut(sample)
            self.assertIsNotNone(result, f"{usage} 的示例 {sample!r} 解析失败")

    def test_parsed_tool_names_are_real_tools(self) -> None:
        """**快捷方式指向的工具必须真的存在。**

        指向不存在的工具时，用户会看到「Unknown tool」而不是语法错误，
        这会让人以为是程序坏了。
        """
        from minicode.tools import create_default_registry

        registered = {t.name for t in create_default_registry(".").list()}
        for sample in (
            "/ls",
            "/grep TODO",
            "/read a.txt",
            "/write a.txt::x",
            "/modify a.txt::x",
            "/edit a.txt::s::r",
            "/patch a.txt::s::r",
            "/cmd pwd",
        ):
            result = parse_local_tool_shortcut(sample)
            self.assertIsNotNone(result, sample)
            self.assertIn(
                result.tool_name, registered, f"{sample} 指向未注册的工具"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
