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

from _shared import force_utf8_output

#: 从脚本自身位置推导，而不是写死路径——否则仓库换目录（或换机器）就失效，
#: 而且失败方式很隐蔽（会去改一个不存在的路径）。
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SESSION = os.path.join(REPO, "src", "minicode", "runtime", "session.py")
GOAL_CTRL = os.path.join(REPO, "src", "minicode", "runtime", "goal_controller.py")
AGENTS_MANAGER = os.path.join(REPO, "src", "minicode", "agents", "manager.py")
AGENTS_TYPES = os.path.join(REPO, "src", "minicode", "agents", "types.py")
SKILLS = os.path.join(REPO, "src", "minicode", "skills.py")
TOOLS_INIT = os.path.join(REPO, "src", "minicode", "tools", "__init__.py")
MCP_STDIO = os.path.join(REPO, "src", "minicode", "mcp", "stdio_client.py")
MCP_JSONRPC = os.path.join(REPO, "src", "minicode", "mcp", "jsonrpc.py")
TUI_WIDTH = os.path.join(REPO, "src", "minicode", "tui", "width.py")
TUI_SCREEN = os.path.join(REPO, "src", "minicode", "tui", "screen.py")
TUI_APP = os.path.join(REPO, "src", "minicode", "tui", "app.py")
TUI_THEME = os.path.join(REPO, "src", "minicode", "tui", "theme.py")
TUI_TERMINAL = os.path.join(REPO, "src", "minicode", "tui", "terminal.py")
TUI_RUNNER = os.path.join(REPO, "src", "minicode", "tui", "runner.py")
CONFIG = os.path.join(REPO, "src", "minicode", "config.py")
TOOLS_FILES = os.path.join(REPO, "src", "minicode", "tools", "files.py")
TOOLS_GREP = os.path.join(REPO, "src", "minicode", "tools", "grep_files.py")

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
    (
        "子Agent: 移除并发上限",
        AGENTS_MANAGER,
        "            if self.running_count >= self.max_concurrent:",
        "            if False:",
    ),
    (
        "子Agent: 关闭不再优先（收尾会覆盖 closed）",
        AGENTS_MANAGER,
        "                if record.status != STATUS_CLOSED:",
        "                if True:",
    ),
    (
        "子Agent: 把失败当成功",
        AGENTS_MANAGER,
        "                    if result.outcome in (\"failed\", \"aborted\"):",
        "                    if False:",
    ),
    (
        "子Agent: 白名单放进写工具",
        AGENTS_TYPES,
        'SUBAGENT_TOOL_NAMES: tuple[str, ...] = (\n'
        '    "list_files",\n'
        '    "grep_files",\n'
        '    "read_file",\n'
        '    "load_skill",\n'
        '    "web_fetch",\n'
        '    "web_search",\n'
        ')',
        'SUBAGENT_TOOL_NAMES: tuple[str, ...] = (\n'
        '    "list_files",\n'
        '    "grep_files",\n'
        '    "read_file",\n'
        '    "load_skill",\n'
        '    "web_fetch",\n'
        '    "web_search",\n'
        '    "write_file",\n'
        ')',
    ),
    (
        "Skills: 不再拒绝路径分隔符（放开穿越）",
        SKILLS,
        '    if "/" in normalized or "\\\\" in normalized:',
        "    if False:",
    ),
    (
        "Skills: 不再拒绝 `..` 前缀名",
        SKILLS,
        '    if normalized in (".", "..") or normalized.startswith("."):',
        "    if False:",
    ),
    (
        "Skills: 同名时后到者胜（优先级反转）",
        SKILLS,
        "            if entry.name in found:\n                continue",
        "            if False:\n                continue",
    ),
    (
        "Skills: 注册表不再注册 load_skill",
        TOOLS_INIT,
        "        create_load_skill_tool(cwd or os.getcwd()),",
        "        # create_load_skill_tool(cwd or os.getcwd()),",
    ),
    (
        "MCP: 探测超时后不再用同一分帧重试（慢启动被误判）",
        MCP_STDIO,
        "        except JsonRpcTimeout:\n"
        "            # ② 同一种分帧、正常超时重试：冷启动可能真的很慢。\n"
        "            #    少了这一步会把「慢」误判成「不支持」，然后一直用错的分帧。\n"
        "            rpc.request(\"initialize\", params, timeout=INITIALIZE_TIMEOUT)",
        "        except JsonRpcTimeout:\n"
        "            raise",
    ),
    (
        # 注意：目标必须带**行首四个空格**，否则会先匹配到 `cached ==
        # PROTOCOL_CONTENT_LENGTH` 那一支（八个空格），把它改成与默认分支
        # 完全相同的代码——变异就变成了空操作，检查会误报「测试无效」。
        "MCP: 分帧协商默认顺序反转（先试 newline-json）",
        MCP_STDIO,
        "\n    return [PROTOCOL_CONTENT_LENGTH, PROTOCOL_NEWLINE_JSON]\n",
        "\n    return [PROTOCOL_NEWLINE_JSON, PROTOCOL_CONTENT_LENGTH]\n",
    ),
    (
        "MCP: 缓存被忽略（永远从默认顺序开始）",
        MCP_STDIO,
        "    if cached == PROTOCOL_NEWLINE_JSON:\n"
        "        return [PROTOCOL_NEWLINE_JSON, PROTOCOL_CONTENT_LENGTH]",
        "    if False:\n"
        "        return [PROTOCOL_NEWLINE_JSON, PROTOCOL_CONTENT_LENGTH]",
    ),
    (
        "MCP: Content-Length 按字符数而非字节数解析",
        MCP_JSONRPC,
        "            body = bytes(self._buffer[body_start : body_start + length])",
        "            body = bytes(self._buffer[body_start : body_start + length])[:length // 3]",
    ),
    (
        "MCP: 缺少 Content-Length 的头块不被丢弃（永久卡住）",
        MCP_JSONRPC,
        "                self._buffer = bytearray(self._buffer[body_start:])\n"
        "                continue",
        "                return messages",
    ),
    (
        "TUI: 未变化的行也重绘（差分失效，会闪）",
        TUI_SCREEN,
        "                if old == line:\n"
        "                    # 没变：只移动，不重写。这是「不闪」的关键。\n"
        "                    continue",
        "                if False:\n                    continue",
    ),
    (
        "TUI: 内容没变也照发定位序列（无变化不再零输出）",
        TUI_SCREEN,
        "        if self.lines == new_lines and self.cursor_row == target:\n"
        "            return \"\"",
        "        if False:\n            return \"\"",
    ),
    (
        "TUI: 区域变短时不擦除残影",
        TUI_SCREEN,
        "            if len(new_lines) < len(previous):\n"
        "                output.append(CARRIAGE_RETURN + ERASE_TO_END)",
        "            if False:\n"
        "                output.append(CARRIAGE_RETURN + ERASE_TO_END)",
    ),
    (
        "TUI: 重绘前不回到区域首行（会画到错位置）",
        TUI_SCREEN,
        "            output.append(cursor_up(len(self.lines) - 1))",
        "            output.append(\"\")",
    ),
    (
        "TUI: 超宽行不再截断（终端自动折行，行数记账失效）",
        TUI_APP,
        "    return [\n"
        "        line if string_display_width(line) <= limit else truncate_plain(line, limit)\n"
        "        for line in lines\n"
        "    ]",
        "    return list(lines)",
    ),
    (
        "TUI: 区域高度不再受终端行数约束（会顶出屏幕）",
        TUI_APP,
        "    return max(MIN_REGION_LINES, min(MAX_REGION_LINES, max(1, rows - 1)))",
        "    return MAX_REGION_LINES",
    ),
    (
        "TUI: 宽字符不再算作 2 列",
        TUI_WIDTH,
        "    for start, end in _WIDE_RANGES:\n"
        "        if start <= code <= end:\n"
        "            return 2",
        "    for start, end in _WIDE_RANGES:\n"
        "        if False:\n"
        "            return 2",
    ),
    (
        "TUI: 组合附加符号不再算作 0 宽",
        TUI_WIDTH,
        "    for start, end in _COMBINING_RANGES:\n"
        "        if start <= code <= end:\n"
        "            return 0",
        "    for start, end in _COMBINING_RANGES:\n"
        "        if False:\n"
        "            return 0",
    ),
    (
        "TUI: paint 参数顺序写反也不再报错（静默画错）",
        TUI_THEME,
        "    if codes and text.startswith(\"\\x1b[\") and not any(c.startswith(\"\\x1b[\") for c in codes):",
        "    if False:",
    ),
    (
        "TUI: NO_COLOR 被无视",
        TUI_THEME,
        '    return not os.environ.get("NO_COLOR")',
        "    return True",
    ),
    (
        "TUI: 不支持 ANSI 时仍打印转义序列（界面会糊掉）",
        TUI_TERMINAL,
        "        if not self.ansi:\n"
        "            # 降级：整块重印，不做原地重绘。会累积，但至少可读、不错乱。\n"
        "            self.write(\"\\n\".join(lines) + \"\\n\")\n"
        "            return",
        "        if False:\n"
        "            self.write(\"\\n\".join(lines) + \"\\n\")\n"
        "            return",
    ),
    (
        "TUI: 退出时不恢复光标（终端会失去光标）",
        TUI_TERMINAL,
        "        if self._cursor_hidden:\n"
        "            self.write(SHOW_CURSOR)\n"
        "            self._cursor_hidden = False",
        "        if False:\n"
        "            self.write(SHOW_CURSOR)\n"
        "            self._cursor_hidden = False",
    ),
    (
        "配置: 环境变量重新压过 settings.json",
        CONFIG,
        "    return str(from_settings or source.get(key) or \"\").strip()",
        "    return str(source.get(key) or from_settings or \"\").strip()",
    ),
    (
        "配置: 凭据不再成对决策（允许跨来源混搭）",
        CONFIG,
        "    settings_has_credentials = bool(\n"
        "        settings_env.get(\"ANTHROPIC_AUTH_TOKEN\") or settings_env.get(\"ANTHROPIC_API_KEY\")\n"
        "    )",
        "    settings_has_credentials = False",
    ),
    (
        "配置: thinking 开关失效（百炼下返回空内容）",
        CONFIG,
        "        if self.thinking in (\"enabled\", \"disabled\"):\n"
        "            return {\"type\": self.thinking}",
        "        if False:\n"
        "            return {\"type\": self.thinking}",
    ),
    (
        "配置: 非法的 thinking 值也被发出去",
        CONFIG,
        "        if self.thinking in (\"enabled\", \"disabled\"):",
        "        if self.thinking is not None:",
    ),
    (
        "list_files: glob 不再过滤（递归列出全部文件）",
        TOOLS_FILES,
        "                if glob and not _match_glob(name, glob):\n"
        "                    continue",
        "                if False:\n"
        "                    continue",
    ),
    (
        "list_files: 递归时不再跳过 __pycache__（统计偏大）",
        TOOLS_GREP,
        '        "__pycache__",\n',
        "",
    ),
    (
        "list_files: 默认也递归（破坏原有单层行为）",
        TOOLS_FILES,
        '    recursive = bool(data.get("recursive", False)) or bool(glob)',
        "    recursive = True",
    ),
    (
        "TUI: 未知斜杠命令被当成提问发给模型",
        TUI_RUNNER,
        '        state.push("error", f"未知命令：{stripped}。输入 /help 查看可用命令。")\n'
        "        return",
        '        state.push("error", "未知命令")',
    ),
]


#: 单次跑测试的上限（秒）。
#:
#: 必须有限制：有的变异会让程序**永久阻塞**而不是失败（例如分帧状态机不再
#: 推进缓冲、子进程等待永远不会到的响应）。没有超时的话，变异检查会挂死
#: 而不是给出结论——这真实发生过一次（10 分钟 0.2 秒 CPU，纯阻塞）。
#: 超时按「被杀死」处理：它确实证明了测试能发现这个变异。
SUITE_TIMEOUT_SECONDS = 240

#: 变异前把原文另存一份的后缀。
#:
#: 存在的理由：如果这个脚本**被中途杀死**（Ctrl-C、超时、进程被杀），
#: `finally` 不会执行，变异就留在源码里了。这真实发生过一次——一次 kill
#: 让 `jsonrpc.py` 带着 `[:length // 3]` 留在工作区，随后测试诡异失败。
#: 有了这份备份，下一次运行开头就能把它修回来，不需要人工判断哪些字节是
#: 变异、哪些是我的代码。
BACKUP_SUFFIX = ".mutation-backup"


def backup_path(path: str) -> str:
    return path + BACKUP_SUFFIX


def heal_leftovers() -> list[str]:
    """如果上次运行被中断，用备份恢复源码。返回被恢复的文件列表。

    **扫描整棵树**而不是只遍历 `MUTATIONS` 里列出的路径。原因：「孤儿备份」
    真实发生过——上一次运行被 kill 时正改到某个变异，而那个变异后来被我**从
    列表里删掉/改写了**，于是按列表查找永远找不到它，备份残留在工作区里
    还可能被 `git add -A` 带进提交。按后缀全树扫描就没有这个盲区。
    """
    healed: list[str] = []
    for root, _dirs, files in os.walk(REPO):
        # 跳过版本库与缓存目录，避免误伤。
        if any(part in root for part in (".git", "__pycache__")):
            continue
        for name in files:
            if not name.endswith(BACKUP_SUFFIX):
                continue
            backup = os.path.join(root, name)
            target = backup[: -len(BACKUP_SUFFIX)]
            if not os.path.exists(target):
                # 只有备份没有原文：说明原文被删了，保留备份不做处理。
                continue
            try:
                pristine = open(backup, encoding="utf-8").read()
                open(target, "w", encoding="utf-8", newline="\n").write(pristine)
                os.remove(backup)
                healed.append(os.path.relpath(target, REPO))
            except OSError:
                pass
    return healed


def run_suite() -> tuple[bool, bool]:
    """跑一次测试。返回 `(是否通过, 是否超时)`。"""
    try:
        proc = subprocess.run(
            [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUITE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, True
    return proc.returncode == 0, False


def main() -> int:
    healed = heal_leftovers()
    if healed:
        print(f"已从上次中断中恢复：{', '.join(healed)}")

    baseline, baseline_timeout = run_suite()
    print(f"baseline: {'PASS' if baseline else 'FAIL'}")
    if not baseline:
        print("基线不通过，无法做变异检查。" + ("（基线超时）" if baseline_timeout else ""))
        return 1

    survived: list[str] = []
    for label, path, old, new in MUTATIONS:
        original = open(path, encoding="utf-8").read()
        if old not in original:
            print(f"  SKIP  {label}  (未找到待变异的代码片段)")
            survived.append(label + " [片段不匹配]")
            continue

        mutated = original.replace(old, new, 1)
        if mutated == original:
            # 变异没有改变任何字节 —— 检查会得出「测试无效」的错误结论。
            # 真实发生过：目标片段不带行首缩进，先匹配到了另一个分支，
            # 替换后那一段与原来**完全等价**。
            print(f"  ERROR {label}  (变异是空操作，检查配置有误)")
            survived.append(label + " [空操作变异]")
            continue

        try:
            # 先留备份，再改源码——顺序不能反，否则「刚改完就被杀」
            # 会留下没有备份的变异。
            with open(backup_path(path), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(original)
            open(path, "w", encoding="utf-8", newline="\n").write(mutated)
            passed, timed_out = run_suite()
            killed = not passed
        finally:
            open(path, "w", encoding="utf-8", newline="\n").write(original)
            try:
                os.remove(backup_path(path))
            except OSError:
                pass

        if timed_out:
            print(f"  KILLED  {label}  (超时——测试发现该变异会阻塞)")
        else:
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
    force_utf8_output()
    raise SystemExit(main())
