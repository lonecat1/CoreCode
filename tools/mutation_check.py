"""Mutation check: do the runtime tests actually protect the four gates?

A green suite only tells us the tests pass. This script deliberately breaks each
guarantee and asserts that the suite goes red. If a mutation survives, the test
covering it is decorative.

Every mutation is applied in memory to a copy of the file, then restored from git.
"""
import os
import re
import subprocess
import sys

REPO = r"D:\CoreCode\minicode-py"
PY = sys.executable
SESSION = os.path.join(REPO, "src", "minicode", "runtime", "session.py")
GOAL_CTRL = os.path.join(REPO, "src", "minicode", "runtime", "goal_controller.py")

# (label, file, old, new) — each should make at least one test fail.
MUTATIONS = [
    (
        "闸门1: submit 不再拒绝 Goal 活跃",
        SESSION,
        "        if self.goal.enabled:\n"
        "            raise RuntimeError_(\"Goal 正在运行。用 /goal pause 后再开始新回合。\")",
        "        if False:\n"
        "            raise RuntimeError_(\"Goal 正在运行。用 /goal pause 后再开始新回合。\")",
    ),
    (
        "闸门2: 创建 Loop 前不检查 Goal",
        SESSION,
        "            if self.goal.enabled:\n"
        "                raise RuntimeError_(\n"
        "                    \"请先暂停或清除 Goal，再启动 Loop；并先回答或清除待处理的问题。\"\n"
        "                )",
        "            if False:\n"
        "                raise RuntimeError_(\n"
        "                    \"请先暂停或清除 Goal，再启动 Loop；并先回答或清除待处理的问题。\"\n"
        "                )",
    ),
    (
        "闸门3: assert_idle 不再检查 Loop",
        SESSION,
        "        if self.loop.enabled:\n"
        "            raise RuntimeError_(\"请先停止 Loop，再启动或恢复 Goal。\")",
        "        if False:\n"
        "            raise RuntimeError_(\"请先停止 Loop，再启动或恢复 Goal。\")",
    ),
    (
        "闸门4: Loop 的 busy 不再包含 Goal",
        SESSION,
        "            or self.goal.enabled\n",
        "            or False\n",
    ),
    (
        "Goal: 不再要求 completed 与标准数匹配",
        os.path.join(REPO, "src", "minicode", "runtime", "goal.py"),
        "        if len(checks) != len(goal.completion_criteria):",
        "        if False:",
    ),
    (
        "Goal: 允许模型改目标（放开 action 白名单）",
        os.path.join(REPO, "src", "minicode", "runtime", "goal.py"),
        'VALID_ACTIONS = frozenset({ACTION_SET_CRITERIA, ACTION_BLOCKED, ACTION_COMPLETED})',
        'VALID_ACTIONS = frozenset({ACTION_SET_CRITERIA, ACTION_BLOCKED, ACTION_COMPLETED, "change_objective"})',
    ),
    (
        "Plan: 不再限制最多一个 in_progress",
        os.path.join(REPO, "src", "minicode", "runtime", "plan.py"),
        "        if in_progress > 1:",
        "        if False:",
    ),
    (
        "Plan: 接受未知 id 当作新增",
        os.path.join(REPO, "src", "minicode", "runtime", "plan.py"),
        "                if todo_id not in existing_ids:\n"
        "                    raise PlanError(\n"
        "                        f\"未知的 Todo id：{todo_id!r}。新增条目请省略 id。\"\n"
        "                    )",
        "                if False:\n"
        "                    raise PlanError(\n"
        "                        f\"未知的 Todo id：{todo_id!r}。新增条目请省略 id。\"\n"
        "                    )",
    ),
    (
        "Loop: 移除自动回合预算（无限续跑）",
        GOAL_CTRL,
        "                    automatic_turns += 1\n"
        "                    if automatic_turns >= MAX_AUTOMATIC_TURNS:",
        "                    automatic_turns += 1\n"
        "                    if False:",
    ),
    (
        "TurnRunner: 去掉单飞拒绝",
        os.path.join(REPO, "src", "minicode", "runtime", "turn.py"),
        "        if self._controller is not None:\n"
        "            raise TurnBusyError(\"A root turn is still running.\")",
        "        if False:\n"
        "            raise TurnBusyError(\"A root turn is still running.\")",
    ),
]


def run_suite() -> bool:
    """Returns True when the suite passes."""
    proc = subprocess.run(
        [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode == 0


def main() -> int:
    baseline = run_suite()
    print(f"baseline: {'PASS' if baseline else 'FAIL'}")
    if not baseline:
        print("基线不通过，无法做变异检查。")
        return 1

    survived: list[str] = []
    for label, path, old, new in MUTATIONS:
        original = open(path, encoding="utf-8").read()
        if old not in original:
            print(f"  SKIP  {label}  (未找到待变异的代码片段)")
            survived.append(label + " [片段不匹配]")
            continue
        try:
            open(path, "w", encoding="utf-8", newline="\n").write(
                original.replace(old, new, 1)
            )
            killed = not run_suite()
        finally:
            open(path, "w", encoding="utf-8", newline="\n").write(original)

        print(f"  {'KILLED' if killed else 'SURVIVED'}  {label}")
        if not killed:
            survived.append(label)

    print()
    print(f"变异检查：{len(MUTATIONS) - len(survived)}/{len(MUTATIONS)} 被测试杀死")
    if survived:
        print("存活（说明对应测试形同虚设）：")
        for item in survived:
            print("  -", item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
