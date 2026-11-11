"""仓库初始化（`/init`）的测试。

## 三条不可违反的性质，各自都有测试

**① 绝不覆盖已有文件。** `CORECODE.md` 可能写了大量项目知识，被模板覆盖是不可接受
的损失。`test_existing_mini_md_is_never_overwritten` 直接钉住这条。

**② `.gitignore` 只追加，不重写。** 用户已有的规则必须原样保留。

**③ 幂等。** 重复跑第二次必须是「全部 skipped」——否则每次 `/init` 都会
改动仓库，产生无意义的 diff。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from corecode.repo_init import (  # noqa: E402
    GITIGNORE_COMMENT,
    GITIGNORE_ENTRIES,
    detect_repo,
    ensure_gitignore_entries,
    initialize_repo,
    render_init_mini_md,
    render_init_report,
)

class InitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="corecode-init-")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name: str, content: str = "") -> Path:
        path = Path(self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read(self, name: str) -> str:
        return (Path(self.root) / name).read_text(encoding="utf-8")

class DetectRepoTests(InitTestCase):
    def test_empty_directory_detects_nothing(self) -> None:
        d = detect_repo(self.root)
        self.assertFalse(d.python)
        self.assertFalse(d.package_json)
        self.assertFalse(d.typescript)
        self.assertFalse(d.src_dir)

    def test_python_markers(self) -> None:
        for marker in ("pyproject.toml", "requirements.txt", "setup.py"):
            with self.subTest(marker=marker):
                sub = Path(self.root) / marker.replace(".", "_")
                sub.mkdir(parents=True, exist_ok=True)
                (sub / marker).write_text("", encoding="utf-8")
                self.assertTrue(detect_repo(str(sub)).python)

    def test_typescript_from_tsconfig(self) -> None:
        self.write("tsconfig.json", "{}")
        self.assertTrue(detect_repo(self.root).typescript)

    def test_typescript_from_package_json_dependency(self) -> None:
        self.write("package.json", '{"devDependencies": {"typescript": "^5"}}')
        d = detect_repo(self.root)
        self.assertTrue(d.typescript)
        self.assertTrue(d.package_json)

    def test_frameworks_from_package_json(self) -> None:
        self.write(
            "package.json",
            '{"dependencies": {"next": "14", "react": "18", "vite": "5", '
            '"@nestjs/core": "10"}}',
        )
        d = detect_repo(self.root)
        self.assertTrue(d.nextjs)
        self.assertTrue(d.react)
        self.assertTrue(d.vite)
        self.assertTrue(d.nest)

    def test_package_json_matching_is_case_insensitive(self) -> None:
        self.write("package.json", '{"dependencies": {"React": "18"}}')
        self.assertTrue(detect_repo(self.root).react)

    def test_rust_root_vs_workspace(self) -> None:
        self.write("Cargo.toml", "")
        d = detect_repo(self.root)
        self.assertTrue(d.rust_root)
        self.assertFalse(d.rust_workspace)

    def test_rust_subdirectory(self) -> None:
        self.write("rust/Cargo.toml", "")
        d = detect_repo(self.root)
        self.assertTrue(d.rust_workspace)
        self.assertTrue(d.rust_dir)

    def test_src_and_tests_directories(self) -> None:
        (Path(self.root) / "src").mkdir()
        (Path(self.root) / "tests").mkdir()
        d = detect_repo(self.root)
        self.assertTrue(d.src_dir)
        self.assertTrue(d.tests_dir)

    def test_corrupt_package_json_does_not_raise(self) -> None:
        """探测必须容错——一个坏掉的 package.json 不该让 `/init` 崩掉。"""
        self.write("package.json", "{ 这不是 JSON")
        d = detect_repo(self.root)
        self.assertTrue(d.package_json)
        self.assertFalse(d.react)

class RenderMiniMdTests(InitTestCase):
    def test_always_has_header_and_working_agreement(self) -> None:
        text = render_init_mini_md(self.root)
        self.assertIn("# CORECODE.md", text)
        self.assertIn("## Detected stack", text)
        self.assertIn("## Working agreement", text)

    def test_empty_repo_says_nothing_detected(self) -> None:
        text = render_init_mini_md(self.root)
        self.assertIn("No specific language markers were detected", text)
        self.assertIn("Frameworks: none detected", text)

    def test_python_repo_mentions_python_and_pyproject(self) -> None:
        self.write("pyproject.toml", "")
        text = render_init_mini_md(self.root)
        self.assertIn("Languages: Python", text)
        self.assertIn("pyproject.toml", text)

    def test_python_without_pyproject_gets_generic_advice(self) -> None:
        self.write("requirements.txt", "")
        text = render_init_mini_md(self.root)
        self.assertIn("Run the repo's Python test/lint commands", text)

    def test_typescript_replaces_javascript(self) -> None:
        """**两者互斥**：同时列出会让人以为有两个独立的语言层。"""
        self.write("tsconfig.json", "{}")
        self.write("package.json", "{}")
        text = render_init_mini_md(self.root)
        self.assertIn("TypeScript", text)
        self.assertNotIn("JavaScript/Node.js", text)

    def test_javascript_when_no_typescript(self) -> None:
        self.write("package.json", "{}")
        text = render_init_mini_md(self.root)
        self.assertIn("JavaScript/Node.js", text)

    def test_verification_section_appears_for_python(self) -> None:
        self.write("pyproject.toml", "")
        self.assertIn("## Verification", render_init_mini_md(self.root))

    def test_repository_shape_section_only_when_relevant(self) -> None:
        self.assertNotIn("## Repository shape", render_init_mini_md(self.root))
        (Path(self.root) / "src").mkdir()
        self.assertIn("## Repository shape", render_init_mini_md(self.root))

    def test_framework_notes_only_when_detected(self) -> None:
        self.assertNotIn("## Framework notes", render_init_mini_md(self.root))
        self.write("package.json", '{"dependencies": {"vite": "5"}}')
        self.assertIn("## Framework notes", render_init_mini_md(self.root))

    def test_react_note_is_skipped_for_nextjs(self) -> None:
        """Next.js 自带 React，再提一句 React 是噪音。"""
        self.write("package.json", '{"dependencies": {"next": "14", "react": "18"}}')
        text = render_init_mini_md(self.root)
        self.assertIn("Next.js detected", text)
        self.assertNotIn("React detected", text)

    def test_src_tests_note_when_both_present(self) -> None:
        (Path(self.root) / "src").mkdir()
        (Path(self.root) / "tests").mkdir()
        self.assertIn("update both surfaces together", render_init_mini_md(self.root))

    def test_rust_workspace_advice_mentions_subdirectory(self) -> None:
        self.write("rust/Cargo.toml", "")
        self.assertIn("from `rust/`", render_init_mini_md(self.root))

    def test_no_empty_sections(self) -> None:
        """探测不到的节**整节不写**，而不是留一个空标题。

        空标题看起来像「这里本该有内容但生成失败了」。
        """
        text = render_init_mini_md(self.root)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("## "):
                following = lines[index + 1] if index + 1 < len(lines) else ""
                self.assertTrue(
                    following.strip(), f"标题 {line!r} 下面没有内容"
                )

class InitializeRepoTests(InitTestCase):
    def test_creates_all_three_artifacts(self) -> None:
        report = initialize_repo(self.root)
        names = {a.name for a in report.artifacts}
        self.assertEqual(names, {".corecode/", ".gitignore", "CORECODE.md"})
        self.assertTrue((Path(self.root) / ".corecode").is_dir())
        self.assertTrue((Path(self.root) / "CORECODE.md").is_file())
        self.assertTrue((Path(self.root) / ".gitignore").is_file())

    def test_first_run_reports_created(self) -> None:
        report = initialize_repo(self.root)
        for artifact in report.artifacts:
            self.assertEqual(artifact.status, "created", artifact.name)

    def test_second_run_is_idempotent(self) -> None:
        """**幂等**：重复跑必须全是 skipped，且文件内容不变。"""
        initialize_repo(self.root)
        before = self.read("CORECODE.md")
        gitignore_before = self.read(".gitignore")

        report = initialize_repo(self.root)
        for artifact in report.artifacts:
            self.assertEqual(artifact.status, "skipped", artifact.name)
        self.assertEqual(self.read("CORECODE.md"), before)
        self.assertEqual(self.read(".gitignore"), gitignore_before)

    def test_existing_mini_md_is_never_overwritten(self) -> None:
        """**最重要的一条。** 用户的 CORECODE.md 可能写了大量项目知识。"""
        self.write("CORECODE.md", "# 我自己的说明\n\n非常宝贵的内容\n")
        report = initialize_repo(self.root)
        self.assertEqual(self.read("CORECODE.md"), "# 我自己的说明\n\n非常宝贵的内容\n")
        status = {a.name: a.status for a in report.artifacts}
        self.assertEqual(status["CORECODE.md"], "skipped")

    def test_gitignore_appends_without_losing_existing_rules(self) -> None:
        self.write(".gitignore", "node_modules/\n*.log\n")
        initialize_repo(self.root)
        content = self.read(".gitignore")
        self.assertIn("node_modules/", content)
        self.assertIn("*.log", content)
        self.assertIn(GITIGNORE_COMMENT, content)
        for entry in GITIGNORE_ENTRIES:
            self.assertIn(entry, content)

    def test_gitignore_update_is_reported_as_updated(self) -> None:
        self.write(".gitignore", "node_modules/\n")
        report = initialize_repo(self.root)
        status = {a.name: a.status for a in report.artifacts}
        self.assertEqual(status[".gitignore"], "updated")

    def test_existing_gitignore_with_entries_is_skipped(self) -> None:
        self.write(
            ".gitignore",
            "\n".join([GITIGNORE_COMMENT, *GITIGNORE_ENTRIES]) + "\n",
        )
        report = initialize_repo(self.root)
        status = {a.name: a.status for a in report.artifacts}
        self.assertEqual(status[".gitignore"], "skipped")

    def test_corecode_as_a_file_raises(self) -> None:
        """`.corecode` 已存在但是个文件 → 必须报错，不能覆盖。"""
        self.write(".corecode", "not a directory")
        with self.assertRaises(NotADirectoryError):
            initialize_repo(self.root)
        self.assertEqual(self.read(".corecode"), "not a directory")

    def test_gitignore_entries_are_not_duplicated(self) -> None:
        self.write(
            ".gitignore",
            "\n".join([GITIGNORE_COMMENT, *GITIGNORE_ENTRIES]) + "\n",
        )
        initialize_repo(self.root)
        content = self.read(".gitignore")
        for entry in GITIGNORE_ENTRIES:
            self.assertEqual(content.count(entry), 1, entry)

class GitignoreTests(InitTestCase):
    def test_creates_when_missing(self) -> None:
        path = os.path.join(self.root, ".gitignore")
        self.assertEqual(ensure_gitignore_entries(path), "created")
        self.assertTrue(os.path.isfile(path))

    def test_returns_skipped_when_complete(self) -> None:
        path = self.write(
            ".gitignore", "\n".join([GITIGNORE_COMMENT, *GITIGNORE_ENTRIES]) + "\n"
        )
        self.assertEqual(ensure_gitignore_entries(str(path)), "skipped")

    def test_returns_updated_when_partial(self) -> None:
        path = self.write(".gitignore", "node_modules/\n")
        self.assertEqual(ensure_gitignore_entries(str(path)), "updated")

    def test_partial_entries_are_completed(self) -> None:
        path = self.write(".gitignore", f"{GITIGNORE_COMMENT}\n")
        ensure_gitignore_entries(str(path))
        content = path.read_text(encoding="utf-8")
        for entry in GITIGNORE_ENTRIES:
            self.assertIn(entry, content)

    def test_trailing_whitespace_on_existing_lines_is_stripped(self) -> None:
        """

        这不是缺陷，但值得钉住——它意味着这个函数**会触碰**已有行。
        """
        path = self.write(".gitignore", "keep-me   \n")
        ensure_gitignore_entries(str(path))
        self.assertIn("keep-me\n", path.read_text(encoding="utf-8"))

class InitReportTests(unittest.TestCase):
    def test_report_shape(self) -> None:
        from corecode.repo_init import InitArtifact, InitReport

        report = InitReport(
            project_root="/tmp/proj",
            artifacts=[
                InitArtifact(".corecode/", "created"),
                InitArtifact(".gitignore", "updated"),
                InitArtifact("CORECODE.md", "skipped"),
            ],
        )
        text = render_init_report(report)
        self.assertIn("Init", text)
        self.assertIn("/tmp/proj", text)
        self.assertIn("created", text)
        self.assertIn("updated", text)
        self.assertIn("skipped (already exists)", text)
        self.assertIn("Next step", text)

    def test_report_labels_are_distinct(self) -> None:
        """三种状态必须显示成三种不同的文字，否则用户分不清发生了什么。"""
        from corecode.repo_init import InitArtifact, InitReport

        labels = set()
        for status in ("created", "updated", "skipped"):
            report = InitReport("/p", [InitArtifact("x", status)])
            labels.add(render_init_report(report).splitlines()[2])
        self.assertEqual(len(labels), 3)

if __name__ == "__main__":
    unittest.main(verbosity=2)
