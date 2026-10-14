"""三个编辑工具 —— `edit_file` / `patch_file` / `modify_file`。

对应原版 `MiniCode/src/tools/` 下的 `edit-file.ts`(45) · `patch-file.ts`(76) ·
`modify-file.ts`(30)。

## 为什么放在一个模块里

三者共享同一条落盘路径（`file_review.apply_reviewed_file_change`）与同一套
替换语义，只是入口形状不同：

    edit_file     一次替换（search → replace）
    patch_file    一批替换（有序应用，任一失败就整体放弃）
    modify_file   整文件改写（**与原版一样，与 write_file 实现完全相同**）

## 关于替换语义：一处**有意不照抄**的地方

原版用 JS 的 `String.replace`。**JS 会在替换串里解释 `$&`、`$1`、`` $` ``、
`$'`、`$$`**：`"abc".replace("b", "$&x")` 得到 `"abxc"` 而不是 `"a$&xc"`。

也就是说，凡是替换内容里含 `$` 的（Python f-string、shell 变量、正则、JS
模板串）都会被原版**悄悄改掉**。这与工具自己的描述矛盾——它写的是
"replacing **exact** text"。

因此本实现**不做 `$` 展开**，`str.replace` 就是字面替换。这是 D33，理由与
影响记在 `docs/divergences.md`。

## 另一个与原版一致的行为

`edit_file` 与 `patch_file` 会**直接读文件**（原版用 `readFile`），因此对
不存在的文件是**失败**；而 `modify_file` 走 `applyReviewedFileChange`，它对
不存在的文件**视为空内容**，于是「写一个新文件」是成功的。这个不对称是原版
就有的，本实现保留——它其实有用：`modify_file` 可以创建文件。
"""

from __future__ import annotations

from typing import Any

from ..file_review import apply_reviewed_file_change
from ..tooling import ToolContext, ToolDefinition, ToolResult
from ..workspace import resolve_tool_path

__all__ = ["edit_file_tool", "modify_file_tool", "patch_file_tool"]


def _read_text_file(target: str, display_path: str) -> tuple[str | None, ToolResult | None]:
    """读文件。读不到时返回一个「模型能看懂」的失败结果。

    原版直接 `readFile` 抛异常，最终文本是一句 Node 的 `ENOENT: no such file
    or directory, open '...'`。这里给出同样语义但更清楚的提示——**行为等价**
    （都是工具失败），只是可读性更好。
    """
    try:
        # `newline=""` 保证换行不被归一化——否则「读 CRLF、写 LF」会让
        # 每次编辑都翻转整个文件的换行风格。
        with open(target, encoding="utf-8", newline="") as handle:
            return handle.read(), None
    except FileNotFoundError:
        return None, ToolResult.failure(f"文件不存在：{display_path}")
    except IsADirectoryError:
        return None, ToolResult.failure(f"这是一个目录，不是文件：{display_path}")
    except UnicodeDecodeError:
        return None, ToolResult.failure(f"无法按 UTF-8 解码：{display_path}")
    except OSError as error:
        return None, ToolResult.failure(f"{type(error).__name__}: {error}")


def apply_replacement(
    content: str, search: str, replace: str, replace_all: bool
) -> str:
    """按原版语义做一次替换。

    **`replace_all=False` 时只替换第一处** —— 这是 JS `String.replace` 对
    字符串模式的行为，也是原版的行为。很多模型会误以为它替换全部，因此这个
    区别值得用一个具名函数+注释显式表达，而不是埋在调用点里。

    替换内容是**字面量**，不做 `$&` 之展开（D33）。
    """
    if replace_all:
        return content.replace(search, replace)
    return content.replace(search, replace, 1)


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _run_edit_file(data: Any, context: ToolContext) -> ToolResult:
    path = str((data or {}).get("path", ""))
    search = str((data or {}).get("search", ""))
    replace = str((data or {}).get("replace", ""))
    replace_all = bool((data or {}).get("replaceAll", False))

    target = resolve_tool_path(context, path, "write")
    original, failure = _read_text_file(target, path)
    if failure is not None:
        return failure
    assert original is not None

    if search not in original:
        return ToolResult.failure(f"Text not found in {path}")

    next_content = apply_replacement(original, search, replace, replace_all)
    return apply_reviewed_file_change(context, path, target, next_content)


edit_file_tool = ToolDefinition(
    name="edit_file",
    description="Edit a text file by replacing exact text.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "search": {"type": "string"},
            "replace": {"type": "string"},
            "replaceAll": {"type": "boolean"},
        },
        "required": ["path", "search", "replace"],
    },
    validator=lambda data: {
        "path": _require_text(data, "path"),
        "search": _require_text(data, "search"),
        "replace": _optional_text(data, "replace"),
        "replaceAll": bool((data or {}).get("replaceAll", False)),
    },
    run=_run_edit_file,
)


# ---------------------------------------------------------------------------
# patch_file
# ---------------------------------------------------------------------------


def _run_patch_file(data: Any, context: ToolContext) -> ToolResult:
    path = str((data or {}).get("path", ""))
    replacements = list((data or {}).get("replacements") or [])

    target = resolve_tool_path(context, path, "write")
    content, failure = _read_text_file(target, path)
    if failure is not None:
        return failure
    assert content is not None

    applied: list[str] = []
    for index, replacement in enumerate(replacements, start=1):
        search = str(replacement.get("search", ""))
        replace = str(replacement.get("replace", ""))
        replace_all = bool(replacement.get("replaceAll", False))

        if search not in content:
            # **任一替换找不到就整体放弃**，不写盘。部分应用会留下一个
            # 半成品文件，比完全不改更难排查。
            return ToolResult.failure(f"Replacement {index} not found in {path}")

        content = apply_replacement(content, search, replace, replace_all)
        applied.append(f"#{index} {'replaceAll' if replace_all else 'replaceOnce'}")

    result = apply_reviewed_file_change(context, path, target, content)
    if not result.ok:
        return result

    return ToolResult.success(
        f"Patched {path} with {len(applied)} replacement(s): {', '.join(applied)}"
    )


def _validate_patch_file(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("输入必须是对象")
    raw = data.get("replacements")
    if not isinstance(raw, list) or not raw:
        raise ValueError("字段 replacements 必须是非空数组")

    replacements: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"replacements[{index}] 必须是对象")
        search = item.get("search")
        if not isinstance(search, str) or not search:
            raise ValueError(f"replacements[{index}].search 必须是非空字符串")
        replace = item.get("replace")
        if not isinstance(replace, str):
            raise ValueError(f"replacements[{index}].replace 必须是字符串")
        entry: dict[str, Any] = {"search": search, "replace": replace}
        if "replaceAll" in item:
            entry["replaceAll"] = bool(item.get("replaceAll"))
        replacements.append(entry)

    return {"path": _require_text(data, "path"), "replacements": replacements}


patch_file_tool = ToolDefinition(
    name="patch_file",
    description="Apply multiple exact-text replacements to one file in a single operation.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "replacements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string"},
                        "replace": {"type": "string"},
                        "replaceAll": {"type": "boolean"},
                    },
                    "required": ["search", "replace"],
                },
            },
        },
        "required": ["path", "replacements"],
    },
    validator=_validate_patch_file,
    run=_run_patch_file,
)


# ---------------------------------------------------------------------------
# modify_file
# ---------------------------------------------------------------------------


def _run_modify_file(data: Any, context: ToolContext) -> ToolResult:
    path = str((data or {}).get("path", ""))
    content = str((data or {}).get("content", ""))
    target = resolve_tool_path(context, path, "write")
    # 与 `write_file` 完全同体：走审查路径，因此**可以创建新文件**
    # （不存在的文件被视为空内容）。原版就是这样，两个工具只是措辞不同。
    return apply_reviewed_file_change(context, path, target, content)


modify_file_tool = ToolDefinition(
    name="modify_file",
    description=(
        "Replace a file with reviewed content so the user can approve the diff first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    validator=lambda data: {
        "path": _require_text(data, "path"),
        "content": _optional_text(data, "content"),
    },
    run=_run_modify_file,
)


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------


def _require_text(data: Any, key: str) -> str:
    """取一个**非空**字符串字段。对应原版 schema 里的 `.min(1)`。"""
    value = (data or {}).get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"字段 {key} 必须是非空字符串")
    return value


def _optional_text(data: Any, key: str) -> str:
    """取一个可以为空、但必须是字符串的字段。对应原版的 `.string()`。"""
    value = (data or {}).get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"字段 {key} 必须是字符串")
    return value
