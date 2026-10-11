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
AGENTS_MANAGER = os.path.join(REPO, "src", "minicode", "agents", "manager.py")
AGENTS_TYPES = os.path.join(REPO, "src", "minicode", "agents", "types.py")
SKILLS = os.path.join(REPO, "src", "minicode", "skills.py")
TOOLS_INIT = os.path.join(REPO, "src", "minicode", "tools", "__init__.py")
MCP_STDIO = os.path.join(REPO, "src", "minicode", "mcp", "stdio_client.py")
MCP_JSONRPC = os.path.join(REPO, "src", "minicode", "mcp", "jsonrpc.py")
TUI_FRAMES = os.path.join(REPO, "src", "minicode", "tui", "frames.py")
TUI_WIDTH = os.path.join(REPO, "src", "minicode", "tui", "width.py")
TUI_PANELS = os.path.join(REPO, "src", "minicode", "tui", "panels.py")
CONFIG = os.path.join(REPO, "src", "minicode", "config.py")

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
        "TUI: 相同帧也重绘（差分失效）",
        TUI_FRAMES,
        "        if next_exists == prev_exists and next_line == previous_line:\n"
        "            continue",
        "        if False:\n            continue",
    ),
    (
        "TUI: 列数变化不再强制全量重绘",
        TUI_FRAMES,
        "        state.lines is None or state.columns != columns or state.rows != rows",
        "        state.lines is None",
    ),
    (
        # 先前的写法是把哨兵 `None` 换回 `""`。那其实是**等价变异**：
        # 写入时用 `next_line or ""`，因此 `None` 与 `""` 产生的字节完全相同，
        # 测试通过是正确判定，不是测试无效。换成一个真正改变行为的变异——
        # 让「行是否存在」这个判断失效，从而在缩帧/增帧时越界或漏擦。
        "TUI: 不再区分「行不存在」与「空行」",
        TUI_FRAMES,
        "        next_exists = index < len(lines)\n"
        "        prev_exists = index < len(previous)",
        "        next_exists = True\n"
        "        prev_exists = True",
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
        "TUI: 上下文徽章进度条不再夹在 0..10",
        TUI_PANELS,
        "    filled = max(0, min(10, filled))",
        "    filled = filled",
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
    """如果上次运行被中断，用备份恢复源码。返回被恢复的文件列表。"""
    healed: list[str] = []
    for _label, path, _old, _new in MUTATIONS:
        backup = backup_path(path)
        if not os.path.exists(backup):
            continue
        try:
            pristine = open(backup, encoding="utf-8").read()
            open(path, "w", encoding="utf-8", newline="\n").write(pristine)
            os.remove(backup)
            healed.append(os.path.basename(path))
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
    raise SystemExit(main())
