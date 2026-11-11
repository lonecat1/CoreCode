"""自进化记忆沉淀的行为测试。

覆盖完整闭环：

    执行轨迹 → 反思 → 四类资产 → 分类持久化 → 索引 → 召回 → Skill 生长

重点不是「字段存在」，而是跨实例、跨会话之后确实还能找到并复用。
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

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from minicode.agent_loop import run_agent_turn  # noqa: E402
from minicode.evolution import (  # noqa: E402
    MEMORY_KIND_EPISODIC,
    MEMORY_KIND_PROCEDURAL,
    MEMORY_KIND_PROFILE,
    MEMORY_KIND_REPAIR,
    MEMORY_KIND_SKILL,
    EvolvedMemory,
    redact_sensitive,
    reflect_turn,
)
from minicode.evolution.index import tokenize  # noqa: E402
from minicode.host import SessionHost  # noqa: E402
from minicode.permissions import PermissionManager  # noqa: E402
from minicode.runtime.plan import PlanManager  # noqa: E402
from minicode.runtime.turn import TurnRequest  # noqa: E402
from minicode.tools import create_default_registry  # noqa: E402
from minicode.types import AgentStep, ToolCall  # noqa: E402


def _call(call_id: str, name: str, payload: dict) -> dict:
    return {
        "role": "assistant_tool_call",
        "id": f"call-message-{call_id}",
        "toolUseId": call_id,
        "toolName": name,
        "input": payload,
    }


def _result(call_id: str, name: str, output: str, *, error: bool = False) -> dict:
    return {
        "role": "tool_result",
        "id": f"result-message-{call_id}",
        "toolUseId": call_id,
        "toolName": name,
        "content": output,
        "isError": error,
    }


def _turn(
    request: str,
    *,
    error_signature: str = "ModuleNotFoundError: No module named 'pytest'",
) -> list[dict]:
    return [
        {"role": "user", "id": "u1", "content": request},
        _call("c1", "read_file", {"path": "src/app.py"}),
        _result("c1", "read_file", "print('hello')"),
        _call("c2", "run_command", {"command": "pytest", "args": ["-q"]}),
        _result("c2", "run_command", error_signature, error=True),
        _call("c3", "run_command", {"command": "python3", "args": ["-m", "pytest", "-q"]}),
        _result("c3", "run_command", "7 passed"),
        {
            "role": "assistant",
            "id": "a1",
            "content": "测试通过，问题已修复。",
        },
    ]


class EvolutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="minicode-evolution-"))
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.home.mkdir()
        self.cwd.mkdir()
        self._env = patch.dict(os.environ, {"MINICODE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class ReflectionTests(EvolutionTestCase):
    def test_extracts_all_four_asset_categories(self) -> None:
        reflection = reflect_turn(
            _turn("以后请始终用 pytest 验证；这个报错帮我修一下。"),
            cwd=str(self.cwd),
            session_id="s1",
            user_text="以后请始终用 pytest 验证；这个报错帮我修一下。",
            outcome="final",
        )
        kinds = {asset.kind for asset in reflection.assets}
        self.assertEqual(
            kinds,
            {
                MEMORY_KIND_PROFILE,
                MEMORY_KIND_EPISODIC,
                MEMORY_KIND_PROCEDURAL,
                MEMORY_KIND_REPAIR,
            },
        )
        repair = next(
            asset for asset in reflection.assets if asset.kind == MEMORY_KIND_REPAIR
        )
        self.assertIn("ModuleNotFoundError", repair.content)
        self.assertIn("python3", repair.content)
        self.assertTrue(reflection.lessons)

    def test_redacts_credentials_before_persistence(self) -> None:
        text = "以后用 api_key=sk-abcdefghijklmnop123456789 访问，Authorization: Bearer abc.def"
        cleaned = redact_sensitive(text)
        self.assertNotIn("sk-abcdefghijklmnop", cleaned)
        self.assertNotIn("abc.def", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_awaiting_user_is_not_grounded_into_long_term_memory(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        result = memory.reflect_turn(
            [{"role": "user", "content": "以后都用中文"}],
            session_id="s1",
            user_text="以后都用中文",
            outcome="awaiting_user",
        )
        self.assertEqual(result.assets, [])
        self.assertEqual(memory.stats()["assets"], 0)

    def test_one_off_task_instruction_is_not_promoted_to_profile(self) -> None:
        reflection = reflect_turn(
            [{"role": "user", "id": "u1", "content": "不要删除这个文件"}],
            cwd=str(self.cwd),
            session_id="s1",
            user_text="不要删除这个文件",
            outcome="final",
        )
        self.assertFalse(
            any(asset.kind == MEMORY_KIND_PROFILE for asset in reflection.assets)
        )


class StoreAndIndexTests(EvolutionTestCase):
    def test_assets_survive_new_engine_and_index_is_persisted(self) -> None:
        first = EvolvedMemory(str(self.cwd), home=self.home)
        first.reflect_turn(
            _turn("修复 pytest 缺失的问题"),
            session_id="s1",
            user_text="修复 pytest 缺失的问题",
            outcome="final",
        )

        index_path = first.paths.project_assets.parent / "index.json"
        self.assertTrue(index_path.is_file())
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertTrue(raw["postings"])

        second = EvolvedMemory(str(self.cwd), home=self.home)
        hits = second.search("pytest ModuleNotFoundError 修复")
        self.assertTrue(hits)
        self.assertTrue(
            any(hit.asset.kind == MEMORY_KIND_REPAIR for hit in hits),
            [hit.asset.title for hit in hits],
        )

    def test_same_procedure_merges_instead_of_duplicating(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        for index in range(2):
            memory.reflect_turn(
                _turn(f"第 {index + 1} 次修复测试问题"),
                session_id=f"s{index + 1}",
                user_text=f"第 {index + 1} 次修复测试问题",
                outcome="final",
            )
        procedures = memory.project.list_assets(kind=MEMORY_KIND_PROCEDURAL)
        self.assertEqual(len(procedures), 1)
        self.assertEqual(procedures[0].occurrences, 2)

    def test_chinese_bigram_tokenization_matches_error_language(self) -> None:
        tokens = tokenize("错误修复与跨会话记忆")
        self.assertIn("错误", tokens)
        self.assertIn("修复", tokens)
        self.assertIn("记忆", tokens)


class ReuseTests(EvolutionTestCase):
    def test_repair_memory_is_recalled_when_same_error_reappears(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        memory.reflect_turn(
            _turn("修复测试环境"),
            session_id="s1",
            user_text="修复测试环境",
            outcome="final",
        )

        fresh = EvolvedMemory(str(self.cwd), home=self.home)
        context = fresh.build_context(
            [
                {
                    "role": "user",
                    "content": "又出现了同样的报错，继续修",
                },
                _call("new1", "run_command", {"command": "pytest"}),
                _result(
                    "new1",
                    "run_command",
                    "ModuleNotFoundError: No module named 'pytest'",
                    error=True,
                ),
            ]
        )
        self.assertIn("<evolved_memory>", context)
        self.assertIn("[repair", context)
        self.assertIn("python3", context)

    def test_profile_crosses_projects_while_project_episode_does_not(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        memory.reflect_turn(
            _turn("以后始终用中文回答我"),
            session_id="s1",
            user_text="以后始终用中文回答我",
            outcome="final",
        )

        other_cwd = self.root / "other"
        other_cwd.mkdir()
        other = EvolvedMemory(str(other_cwd), home=self.home)
        context = other.build_context(
            [{"role": "user", "content": "继续实现功能"}]
        )
        self.assertIn("中文回答", context)
        self.assertNotIn("修复测试环境", context)

    def test_repeated_procedure_grows_managed_skill(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        for index in range(2):
            run = memory.reflect_turn(
                _turn(f"完成第 {index + 1} 次验证"),
                session_id=f"s{index + 1}",
                user_text=f"完成第 {index + 1} 次验证",
                outcome="final",
            )

        self.assertTrue(run.skills, "第二次出现相同流程后应生长出 Skill")
        skill_path = Path(run.skills[0]["path"])
        self.assertTrue(skill_path.is_file())
        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("managed-by: minicode-self-evolution", content)
        self.assertIn("Proven steps", content)
        skills = memory.project.list_assets(kind=MEMORY_KIND_SKILL)
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].occurrences, 2)


class _CaptureModel:
    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def next(self, messages, **_kwargs):
        self.seen.append([dict(message) for message in messages])
        return AgentStep(type="assistant", content="已完成。", kind="final")


class _WorkflowModel:
    def __init__(self) -> None:
        self.steps = [
            AgentStep(
                type="tool_calls",
                calls=[ToolCall(id="c1", toolName="read_file", input={"path": "note.txt"})],
            ),
            AgentStep(
                type="tool_calls",
                calls=[
                    ToolCall(
                        id="c2",
                        toolName="run_command",
                        input={"command": "echo", "args": ["ok"]},
                    )
                ],
            ),
            AgentStep(type="assistant", content="流程完成。", kind="final"),
        ]

    def next(self, messages, **_kwargs):
        return self.steps.pop(0)


class _ErrorThenCaptureModel:
    def __init__(self) -> None:
        self.seen: list[list[dict]] = []
        self.stage = 0

    def next(self, messages, **_kwargs):
        self.seen.append([dict(message) for message in messages])
        self.stage += 1
        if self.stage == 1:
            return AgentStep(
                type="tool_calls",
                calls=[
                    ToolCall(
                        id="missing",
                        toolName="read_file",
                        input={"path": "missing.txt"},
                    )
                ],
            )
        return AgentStep(type="assistant", content="已看到修复经验。", kind="final")


class HostIntegrationTests(EvolutionTestCase):
    def _host(self, model) -> SessionHost:
        plan = PlanManager("s1")
        return SessionHost(
            cwd=str(self.cwd),
            model=model,
            runtime_config=None,
            permissions=PermissionManager(str(self.cwd), store_path=None),
            tools=create_default_registry(str(self.cwd), plan),
            plan=plan,
            session_id="s1",
        )

    def test_agent_loop_receives_memory_context_callback(self) -> None:
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
                {"role": "user", "content": "hello"},
            ],
            cwd=str(self.cwd),
            memory_context=lambda messages: [
                {
                    **messages[0],
                    "content": messages[0]["content"] + "\n<evolved_memory>hit</evolved_memory>",
                },
                *messages[1:],
            ],
        )
        self.assertIn("<evolved_memory>", seen[0])

    def test_turn_is_reflected_and_new_session_reuses_it(self) -> None:
        (self.cwd / "note.txt").write_text("hello", encoding="utf-8")
        first = self._host(_WorkflowModel())
        result = first.execute(
            TurnRequest(
                input={"role": "user", "content": "以后始终用中文回答；验证这个项目。"}
            )
        )
        self.assertEqual(result.outcome, "final")
        self.assertIsNone(first.last_reflection_error)
        self.assertGreater(first.evolved_memory.stats()["assets"], 0)

        capture = _CaptureModel()
        second = self._host(capture)
        second.session_id = "s2"
        second.execute(
            TurnRequest(input={"role": "user", "content": "继续处理同类任务"})
        )
        self.assertTrue(capture.seen)
        system_prompt = capture.seen[0][0]["content"]
        self.assertIn("<evolved_memory>", system_prompt)
        self.assertIn("中文回答", system_prompt)

    def test_tool_error_triggers_repair_memory_inside_same_turn(self) -> None:
        memory = EvolvedMemory(str(self.cwd), home=self.home)
        memory.reflect_turn(
            [
                {"role": "user", "id": "u0", "content": "修复缺失文件"},
                _call("old1", "read_file", {"path": "missing.txt"}),
                _result("old1", "read_file", "File not found: missing.txt", error=True),
                _call("old2", "list_files", {"path": "."}),
                _result("old2", "list_files", "note.txt"),
                {"role": "assistant", "id": "a0", "content": "已定位。"},
            ],
            session_id="old-session",
            user_text="修复缺失文件",
            outcome="final",
        )

        model = _ErrorThenCaptureModel()
        host = self._host(model)
        result = host.execute(
            TurnRequest(input={"role": "user", "content": "又缺文件了，继续修。"})
        )
        self.assertEqual(result.outcome, "final")
        self.assertGreaterEqual(len(model.seen), 2)
        second_step_prompt = model.seen[1][0]["content"]
        self.assertIn("<evolved_memory>", second_step_prompt)
        self.assertIn("[repair", second_step_prompt)

    def test_skill_growth_refreshes_live_tool_catalog(self) -> None:
        (self.cwd / "note.txt").write_text("hello", encoding="utf-8")
        first = self._host(_WorkflowModel())
        first.execute(TurnRequest(input={"role": "user", "content": "验证项目流程"}))

        second = self._host(_WorkflowModel())
        second.session_id = "s2"
        second.execute(TurnRequest(input={"role": "user", "content": "再次验证项目流程"}))
        names = {item["name"] for item in second.tools.skills()}
        self.assertTrue(
            any(name.startswith("evolved-") for name in names),
            names,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
