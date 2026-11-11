"""本地工具快捷方式 —— 直接输入 `/ls`、`/edit` 等以调用某个工具。

## 它解决什么

模型调工具要走一整轮对话（发请求 → 等回答 → 执行 → 再发请求）。但有些操作
用户自己就想立刻做：看一眼目录、读一个文件、改一行。这些不该绕一圈模型。

快捷方式是**用户侧**的入口：把一行输入直接翻译成「工具名 + 输入」，由本地
直接执行，结果当作一次工具调用来显示。

    运行时命令 → 本地命令 → **快捷方式** → 未知命令提示 → 交给模型

## 一处有意修正（D35）

`/write` 与 `/modify` 没有这个问题（它们用 `indexOf('::')` + `slice`）。

"替换内容里含 `::`" 是很常见的（Python 切片 `a[1::2]`、URL、命名空间），
静默截断属于「悄悄改坏数据」，因此本实现对 `/edit` 与 `/grep` 改用
**最后一个分隔符归后者**的切法，见 `_split_limited`。理由与影响记在
`docs/divergences.md` D35。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SHORTCUT_USAGES",
    "LocalToolShortcut",
    "parse_local_tool_shortcut",
]

@dataclass(frozen=True, slots=True)
class LocalToolShortcut:
    """一行输入翻译出的「工具名 + 输入」。"""

    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)

#: 帮助里展示的用法与说明。
#:
#: 描述里写明了分隔符格式，改了就等于改了别人的输入习惯。
SHORTCUT_USAGES: tuple[tuple[str, str], ...] = (
    ("/ls [path]", "List files in a directory."),
    ("/grep <pattern>::[path]", "Search text in files."),
    ("/read <path>", "Read a file directly."),
    ("/write <path>::<content>", "Write a file directly."),
    (
        "/modify <path>::<content>",
        "Replace a file, showing a reviewable diff before applying it.",
    ),
    ("/edit <path>::<search>::<replace>", "Edit a file by exact replacement."),
    (
        "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...",
        "Apply multiple replacements to one file in one command.",
    ),
    (
        "/cmd [cwd::]<command> [args...]",
        "Run an allowed development command directly, optionally in another directory.",
    ),
)

def _split_limited(payload: str, count: int) -> list[str]:
    """把 `payload` 按 `::` 切成**至多 `count` 段**，最后一段收下剩余全部。

    """
    parts = payload.split("::", count - 1)
    return parts

def parse_local_tool_shortcut(text: str) -> LocalToolShortcut | None:
    """把一行输入解析成工具调用。**不是快捷方式时返回 `None`。**

    返回 `None` 有两种含义，调用方不需要区分：
      - 这行输入根本不是快捷方式（例如普通提问）；
      - 它以快捷方式开头，但格式不对（缺分隔符、缺必填片段）。

    """
    if text == "/ls" or text.startswith("/ls "):
        directory = text[len("/ls") :].strip()
        return LocalToolShortcut(
            "list_files", {"path": directory} if directory else {}
        )

    if text.startswith("/grep "):
        payload = text[len("/grep ") :].strip()
        # 至多两段：pattern 与 path。pattern 里的 `::` 会留在 path 一侧，
        parts = _split_limited(payload, 2)
        pattern = parts[0].strip() if parts else ""
        if not pattern:
            return None
        search_path = parts[1].strip() if len(parts) > 1 else ""
        result: dict[str, Any] = {"pattern": pattern}
        if search_path:
            result["path"] = search_path
        return LocalToolShortcut("grep_files", result)

    if text.startswith("/read "):
        path = text[len("/read ") :].strip()
        if not path:
            return None
        return LocalToolShortcut("read_file", {"path": path})

    for command, tool_name in (("/write ", "write_file"), ("/modify ", "modify_file")):
        if text.startswith(command):
            payload = text[len(command) :]
            # 用 `indexOf('::')` 切一刀：内容里的后续 `::` 全部保留。
            split_at = payload.find("::")
            if split_at == -1:
                return None
            target = payload[:split_at].strip()
            if not target:
                return None
            return LocalToolShortcut(
                tool_name, {"path": target, "content": payload[split_at + 2 :]}
            )

    if text.startswith("/edit "):
        payload = text[len("/edit ") :]
        # 至多三段；第三段（replace）收下剩余全部，避免内容里的 `::` 被丢掉。
        parts = _split_limited(payload, 3)
        target = parts[0].strip() if parts else ""
        if not target or len(parts) < 3:
            return None
        return LocalToolShortcut(
            "edit_file",
            {"path": target, "search": parts[1], "replace": parts[2]},
        )

    if text.startswith("/cmd "):
        payload = text[len("/cmd ") :].strip()
        split_at = payload.find("::")
        if split_at == -1:
            command_text = payload
            command_cwd = ""
        else:
            # 格式是 `[cwd::]<command> [args...]`：`::` **之前**是 cwd。
            command_cwd = payload[:split_at].strip()
            command_text = payload[split_at + 2 :].strip()

        # `-m "a b"` 会得到 `['-m', '"a', 'b"']`。保持一致，因为
        # `/cmd` 只是便捷入口，复杂引号应当直接用 `run_command` 工具。
        pieces = command_text.split()
        if not pieces:
            return None
        command, args = pieces[0], pieces[1:]
        result = {"command": command, "args": args}
        if command_cwd:
            result["cwd"] = command_cwd
        return LocalToolShortcut("run_command", result)

    if text.startswith("/patch "):
        payload = text[len("/patch ") :]
        parts = payload.split("::")
        target = parts[0].strip() if parts else ""
        ops = parts[1:]
        # 至少一对，且必须是偶数对——奇数说明漏写了一段。
        if not target or len(ops) < 2 or len(ops) % 2 != 0:
            return None
        replacements = [
            {"search": ops[index], "replace": ops[index + 1]}
            for index in range(0, len(ops), 2)
        ]
        return LocalToolShortcut(
            "patch_file", {"path": target, "replacements": replacements}
        )

    return None
