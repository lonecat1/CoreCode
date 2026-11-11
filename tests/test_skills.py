"""Skills 的行为测试。

重点四块：
  1. 描述提取（无 front-matter，靠段落启发式）；
  2. 四层发现与优先级（同名时先到者胜）；
  3. **名字校验** —— 本实现相对原实现的加固，挡住路径穿越；
  4. 被 `load_skill` 工具与系统提示实际使用（否则「实现了」等于没有）。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.skills import (  # noqa: E402
    DEFAULT_DESCRIPTION,
    SOURCE_COMPAT_PROJECT,
    SOURCE_PROJECT,
    SOURCE_USER,
    SkillError,
    discover_skills,
    extract_description,
    install_skill,
    load_skill,
    managed_skill_root,
    remove_managed_skill,
    skill_roots,
    validate_skill_name,
)
from minicode.tooling import ToolContext  # noqa: E402
from minicode.tools import create_default_registry  # noqa: E402

unittest.TestCase.maxDiff = None

_TMP = Path(_HERE) / "_tmp" / "skills"


def write_skill(root: Path, name: str, body: str) -> Path:
    """在 `root/<name>/SKILL.md` 写一个技能。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


class SkillsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)
        self.cwd = _TMP / "project"
        self.home = _TMP / "home"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)

        # 注意：`skill_roots` 每次调用都重新解析家目录，
        # 所以这个 patch 对已经导入的模块同样生效。
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(_TMP, ignore_errors=True)


# ---------------------------------------------------------------------------
# 描述提取
# ---------------------------------------------------------------------------


class ExtractDescriptionTests(unittest.TestCase):
    def test_takes_first_non_heading_line(self) -> None:
        markdown = "# 标题\n\n这是描述。\n\n更多内容。"
        self.assertEqual(extract_description(markdown), "这是描述。")

    def test_skips_leading_headings(self) -> None:
        markdown = "# 标题\n## 副标题\n\n真正的描述"
        self.assertEqual(extract_description(markdown), "真正的描述")

    def test_paragraph_starting_with_heading_is_skipped_whole(self) -> None:
        """整段以标题开头时，**整段**跳过——不是只跳那一行。

        这条语义容易误判，所以显式钉住：`# 标题\\n正文行` 是一个段落，
        因为它以 `#` 开头，里面的「正文行」也不会被取用。
        与原 TS 实现一致（段落级过滤先于行级查找）。
        """
        markdown = "# 标题\n正文行\n\n后面的段落"
        self.assertEqual(extract_description(markdown), "后面的段落")

    def test_body_line_inside_heading_paragraph_is_not_used(self) -> None:
        markdown = "# 标题\n正文行"
        self.assertEqual(extract_description(markdown), DEFAULT_DESCRIPTION)

    def test_normal_paragraph_with_multiple_lines_uses_the_first(self) -> None:
        self.assertEqual(extract_description("第一行\n第二行"), "第一行")

    def test_prose_before_a_heading_wins(self) -> None:
        self.assertEqual(extract_description("前置说明\n\n# 后面有标题"), "前置说明")

    def test_strips_backticks(self) -> None:
        markdown = "# t\n\n用 `pytest` 跑测试"
        self.assertEqual(extract_description(markdown), "用 pytest 跑测试")

    def test_handles_crlf(self) -> None:
        markdown = "# t\r\n\r\n描述在 CRLF 文件里"
        self.assertEqual(extract_description(markdown), "描述在 CRLF 文件里")

    def test_falls_back_when_no_body(self) -> None:
        self.assertEqual(extract_description("# 只有标题"), DEFAULT_DESCRIPTION)

    def test_empty_input_falls_back(self) -> None:
        self.assertEqual(extract_description(""), DEFAULT_DESCRIPTION)


class BomHandlingTests(SkillsTestCase):
    """带 BOM 的 SKILL.md 必须被正常解析。

    这不是假想场景：Windows 工具（PowerShell 的 `Set-Content -Encoding utf8`）
    和不少编辑器都会写出 BOM。用普通 `utf-8` 读时 BOM 会留在正文最前面，
    于是 `# 标题` 不再以 `#` 开头、被当成描述行，系统提示里就会出现乱码描述。
    这个 bug 是在一次真实的 CLI 运行中被发现的。
    """

    def _write_with_bom(self, root: Path, name: str, body: str) -> None:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    def test_bom_does_not_become_the_description(self) -> None:
        self._write_with_bom(
            self.cwd / ".mini-code" / "skills",
            "bommed",
            "# 标题\n\n真正的描述\n",
        )
        found = {item.name: item for item in discover_skills(str(self.cwd))}
        self.assertIn("bommed", found)
        self.assertEqual(found["bommed"].description, "真正的描述")
        self.assertFalse(
            found["bommed"].description.startswith("\ufeff"),
            "描述里不该出现 BOM 字符",
        )

    def test_bom_is_stripped_from_loaded_content(self) -> None:
        self._write_with_bom(
            self.cwd / ".mini-code" / "skills", "bommed", "# 标题\n\n正文\n"
        )
        loaded = load_skill(str(self.cwd), "bommed")
        assert loaded is not None
        self.assertTrue(loaded.content.startswith("# 标题"))

    def test_bommed_file_installs_cleanly(self) -> None:
        """安装时也要吞掉 BOM，否则装完还是坏的。"""
        self._write_with_bom(_TMP / "external", "bommed", "# 标题\n\n正文\n")
        _name, target = install_skill(
            str(self.cwd), str(_TMP / "external" / "bommed"), scope="user"
        )
        content = Path(target).read_text(encoding="utf-8-sig")
        self.assertTrue(content.startswith("# 标题"))


# ---------------------------------------------------------------------------
# 名字校验（本实现的加固）
# ---------------------------------------------------------------------------


class ValidateSkillNameTests(unittest.TestCase):
    def test_accepts_ordinary_names(self) -> None:
        for name in ("review", "my-skill", "skill_2", "中文技能"):
            with self.subTest(name=name):
                self.assertEqual(validate_skill_name(name), name)

    def test_rejects_empty(self) -> None:
        for name in ("", "   "):
            with self.subTest(name=name):
                with self.assertRaises(SkillError):
                    validate_skill_name(name)

    def test_rejects_path_separators(self) -> None:
        for name in ("a/b", "a\\b", "../x", "..\\x"):
            with self.subTest(name=name):
                with self.assertRaises(SkillError):
                    validate_skill_name(name)

    def test_rejects_absolute_paths(self) -> None:
        with self.assertRaises(SkillError):
            validate_skill_name("/etc/passwd")

    def test_rejects_dot_names(self) -> None:
        for name in (".", "..", ".hidden"):
            with self.subTest(name=name):
                with self.assertRaises(SkillError):
                    validate_skill_name(name)


# ---------------------------------------------------------------------------
# 发现与优先级
# ---------------------------------------------------------------------------


class DiscoveryTests(SkillsTestCase):
    def test_finds_project_skill(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "review", "# review\n\n代码审查流程")
        found = discover_skills(str(self.cwd))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "review")
        self.assertEqual(found[0].source, SOURCE_PROJECT)
        self.assertEqual(found[0].description, "代码审查流程")

    def test_finds_user_skill(self) -> None:
        write_skill(self.home / "skills", "global", "# global\n\n全局技能")
        found = discover_skills(str(self.cwd))
        self.assertEqual(found[0].source, SOURCE_USER)

    def test_project_wins_over_user_for_same_name(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "dup", "# d\n\n项目版本")
        write_skill(self.home / "skills", "dup", "# d\n\n用户版本")
        found = discover_skills(str(self.cwd))
        self.assertEqual(len(found), 1, "同名只应保留一个")
        self.assertEqual(found[0].description, "项目版本")
        self.assertEqual(found[0].source, SOURCE_PROJECT)

    def test_claude_skills_are_compatible(self) -> None:
        write_skill(self.cwd / ".claude" / "skills", "legacy", "# l\n\n来自 Claude")
        found = discover_skills(str(self.cwd))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, SOURCE_COMPAT_PROJECT)

    def test_mini_code_wins_over_claude_for_same_name(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "dup", "# d\n\nMiniCode 版本")
        write_skill(self.cwd / ".claude" / "skills", "dup", "# d\n\nClaude 版本")
        found = discover_skills(str(self.cwd))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "MiniCode 版本")

    def test_directory_without_skill_md_is_ignored(self) -> None:
        (self.cwd / ".mini-code" / "skills" / "broken").mkdir(parents=True)
        self.assertEqual(discover_skills(str(self.cwd)), [])

    def test_empty_skill_md_is_ignored(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "empty", "   \n")
        self.assertEqual(discover_skills(str(self.cwd)), [])

    def test_missing_roots_yield_empty(self) -> None:
        self.assertEqual(discover_skills(str(self.cwd)), [])

    def test_roots_are_in_priority_order(self) -> None:
        roots = skill_roots(str(self.cwd))
        self.assertEqual(
            [source for _root, source in roots],
            ["project", "user", "compat_project", "compat_user"],
        )

    def test_category_directory_is_discovered_and_sets_default_category(self) -> None:
        write_skill(
            self.cwd / ".mini-code" / "skills" / "quality",
            "review",
            "# review\n\n代码审查流程",
        )
        found = discover_skills(str(self.cwd))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "review")
        self.assertEqual(found[0].category, "quality")
        loaded = load_skill(str(self.cwd), "review")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.metadata.category, "quality")


class LoadSkillTests(SkillsTestCase):
    def test_loads_content(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "review", "# review\n\n完整正文在这里")
        loaded = load_skill(str(self.cwd), "review")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertIn("完整正文在这里", loaded.content)

    def test_unknown_skill_returns_none(self) -> None:
        self.assertIsNone(load_skill(str(self.cwd), "nope"))

    def test_traversal_name_returns_none_not_a_file(self) -> None:
        """加固点的核心断言：`../x` 不能读到技能目录之外。"""
        secret = _TMP / "secret"
        write_skill(secret, "outside", "# s\n\n这是技能目录之外的内容")
        # 构造一个真实存在的越界目标
        self.assertIsNone(load_skill(str(self.cwd), "../secret"))
        self.assertIsNone(load_skill(str(self.cwd), "../secret/outside"))
        self.assertIsNone(load_skill(str(self.cwd), "..\\secret"))

    def test_empty_name_returns_none(self) -> None:
        self.assertIsNone(load_skill(str(self.cwd), "   "))

    def test_search_order_matches_priority(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "dup", "# d\n\n项目版本正文")
        write_skill(self.home / "skills", "dup", "# d\n\n用户版本正文")
        loaded = load_skill(str(self.cwd), "dup")
        assert loaded is not None
        self.assertIn("项目版本正文", loaded.content)


# ---------------------------------------------------------------------------
# 安装 / 移除
# ---------------------------------------------------------------------------


class InstallRemoveTests(SkillsTestCase):
    def test_install_from_directory(self) -> None:
        source = write_skill(_TMP / "external", "cool", "# cool\n\n外部技能")
        name, target = install_skill(str(self.cwd), str(source), scope="user")
        self.assertEqual(name, "cool")
        self.assertTrue(Path(target).is_file())
        self.assertIn("外部技能", Path(target).read_text(encoding="utf-8"))

    def test_install_infers_name_from_directory(self) -> None:
        source = write_skill(_TMP / "external", "inferred", "# i\n\n内容")
        name, _target = install_skill(str(self.cwd), str(source))
        self.assertEqual(name, "inferred")

    def test_install_accepts_direct_skill_md_path(self) -> None:
        source = write_skill(_TMP / "external", "direct", "# d\n\n内容")
        name, _target = install_skill(str(self.cwd), str(source / "SKILL.md"))
        self.assertEqual(name, "direct")

    def test_install_with_explicit_name(self) -> None:
        source = write_skill(_TMP / "external", "original", "# o\n\n内容")
        name, _target = install_skill(str(self.cwd), str(source), name="renamed")
        self.assertEqual(name, "renamed")

    def test_install_to_project_scope(self) -> None:
        source = write_skill(_TMP / "external", "proj", "# p\n\n内容")
        _name, target = install_skill(
            str(self.cwd), str(source), scope="project"
        )
        self.assertIn(".mini-code", target)
        self.assertTrue(Path(target).is_file())

    def test_install_rejects_bad_name(self) -> None:
        source = write_skill(_TMP / "external", "ok", "# o\n\n内容")
        with self.assertRaises(SkillError):
            install_skill(str(self.cwd), str(source), name="../evil")

    def test_install_missing_source_raises(self) -> None:
        with self.assertRaises(SkillError):
            install_skill(str(self.cwd), str(_TMP / "does-not-exist"))

    def test_installed_skill_is_then_discoverable(self) -> None:
        """安装的最终意义是「能被发现并使用」。"""
        source = write_skill(_TMP / "external", "roundtrip", "# r\n\n往返验证")
        install_skill(str(self.cwd), str(source), scope="user")
        found = {item.name: item for item in discover_skills(str(self.cwd))}
        self.assertIn("roundtrip", found)
        self.assertEqual(found["roundtrip"].description, "往返验证")

    def test_remove_managed_skill(self) -> None:
        source = write_skill(_TMP / "external", "temp", "# t\n\n内容")
        install_skill(str(self.cwd), str(source), scope="user")
        removed, target = remove_managed_skill(str(self.cwd), "temp")
        self.assertTrue(removed)
        self.assertFalse(Path(target).exists())
        self.assertEqual(discover_skills(str(self.cwd)), [])

    def test_remove_missing_returns_false(self) -> None:
        removed, _target = remove_managed_skill(str(self.cwd), "nope")
        self.assertFalse(removed)

    def test_remove_rejects_bad_name(self) -> None:
        with self.assertRaises(SkillError):
            remove_managed_skill(str(self.cwd), "../evil")

    def test_managed_root_respects_scope(self) -> None:
        self.assertEqual(managed_skill_root("project", str(self.cwd)), self.cwd / ".mini-code" / "skills")
        self.assertEqual(managed_skill_root("user", str(self.cwd)), self.home / "skills")


# ---------------------------------------------------------------------------
# 接线：工具与系统提示真的用上了技能
# ---------------------------------------------------------------------------


class SkillsWiringTests(SkillsTestCase):
    def test_load_skill_tool_is_always_registered(self) -> None:
        """即使不传 cwd 也要注册：子 Agent 白名单里有它，缺了就会 Unknown tool。"""
        registry = create_default_registry()
        self.assertIsNotNone(registry.find("load_skill"))

    def test_load_skill_tool_reads_content(self) -> None:
        write_skill(self.cwd / ".mini-code" / "skills", "review", "# r\n\n工具读取的正文")
        registry = create_default_registry(str(self.cwd))
        result = registry.execute(
            "load_skill", {"name": "review"}, ToolContext(cwd=str(self.cwd))
        )
        self.assertTrue(result.ok)
        self.assertIn("SKILL: review", result.output)
        self.assertIn("SOURCE: project", result.output)
        self.assertIn("PATH:", result.output)
        self.assertIn("工具读取的正文", result.output)

    def test_load_skill_tool_reports_unknown(self) -> None:
        registry = create_default_registry(str(self.cwd))
        result = registry.execute(
            "load_skill", {"name": "nope"}, ToolContext(cwd=str(self.cwd))
        )
        self.assertFalse(result.ok)
        self.assertIn("Unknown skill", result.output)

    def test_load_skill_tool_rejects_traversal(self) -> None:
        registry = create_default_registry(str(self.cwd))
        result = registry.execute(
            "load_skill", {"name": "../secret"}, ToolContext(cwd=str(self.cwd))
        )
        self.assertFalse(result.ok, "越界名字必须失败")

    def test_registry_exposes_discovered_skills(self) -> None:
        """系统提示靠 `registry.skills()` 列出可用技能。"""
        write_skill(self.cwd / ".mini-code" / "skills", "review", "# r\n\n代码审查")
        registry = create_default_registry(str(self.cwd))
        skills = registry.skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "review")

    def test_system_prompt_lists_skills(self) -> None:
        from minicode.prompt import build_system_prompt

        write_skill(self.cwd / ".mini-code" / "skills", "review", "# r\n\n代码审查流程")
        registry = create_default_registry(str(self.cwd))
        text = build_system_prompt(str(self.cwd), skills=registry.skills())
        self.assertIn("Available skill catalog:", text)
        self.assertIn("general: 1", text)
        self.assertNotIn("review: 代码审查流程", text)

    def test_system_prompt_says_none_when_empty(self) -> None:
        from minicode.prompt import build_system_prompt

        text = build_system_prompt(str(self.cwd))
        self.assertIn("Available skill catalog: none discovered.", text)

    def test_worker_can_use_skills(self) -> None:
        """子 Agent 白名单里包含 load_skill —— 已随本模块落地。"""
        from minicode.agents.types import PENDING_SUBAGENT_TOOLS, SUBAGENT_TOOL_NAMES
        from minicode.tools import create_worker_registry

        self.assertIn("load_skill", SUBAGENT_TOOL_NAMES)
        self.assertEqual(PENDING_SUBAGENT_TOOLS, (), "已无待补工具")
        worker = create_worker_registry(create_default_registry(str(self.cwd)))
        self.assertIsNotNone(worker.find("load_skill"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
