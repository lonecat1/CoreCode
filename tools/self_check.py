"""Environment self-check: does this checkout actually work?

Deliberately a **standalone script**, not a CLI flag.

An earlier version exposed this as `minicode --agent`, which ran the test suite —
and the test suite contained a test that called `--agent`. That is infinite
recursion, and it hung the suite instead of failing it. Anything that runs the
tests must live outside the tests.

Usage:  python tools/self_check.py
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")


def _run(label: str, command: list[str], *, env: dict[str, str] | None = None) -> bool:
    print(f"\n=== {label} ===")
    result = subprocess.run(command, cwd=REPO, env=env)
    ok = result.returncode == 0
    print(f"--- {label}: {'通过' if ok else '失败'} ---")
    return ok


def main() -> int:
    print(f"仓库根目录：{REPO}")
    print(f"Python    ：{sys.version.split()[0]}")

    # 依赖检查：本项目承诺零第三方运行时依赖。
    print("\n=== 依赖检查 ===")
    missing: list[str] = []
    for module in ("zod", "requests", "pydantic"):
        found = _module_exists(module)
        if found:
            missing.append(module)
    if missing:
        print(f"警告：检测到非预期依赖 {missing}（本项目应为零运行时依赖）")
    else:
        print("✓ 未发现第三方运行时依赖")

    checks: list[tuple[str, list[str]]] = [
        ("导入检查", [sys.executable, os.path.join(REPO, "tools", "check_imports.py")]),
        ("测试", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
    ]

    results = [_run(label, command) for label, command in checks]

    print()
    passed = sum(1 for item in results if item)
    print(f"最终结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


def _module_exists(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


if __name__ == "__main__":
    raise SystemExit(main())
