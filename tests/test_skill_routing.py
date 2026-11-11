"""Skill 分层路由的行为测试。

重点验证不是“有排序函数”，而是：

  - 元信息能解析；
  - 不相关 Skill 不进入最终上下文；
  - 两个高度重叠 Skill 不会同时占满名额；
  - 大量 Skill 下仍有候选上限和 Token 预算；
  - 自进化生成的 Skill 自带可路由元信息。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.agent_loop import run_agent_turn  # noqa: E402
from minicode.agents.manager import SubAgentManager  # noqa: E402
from minicode.evolution import EvolvedMemory, MemoryAsset  # noqa: E402
from minicode.host import SessionHost  # noqa: E402
from minicode.permissions import PermissionManager  # noqa: E402
from minicode.prompt import build_system_prompt  # noqa: E402
from minicode.runtime.plan import PlanManager  # noqa: E402
from minicode.runtime.turn import TurnRequest  # noqa: E402
from minicode.skill_metadata import parse_skill_metadata, split_frontmatter  # noqa: E402
from minicode.skill_routing import SkillRouter, detect_intents  # noqa: E402
from minicode.skills import discover_skills  # noqa: E402
from minicode.tooling import ToolContext  # noqa: E402
from minicode.tools import create_default_registry, create_worker_registry  # noqa: E402
from minicode.types import AgentStep  # noqa: E402


def write_skill(root: Path, name: str, markdown: str) -> None:
    directory = root / ".mini-code" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(markdown, encoding="utf-8")


def review_skill(description: str = "Review Python changes for security") -> str:
    return f"""---
description: {description}
category: quality
tags: [python, security, review]
intents: [code_review, security]
when_to_use: Reviewing Python pull requests and security-sensitive changes
when_not_to_use: Production deployment and release operations
examples: ["审查这个 Python 补丁", "check security of this PR"]
priority: 0.9
capabilities: [read_file, grep_files]
---

# Review

Inspect the patch and report concrete risks.
"""


class SkillRoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="minicode-skill-routing-"))
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.home.mkdir()
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class MetadataTests(SkillRoutingTestCase):
    def test_frontmatter_metadata_is_parsed(self) -> None:
        data, body = split_frontmatter(review_skill())
        self.assertEqual(data["category"], "quality")
        self.assertEqual(data["tags"], ["python", "security", "review"])
        self.assertIn("# Review", body)

        metadata = parse_skill_metadata(
            review_skill(),
            name="review",
            fallback_description="fallback",
        )
        self.assertEqual(metadata.category, "quality")
        self.assertIn("security", metadata.tags)
        self.assertIn("code_review", metadata.intents)
        self.assertIn("deployment", metadata.when_not_to_use.lower())
        self.assertIn("Python", metadata.examples[0])
        self.assertEqual(metadata.priority, 0.9)
        self.assertEqual(metadata.capabilities, ["read_file", "grep_files"])

    def test_legacy_skill_falls_back_to_derived_metadata(self) -> None:
        write_skill(
            self.cwd,
            "debug-runtime",
            "# Debug\n\n排查 Python 运行时错误并验证修复。\n\n## When to use\n\n运行时报错。",
        )
        found = discover_skills(str(self.cwd))[0]
        self.assertEqual(found.category, "general")
        self.assertIn("debugging", found.intents or [])
        self.assertIn("运行时报错", found.when_to_use)

    def test_detects_mixed_language_intents(self) -> None:
        self.assertIn("code_review", detect_intents("审查这个补丁的安全性"))
        self.assertIn("verification", detect_intents("run tests and verify"))
        self.assertIn("verification", detect_intents("please run tests"))
        self.assertNotIn("verification", detect_intents("show the latest change"))


class RecallAndRerankTests(SkillRoutingTestCase):
    def test_relevant_skill_beats_unrelated_catalog_entry(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        write_skill(
            self.cwd,
            "deploy-prod",
            """---
description: Deploy production release
category: operations
tags: [deploy, release, production]
intents: [deployment]
when_to_use: Release a production build
when_not_to_use: Review source code
examples: ["发布生产版本"]
priority: 0.9
---

# Deploy
""",
        )
        registry = create_default_registry(str(self.cwd))
        router = SkillRouter(
            str(self.cwd), tool_definitions=registry.list()
        )
        route = router.route("审查 Python 补丁安全")
        self.assertTrue(route.candidates)
        self.assertEqual(route.candidates[0].skill.name, "review-python")
        self.assertNotIn("deploy-prod", [item.skill.name for item in route.candidates])
        self.assertIn("read_file", route.suggested_tools)
        self.assertIn("grep_files", route.suggested_tools)

    def test_boundary_penalty_changes_the_winner(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        write_skill(
            self.cwd,
            "deploy-prod",
            """---
description: Deploy production release safely
category: operations
tags: [deploy, production, release]
intents: [deployment]
when_to_use: Production deployment
when_not_to_use: Source review
examples: ["部署生产环境"]
priority: 0.7
---

# Deploy
""",
        )
        router = SkillRouter(str(self.cwd))
        route = router.route("production deployment release")
        self.assertEqual(route.candidates[0].skill.name, "deploy-prod")

    def test_overlapping_skills_are_deduplicated(self) -> None:
        write_skill(self.cwd, "review-a", review_skill())
        write_skill(
            self.cwd,
            "review-b",
            review_skill("Review Python changes for security review"),
        )
        router = SkillRouter(str(self.cwd))
        route = router.route("审查 Python security review", top_k=2)
        names = [item.skill.name for item in route.candidates]
        self.assertEqual(len(names), 1, names)
        self.assertGreaterEqual(route.overlap_dropped, 1)

    def test_stage1_and_context_have_hard_budgets(self) -> None:
        for index in range(45):
            write_skill(
                self.cwd,
                f"generic-{index:02d}",
                f"""---
description: Generic workflow number {index}
category: cat-{index % 6}
tags: [topic-{index}, shared]
intents: [implementation]
when_to_use: Generic implementation task {index}
priority: 0.5
---

# Generic {index}
""",
            )
        write_skill(self.cwd, "review-python", review_skill())
        router = SkillRouter(str(self.cwd), context_budget=900)
        route = router.route("审查 Python 补丁 security", top_k=3)
        self.assertEqual(route.total_skills, 46)
        self.assertLessEqual(route.stage1_count, 32)
        self.assertLessEqual(len(route.candidates), 3)
        context = router.format_context(route)
        self.assertIn("<skill_routing>", context)
        self.assertLessEqual(len(context), 1_200)

    def test_catalog_does_not_inline_every_skill(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        write_skill(self.cwd, "deploy-prod", "# Deploy\n\nProduction release.")
        router = SkillRouter(str(self.cwd))
        catalog = router.catalog_text()
        self.assertIn("2 skills", catalog)
        self.assertNotIn("review-python", catalog)


class PromptAndToolTests(SkillRoutingTestCase):
    def test_system_prompt_uses_catalog_summary(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        registry = create_default_registry(str(self.cwd))
        prompt = build_system_prompt(str(self.cwd), skills=registry.skills())
        self.assertIn("Available skill catalog: 1 skills", prompt)
        self.assertIn("quality: 1", prompt)
        self.assertNotIn("review-python: Review Python", prompt)

    def test_load_skill_still_loads_exact_name(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        registry = create_default_registry(str(self.cwd))
        result = registry.execute(
            "load_skill",
            {"name": "review-python"},
            ToolContext(cwd=str(self.cwd)),
        )
        self.assertTrue(result.ok, result.output)
        self.assertIn("Inspect the patch", result.output)
        self.assertNotIn("category: quality", result.output)

    def test_agent_loop_accepts_skill_context_projection(self) -> None:
        seen: list[str] = []

        class Model:
            def next(self, messages, **_kwargs):
                seen.append(messages[0]["content"])
                return AgentStep(type="assistant", content="ok", kind="final")

        run_agent_turn(
            model=Model(),
            tools=create_default_registry(str(self.cwd)),
            messages=[
                {"role": "system", "content": "base"},
                {"role": "user", "content": "review"},
            ],
            cwd=str(self.cwd),
            skill_context=lambda messages: [
                {**messages[0], "content": messages[0]["content"] + "\n<skill_routing>hit</skill_routing>"},
                *messages[1:],
            ],
        )
        self.assertIn("<skill_routing>", seen[0])

    def test_host_injects_routed_skill_context_per_turn(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        seen: list[list[dict]] = []

        class Model:
            def next(self, messages, **_kwargs):
                seen.append([dict(message) for message in messages])
                return AgentStep(type="assistant", content="ok", kind="final")

        plan = PlanManager("s1")
        host = SessionHost(
            cwd=str(self.cwd),
            model=Model(),
            runtime_config=None,
            permissions=PermissionManager(str(self.cwd), store_path=None),
            tools=create_default_registry(str(self.cwd), plan),
            plan=plan,
            session_id="s1",
        )
        host.execute(
            TurnRequest(input={"role": "user", "content": "审查这个 Python 补丁的安全问题"})
        )
        system_text = seen[0][0]["content"]
        self.assertIn("<skill_routing>", system_text)
        self.assertIn("review-python", system_text)
        self.assertIn("Not for:", system_text)

    def test_sub_agent_receives_task_scoped_skill_route(self) -> None:
        write_skill(self.cwd, "review-python", review_skill())
        seen: list[str] = []

        class WorkerModel:
            def next(self, messages, **_kwargs):
                seen.append(messages[0]["content"])
                return AgentStep(type="assistant", content="worker done", kind="final")

        manager = SubAgentManager(
            model=WorkerModel(),
            tools=create_worker_registry(create_default_registry(str(self.cwd))),
            cwd=str(self.cwd),
        )
        snapshot = manager.spawn("审查 Python 安全补丁")
        manager.wait([snapshot.id], timeout_ms=5_000)
        self.assertTrue(seen)
        self.assertIn("<skill_routing>", seen[0])
        self.assertIn("review-python", seen[0])

    def test_evolved_skill_has_routable_metadata(self) -> None:
        engine = EvolvedMemory(str(self.cwd), home=self.home)
        procedure = MemoryAsset(
            kind="procedural",
            title="Workflow: read_file -> run_command",
            content="successful workflow",
            summary="Run tests after reading the source",
            tags=["pytest", "python"],
            metadata={
                "steps": [
                    {"tool": "read_file", "input": "path=src/app.py"},
                    {"tool": "run_command", "input": "command=pytest"},
                ],
                "examples": ["修复 Python 测试失败"],
            },
            occurrences=2,
        )
        markdown = engine._skill_markdown(procedure, "evolved-test")  # noqa: SLF001
        metadata = parse_skill_metadata(
            markdown,
            name="evolved-test",
            fallback_description="fallback",
        )
        self.assertEqual(metadata.category, "evolved")
        self.assertIn("pytest", metadata.tags)
        self.assertIn("verification", metadata.intents)
        self.assertIn("run_command", metadata.capabilities)
        self.assertTrue(metadata.examples)


if __name__ == "__main__":
    unittest.main(verbosity=2)
