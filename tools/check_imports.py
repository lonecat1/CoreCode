"""Zero-dependency import checker.

Two of the bugs in this project were *wrong relative imports* that Python only
surfaces when a specific import order is exercised:

    from .goal import GoalError        # inside tools/goal.py -> should be ..runtime.goal
    from .manager import SubAgentError # inside tools/subagents.py -> should be ..agents.manager

Both are `ModuleNotFoundError` at runtime but perfectly silent until something
imports the module first. A type checker would catch them; we have no network to
install one, so this script does the one check that matters:

  1. every `from X import a, b` resolves to a module that actually exists;
  2. every imported name is actually defined (or re-exported) in that module.

It also reports modules that are never imported, which is usually a sign of a
filesystem path constant that drifted (see config.MINICODE_HOME / MINICODE_DIR).

Usage:  python tools/check_imports.py
Exit 0 when clean, 1 when problems are found.
"""

from __future__ import annotations

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
PACKAGE = "minicode"


def module_files() -> dict[str, tuple[str, bool]]:
    """Map dotted module name -> (file path, is_package)."""
    found: dict[str, tuple[str, bool]] = {}
    for directory, _subdirs, files in os.walk(SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(directory, name)
            relative = os.path.relpath(full, SRC)
            parts = relative[: -len(".py")].split(os.sep)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts = parts[:-1]
            if not parts:
                continue
            found[".".join(parts)] = (full, is_package)
    return found


def defined_names(path: str) -> set[str]:
    """Top-level names a module defines or imports (so re-exports count)."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def resolve(module: str, node: ast.ImportFrom, is_package: bool) -> str | None:
    """Resolve a relative ImportFrom to an absolute dotted module name.

    The subtlety that makes this easy to get wrong: inside a package's
    `__init__.py`, the module's own dotted name *is* the package, so
    `from .x import y` means `package.x`, not "go up one and look for x".
    Inside a regular module `a.b.c`, `from .x import y` means `a.b.x`.

    Concretely: `minicode/adapters/__init__.py` with `from .anthropic import X`
    resolves to `minicode.adapters.anthropic` — the first version of this
    checker wrongly produced `minicode.anthropic`.
    """
    if node.level == 0:
        return node.module

    parts = module.split(".") if module else []
    if not is_package:
        # A module's own name includes its filename; the containing package is
        # the part we climb from.
        parts = parts[:-1] if parts else []

    # `level == 1` means the containing package itself.
    climb = node.level - 1
    if climb > len(parts):
        return None
    base = parts[: len(parts) - climb] if climb else parts

    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def main() -> int:
    modules = module_files()
    problems: list[str] = []
    imported: set[str] = set()

    for module, (path, is_package) in sorted(modules.items()):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name
                    if target.startswith(PACKAGE):
                        imported.add(target)
                        if target not in modules:
                            problems.append(
                                f"{module}: import {target} -> 模块不存在"
                            )
                continue

            if not isinstance(node, ast.ImportFrom):
                continue

            target = resolve(module, node, is_package)
            if target is None:
                # `from ..x import y` inside a top-level module escapes the package.
                # The first version of this checker skipped these silently — which is
                # exactly how host.py shipped with 18 wrong-depth imports.
                problems.append(
                    f"{module}:{node.lineno}: 相对导入超出包边界"
                    f"（level={node.level}, module={node.module!r}）"
                )
                continue
            if not target.startswith(PACKAGE):
                continue
            imported.add(target)

            if target not in modules:
                problems.append(
                    f"{module}:{node.lineno}: from {target} import … -> 模块不存在"
                )
                continue

            available = defined_names(modules[target][0])
            # Sub-module imports (`from package import submodule`) are legal even
            # when the submodule is not a name inside __init__.
            submodules = {
                name.rsplit(".", 1)[-1]
                for name in modules
                if name.startswith(target + ".")
            }
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in available and alias.name not in submodules:
                    problems.append(
                        f"{module}:{node.lineno}: from {target} import {alias.name} "
                        f"-> 该名字不存在"
                    )

    # Report modules nothing imports: usually a path/logic drift, worth a look.
    orphans = sorted(
        name
        for name in modules
        if name != PACKAGE
        and not name.startswith(PACKAGE + ".__")
        and name not in imported
        and not any(other.startswith(name + ".") for other in imported)
    )

    print(f"检查 {len(modules)} 个模块 …")
    if problems:
        print(f"\n发现 {len(problems)} 个导入问题：")
        for item in problems:
            print("  ✗", item)
    else:
        print("✓ 全部导入均可解析")

    if orphans:
        print(f"\n未被任何模块导入（{len(orphans)} 个，仅供留意）：")
        for name in orphans:
            print("  ·", name)

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
