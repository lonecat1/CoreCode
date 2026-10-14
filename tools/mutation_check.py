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
CONFIG = os.path.join(REPO, "src", "minicode", "config.py")
TOOLS_FILES = os.path.join(REPO, "src", "minicode", "tools", "files.py")
TOOLS_GREP = os.path.join(REPO, "src", "minicode", "tools", "grep_files.py")
UI_THEME = os.path.join(REPO, "src", "minicode", "ui", "theme.py")
UI_RENDER = os.path.join(REPO, "src", "minicode", "ui", "render.py")
UI_CONSOLE = os.path.join(REPO, "src", "minicode", "ui", "console.py")
UI_CAPS = os.path.join(REPO, "src", "minicode", "ui", "caps.py")
UI_ANIMATE = os.path.join(REPO, "src", "minicode", "ui", "animate.py")
UI_STARTUP = os.path.join(REPO, "src", "minicode", "ui", "startup.py")
CLI = os.path.join(REPO, "src", "minicode", "cli.py")

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
        "UI: 非终端也上色（转义序列污染重定向的文件）",
        UI_THEME,
        "    if _color_override is False:\n        return False",
        "    if False:\n        return False",
    ),
    (
        "UI: NO_COLOR 被无视",
        UI_THEME,
        '    if os.environ.get("NO_COLOR"):\n        return False',
        "    if False:\n        return False",
    ),
    (
        "UI: 非终端流也被判定为支持颜色",
        UI_RENDER,
        "    try:\n        return bool(stream.isatty())\n"
        "    except (AttributeError, ValueError):\n        return False",
        "    return True",
    ),
    (
        "UI: 流式输出不 flush 尾巴（丢最后一段）",
        UI_RENDER,
        "        if not self._buffer:\n            return \"\"\n"
        "        tail, self._buffer = self._buffer, \"\"",
        "        if True:\n            return \"\"\n"
        "        tail, self._buffer = self._buffer, \"\"",
    ),
    (
        "UI: 工具输出不压缩（刷屏）",
        UI_CONSOLE,
        "        short = _shorten(detail)\n        label = short or summary",
        "        label = summary",
    ),
    (
        "UI: ask_user 不再醒目提示（用户以为程序卡住）",
        UI_CONSOLE,
        '        if summary == "ask_user":\n            self.question(detail)\n            return',
        '        if False:\n            self.question(detail)\n            return',
    ),
    (
        "UI: 回合结束不冲流式缓冲",
        UI_CONSOLE,
        "        if not self._streamed_this_turn:\n            self.flush_assistant()",
        "        if False:\n            self.flush_assistant()",
    ),
    (
        "动画: 非终端也做动画（\\r 污染重定向文件）",
        UI_CAPS,
        "    if not is_tty(stream):\n        return False",
        "    if False:\n        return False",
    ),
    (
        "动画: 需要 VT 的动画不检查 VT（Windows 打印字面量）",
        UI_CAPS,
        "    if require_vt and not enable_virtual_terminal():\n        return False",
        "    if False:\n        return False",
    ),
    (
        "动画: 转圈停止时不擦除（残影留在屏幕上）",
        UI_ANIMATE,
        "        if self._drawn and self.enabled:\n"
        "            with self._lock:\n"
        "                self._write(CARRIAGE_RETURN + ERASE_LINE)\n"
        "            self._drawn = False",
        "        if False:\n            self._drawn = False",
    ),
    (
        "动画: 块动画帧高不固定（相对移动算错、画面漂移）",
        UI_ANIMATE,
        "        while len(frame) < self.height:\n            frame.append(\"\")",
        "        pass",
    ),
    (
        "动画: 块动画用绝对定位而非相对移动",
        UI_ANIMATE,
        "                parts.append(cursor_up(self._drawn_height - 1))",
        "                parts.append(\"\")",
    ),
    (
        "动画: 块动画 clear 不擦除（残影）",
        UI_ANIMATE,
        "            parts = [cursor_up(self._drawn_height - 1), CARRIAGE_RETURN, ERASE_TO_END]",
        "            parts = []",
    ),
    (
        "动画: 窄终端下截断像素画（字形变乱码）",
        UI_STARTUP,
        "        if natural == 0 or natural > self.width:\n            return []",
        "        if natural == 0:\n            return []",
    ),
    (
        "动画: 启动动画不设时长上限（拖慢启动）",
        UI_ANIMATE,
        "            if max_seconds is not None and time.monotonic() - started > max_seconds:\n"
        "                break",
        "            if False:\n                break",
    ),
    (
        "启动动画: 全亮后不停留（一闪而过）",
        UI_STARTUP,
        "        if self.hold_seconds > 0 and not (callable(abort) and abort()):",
        "        if False:",
    ),
    (
        "流式: 不剥协议标记（<final> 显示给用户）",
        UI_CONSOLE,
        "    for opening, closing in _RESPONSE_MARKERS:\n"
        "        stripped = cleaned.lstrip()",
        "    for opening, closing in ():\n"
        "        stripped = cleaned.lstrip()",
    ),
    (
        "流式: 短回答不冲探测缓冲（一个字都不显示）",
        UI_CONSOLE,
        "        if not self._stream_probe_done and self._stream_probe:\n"
        "            text = self._release_stream_probe()\n"
        "            if text:\n"
        "                self._emit(text)",
        "        if False:\n"
        "            text = self._release_stream_probe()\n"
        "            if text:\n"
        "                self._emit(text)",
    ),
    (
        "流式: 不扣留末尾半个收尾标签（界面残留 </final>）",
        UI_CONSOLE,
        "        combined = self._tail_hold + text\n"
        "        keep = _longest_closing_prefix(combined)",
        "        combined = self._tail_hold + text\n"
        "        keep = 0",
    ),
    (
        "流式: 不检查是否可能是标记前缀（短增量被攒住、卡顿）",
        UI_CONSOLE,
        "            if not _could_be_marker_prefix(self._stream_probe):\n"
        "                text = self._release_stream_probe()",
        "            if False:\n"
        "                text = self._release_stream_probe()",
    ),
    (
        "流式: 流式后不忽略 assistant（内容重复）",
        UI_CONSOLE,
        "        if self._streamed_this_turn:\n            return\n"
        "        printer = self._ensure_printer()",
        "        if False:\n            return\n"
        "        printer = self._ensure_printer()",
    ),
    (
        "流式: 流式后不忽略 progress（进度重复）",
        UI_CONSOLE,
        "        if self._streamed_this_turn:\n            return\n"
        "        self.write(render_progress(text, self.width))",
        "        if False:\n            return\n"
        "        self.write(render_progress(text, self.width))",
    ),
    (
        "统计: 无回合记录也显示（凭空多一行）",
        UI_CONSOLE,
        '        if not getattr(stats, "turns", 0):\n            return',
        "        if False:\n            return",
    ),
    (
        "分派: 斜杠快捷方式被当成未知命令拦掉（功能回归）",
        CLI,
        "    return any(usage.startswith(stripped) for usage, _ in _COMMAND_USAGES)",
        "    return True",
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
