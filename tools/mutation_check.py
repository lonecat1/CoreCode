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
RUN_COMMAND = os.path.join(REPO, "src", "minicode", "tools", "run_command.py")
BACKGROUND_TASKS = os.path.join(REPO, "src", "minicode", "background_tasks.py")
EDIT_TOOLS = os.path.join(REPO, "src", "minicode", "tools", "edit.py")
TOOLS_INIT = os.path.join(REPO, "src", "minicode", "tools", "__init__.py")
FILE_REVIEW = os.path.join(REPO, "src", "minicode", "file_review.py")
SHORTCUTS = os.path.join(REPO, "src", "minicode", "local_tool_shortcuts.py")
GREP_FILES = os.path.join(REPO, "src", "minicode", "tools", "grep_files.py")
MCP_STATUS = os.path.join(REPO, "src", "minicode", "mcp", "status.py")
CLI_COMMANDS = os.path.join(REPO, "src", "minicode", "cli_commands.py")
PERMISSIONS = os.path.join(REPO, "src", "minicode", "permissions.py")
REPO_INIT = os.path.join(REPO, "src", "minicode", "repo_init.py")
HISTORY = os.path.join(REPO, "src", "minicode", "history.py")
WEB_UTILS = os.path.join(REPO, "src", "minicode", "utils", "web.py")
WEB_TOOLS = os.path.join(REPO, "src", "minicode", "tools", "web.py")
MANAGE_CLI = os.path.join(REPO, "src", "minicode", "manage_cli.py")

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
        # 锚点必须**唯一**：这行 `if record.status != STATUS_CLOSED:` 在成功路径
        # 与异常路径各出现一次。只写这一行的话，`replace(..., 1)` 会命中第一次
        # 出现的位置，而变异意图对其他路径未必成立。带上上方的注释即可唯一定位。
        "                # 被关闭过的 worker 状态不再改写——否则 close_agent 会看到「已完成」。\n"
        "                if record.status != STATUS_CLOSED:",
        "                # 被关闭过的 worker 状态不再改写——否则 close_agent 会看到「已完成」。\n"
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
    (
        "run_command: 开发命令被当成只读（危险命令绕过审批）",
        RUN_COMMAND,
        '    """命令名是否是纯观察类。只有这一类**免审批**。"""\n'
        "    return command in READONLY_COMMANDS",
        '    """命令名是否是纯观察类。只有这一类**免审批**。"""\n'
        "    return True",
    ),
    (
        "run_command: 管道/重定向不再交给 shell（片段被静态拆分）",
        RUN_COMMAND,
        "    if args:\n        return False\n"
        "    return any(char in SHELL_METACHARACTERS for char in command)",
        "    if args:\n        return False\n    return False",
    ),
    (
        "run_command: 给了 args 仍去拆分 command（Windows 路径被破坏）",
        RUN_COMMAND,
        "    if args:\n        return command.strip(), list(args)",
        "    if False:\n        return command.strip(), list(args)",
    ),
    (
        "run_command: 有 args 时仍当 shell 片段（| 变成真管道）",
        RUN_COMMAND,
        "    if args:\n        return False\n"
        "    return any(char in SHELL_METACHARACTERS for char in command)",
        "    return any(char in SHELL_METACHARACTERS for char in command)",
    ),
    (
        "run_command: && 被当成后台执行（顺序执行被误判）",
        RUN_COMMAND,
        '    return trimmed.endswith("&") and not trimmed.endswith("&&")',
        '    return trimmed.endswith("&")',
    ),
    (
        "run_command: 后台片段的结尾 & 没去掉",
        RUN_COMMAND,
        '    return command.strip().removesuffix("&").strip()',
        "    return command.strip()",
    ),
    (
        "run_command: 非零退出不算失败（agent 无法察觉命令失败）",
        RUN_COMMAND,
        "    if returncode != 0:",
        "    if False:",
    ),
    (
        "run_command: 非零退出不标记 fatal（与原版语义不符）",
        RUN_COMMAND,
        '            fatal=True,\n        )\n\n    return ToolResult.success(body or "(no output)")',
        '            fatal=False,\n        )\n\n    return ToolResult.success(body or "(no output)")',
    ),
    (
        "run_command: 空命令不再拒绝",
        RUN_COMMAND,
        "    if not command:\n"
        '        return ToolResult.failure("Command not allowed: empty command")',
        "    if False:\n"
        '        return ToolResult.failure("Command not allowed: empty command")',
    ),
    (
        "run_command: 不排除 WSL 转发器（Windows 上报错看不懂）",
        RUN_COMMAND,
        "    if found and not _is_wsl_shim(found):\n        return found",
        "    if found:\n        return found",
    ),
    (
        "run_command: shell 权限判定用绝对路径（分类器认不出 bash，绕过审批）",
        RUN_COMMAND,
        "        permission_command = SHELL_EXECUTABLE",
        "        permission_command = executable",
    ),
    (
        "run_command: cwd 参数被忽略（无法切换目录）",
        RUN_COMMAND,
        "    effective_cwd = (\n"
        '        resolve_tool_path(context, str(raw_cwd), "list") if raw_cwd else context.cwd\n'
        "    )",
        "    effective_cwd = context.cwd",
    ),
    (
        "run_command: 输出超限不截断",
        RUN_COMMAND,
        "    if len(stdout) + len(stderr) > MAX_OUTPUT_BYTES:",
        "    if False:",
    ),
    (
        "run_command: 后台任务不登记（UI 无法观测）",
        RUN_COMMAND,
        "            background_task=task,",
        "            background_task=None,",
    ),
    (
        "background: 已结束的任务仍被重新探测（PID 复用会错标回 running）",
        BACKGROUND_TASKS,
        '    if record.status != "running":\n        return record',
        "    if False:\n        return record",
    ),
    (
        "background: 进程不存在仍报 running（任务永远不结束）",
        BACKGROUND_TASKS,
        '    return replace(record, status="running" if alive else "completed")',
        '    return replace(record, status="running")',
    ),
    (
        "edit: replaceAll 被忽略（批量替换只改一处）",
        EDIT_TOOLS,
        "    if replace_all:\n        return content.replace(search, replace)\n"
        "    return content.replace(search, replace, 1)",
        "    if False:\n        return content.replace(search, replace)\n"
        "    return content.replace(search, replace, 1)",
    ),
    (
        "edit: 默认替换全部（与原版「只替换第一处」不符）",
        EDIT_TOOLS,
        "    return content.replace(search, replace, 1)",
        "    return content.replace(search, replace)",
    ),
    (
        "edit: search 找不到也继续（写坏文件）",
        EDIT_TOOLS,
        '        return ToolResult.failure(f"Text not found in {path}")',
        "        pass",
    ),
    (
        "edit: patch 任一替换失败仍写盘（留下半成品文件）",
        EDIT_TOOLS,
        '            return ToolResult.failure(f"Replacement {index} not found in {path}")',
        "            continue",
    ),
    (
        "edit: patch 的 replaceAll 标签恒为 replaceOnce（输出信息失真）",
        EDIT_TOOLS,
        "        applied.append(f\"#{index} {'replaceAll' if replace_all else 'replaceOnce'}\")",
        '        applied.append(f"#{index} replaceOnce")',
    ),
    (
        "edit: 文件不存在不报错（模型以为改成功了）",
        EDIT_TOOLS,
        '    except FileNotFoundError:\n        return None, ToolResult.failure(f"文件不存在：{display_path}")',
        "    except FileNotFoundError:\n        return \"\", None",
    ),
    (
        "edit: 读文件时归一化换行（CRLF 文件被改写）",
        EDIT_TOOLS,
        '        with open(target, encoding="utf-8", newline="") as handle:',
        '        with open(target, encoding="utf-8") as handle:',
    ),
    (
        "file_review: 写文件时转换换行（LF 文件被改成 CRLF）",
        FILE_REVIEW,
        '    with open(target_path, "w", encoding="utf-8", newline="") as handle:',
        '    with open(target_path, "w", encoding="utf-8") as handle:',
    ),
    (
        "file_review: 读文件时归一化换行（内容与磁盘不一致）",
        FILE_REVIEW,
        '        with open(target_path, encoding="utf-8", newline="") as handle:',
        '        with open(target_path, encoding="utf-8") as handle:',
    ),
    (
        "注册表: 不给 update_plan 接线（模型提示里让它用，实际不存在）",
        TOOLS_INIT,
        "    if plan is not None:\n        tools.append(create_update_plan_tool(plan))",
        "    if False:\n        tools.append(create_update_plan_tool(plan))",
    ),
    (
        "注册表: 撤销 update_plan 条件（没给 plan 也注册写工具）",
        TOOLS_INIT,
        "    if plan is not None:\n        tools.append(create_update_plan_tool(plan))",
        "    if True:\n        tools.append(create_update_plan_tool(plan))",
    ),
    (
        "快捷方式: /ls 前缀不再要求空格（/lsof 被当成 /ls）",
        SHORTCUTS,
        '    if text == "/ls" or text.startswith("/ls "):',
        '    if text.startswith("/ls"):',
    ),
    (
        "快捷方式: /read 不拒绝空路径（拿空路径去读文件）",
        SHORTCUTS,
        '        path = text[len("/read ") :].strip()\n        if not path:\n            return None',
        '        path = text[len("/read ") :].strip()',
    ),
    (
        "快捷方式: /write 不拒绝空路径",
        SHORTCUTS,
        "            target = payload[:split_at].strip()\n            if not target:\n                return None",
        "            target = payload[:split_at].strip()",
    ),
    (
        "快捷方式: /edit 用 split 解构（替换内容里的 :: 被丢掉）",
        SHORTCUTS,
        '        parts = _split_limited(payload, 3)\n        target = parts[0].strip() if parts else ""',
        '        parts = payload.split("::")\n        target = parts[0].strip() if parts else ""',
    ),
    (
        "快捷方式: /grep 不拒绝空 pattern",
        SHORTCUTS,
        "        pattern = parts[0].strip() if parts else \"\"\n        if not pattern:\n            return None",
        '        pattern = parts[0].strip() if parts else ""',
    ),
    (
        "快捷方式: /cmd 把 cwd 与命令解反（在错误目录执行）",
        SHORTCUTS,
        "            command_cwd = payload[:split_at].strip()\n            command_text = payload[split_at + 2 :].strip()",
        "            command_cwd = payload[split_at + 2 :].strip()\n            command_text = payload[:split_at].strip()",
    ),
    (
        "快捷方式: /cmd 不给命令时也返回（空命令）",
        SHORTCUTS,
        "        pieces = command_text.split()\n        if not pieces:\n            return None",
        "        pieces = command_text.split()",
    ),
    (
        "快捷方式: /patch 不检查奇数片段（配对错位）",
        SHORTCUTS,
        "        if not target or len(ops) < 2 or len(ops) % 2 != 0:",
        "        if not target or len(ops) < 2:",
    ),
    (
        "分派: REPL 不再尝试快捷方式（/ls 被送给模型）",
        CLI,
        "    if _run_tool_shortcut(host, line, ui):\n        return",
        "    if False:\n        return",
    ),
    (
        "分派: -p 单次模式不再尝试快捷方式",
        CLI,
        "        if _run_tool_shortcut(host, prompt, ui):\n            return 0",
        "        if False:\n            return 0",
    ),
    (
        "快捷方式: 不完整输出（用户 /read 却只看到一行摘要）",
        CLI,
        "    ui.tool_result(shortcut.tool_name, ok=result.ok, detail=result.output, full=True)",
        "    ui.tool_result(shortcut.tool_name, ok=result.ok, detail=result.output, full=False)",
    ),
    (
        "UI: full=True 被忽略（多行输出被压成一行）",
        UI_CONSOLE,
        "        if full:\n            head, _, rest = detail.strip().partition(\"\\n\")",
        "        if False:\n            head, _, rest = detail.strip().partition(\"\\n\")",
    ),
    (
        "grep: 搜单个文件时不处理文件路径（永远没有结果）",
        GREP_FILES,
        "    if os.path.isfile(target):\n        yield target\n        return",
        "    if False:\n        yield target\n        return",
    ),
    (
        "grep: 回退扫描不再跳过 .git/node_modules 等目录",
        GREP_FILES,
        "        subdirectories[:] = [\n"
        "            name for name in subdirectories if name not in SKIP_DIRECTORIES\n"
        "        ]",
        "        pass",
    ),
    (
        "配置: 不再合并 Claude 兼容层（迁移用户配置全部失效）",
        CONFIG,
        "    merged = merge_settings(claude, {\"mcpServers\": global_mcp})",
        "    merged = merge_settings({}, {\"mcpServers\": global_mcp})",
    ),
    (
        "配置: Claude 路径跟随 MINICODE_HOME（自我引用 / 破坏隔离）",
        CONFIG,
        '    return Path.home() / ".claude" / "settings.json"\n\n\n#: 导入期快照',
        '    return minicode_home() / ".claude" / "settings.json"\n\n\n#: 导入期快照',
    ),
    (
        "配置: 读设置回退到导入期快照（MINICODE_HOME 变化后读错文件）",
        CONFIG,
        "    target = path or settings_path()\n    try:\n        raw = target.read_text(encoding=\"utf-8\")",
        "    target = path or SETTINGS_PATH\n    try:\n        raw = target.read_text(encoding=\"utf-8\")",
    ),
    (
        "配置: 写设置回退到导入期快照（/model 写到错误位置）",
        CONFIG,
        "    target = path or settings_path()\n    existing = read_settings_file(target)",
        "    target = path or SETTINGS_PATH\n    existing = read_settings_file(target)",
    ),
    (
        "权限: 存储读写回退到导入期快照（授权规则静默失效）",
        PERMISSIONS,
        "    target = path or permissions_path()\n    try:\n        raw = target.read_text(encoding=\"utf-8\")",
        "    target = path or PERMISSIONS_PATH\n    try:\n        raw = target.read_text(encoding=\"utf-8\")",
    ),
    (
        "MCP: 未知状态被算作 connected（数字看起来正常但是错的）",
        MCP_STATUS,
        "        if status == STATUS_CONNECTED:\n            summary.connected += 1",
        "        if status == STATUS_CONNECTED or status not in (\n"
        "            STATUS_CONNECTING,\n            STATUS_ERROR,\n        ):\n"
        "            summary.connected += 1",
    ),
    (
        "MCP: 负的工具数不再钳零（总数被拉小）",
        MCP_STATUS,
        "            summary.tool_count += max(0, tool_count)",
        "            summary.tool_count += tool_count",
    ),
    (
        "MCP: 状态行不显示等待与失败（用户看不出有问题）",
        MCP_STATUS,
        "        if self.connecting:\n            text += f\" · {self.connecting} 等待\"\n"
        "        if self.error:\n            text += f\" · {self.error} 失败\"",
        "        if False:\n            text += \"\"\n        if False:\n            text += \"\"",
    ),
    (
        "MCP: 没有服务器时仍显示 MCP 行（凭空多一行）",
        MCP_STATUS,
        "        if not self.total:\n            return \"\"",
        "        if False:\n            return \"\"",
    ),
    (
        "配置路径: 不显示 Claude 兼容回退（用户不知道配置从哪来）",
        CLI_COMMANDS,
        '        ("compat fallback", str(claude_settings_path())),\n',
        "",
    ),
    (
        "UI: wrap_values 被忽略（长路径被截断，命令失去意义）",
        UI_RENDER,
        "        if not wrap_values:\n            lines.append(truncate_plain(head + value, limit))\n            continue",
        "        if True:\n            lines.append(truncate_plain(head + value, limit))\n            continue",
    ),
    (
        "UI: 横幅不显示 MCP 行",
        UI_RENDER,
        '    if mcp_summary:\n        rows.append(("MCP", mcp_summary))',
        "    if False:\n        rows.append((\"MCP\", mcp_summary))",
    ),
    (
        "init: MINI.md 用覆盖写（用户的项目说明被模板冲掉）",
        REPO_INIT,
        '        with open(file_path, "x", encoding="utf-8", newline="") as handle:\n'
        "            handle.write(content)\n        return \"created\"\n"
        "    except FileExistsError:\n        return \"skipped\"",
        '        with open(file_path, "w", encoding="utf-8", newline="") as handle:\n'
        "            handle.write(content)\n        return \"created\"\n"
        "    except FileExistsError:\n        return \"skipped\"",
    ),
    (
        "init: .gitignore 被重写（用户已有规则丢失）",
        REPO_INIT,
        "    current = [line.rstrip() for line in existing.split(\"\\n\")]\n"
        "    changed = False",
        "    current = []\n    changed = True",
    ),
    (
        "init: .mini-code 已存在为文件时不报错（静默覆盖）",
        REPO_INIT,
        "        if not os.path.isdir(dir_path):\n"
        "            raise NotADirectoryError(\n"
        '                f"Cannot initialize: {dir_path} exists and is not a directory"\n'
        "            )",
        "        if False:\n            pass",
    ),
    (
        "init: TypeScript 与 JavaScript 同时列出（语言层重复）",
        REPO_INIT,
        "    if d.typescript:\n        languages.append(\"TypeScript\")\n"
        "    elif d.package_json:\n        languages.append(\"JavaScript/Node.js\")",
        "    if d.typescript:\n        languages.append(\"TypeScript\")\n"
        "    if d.package_json:\n        languages.append(\"JavaScript/Node.js\")",
    ),
    (
        "init: Next.js 项目里也提 React（噪音）",
        REPO_INIT,
        "    if d.react and not d.nextjs:",
        "    if d.react:",
    ),
    (
        "init: 探测不到也写空标题（看起来像生成失败）",
        REPO_INIT,
        "    verify = verification_lines(cwd, detection)\n    if verify:\n"
        '        sections.append(["## Verification", *verify, ""])',
        "    verify = verification_lines(cwd, detection)\n"
        '    sections.append(["## Verification", *verify, ""])',
    ),
    (
        "init: package.json 匹配区分大小写（React 认不出）",
        REPO_INIT,
        '    package_json_text = _read_text_or_empty(os.path.join(cwd, "package.json")).lower()',
        '    package_json_text = _read_text_or_empty(os.path.join(cwd, "package.json"))',
    ),
    (
        "init: 三种状态的报告文字相同（用户分不清发生了什么）",
        REPO_INIT,
        '    if status == "updated":\n        return "updated"',
        '    if status == "updated":\n        return "created"',
    ),
    (
        "历史: 没有新条目也重写文件（每次退出都把整份历史再追加一遍）",
        HISTORY,
        "    if not new_entries:\n        return 0",
        "    if False:\n        return 0",
    ),
    (
        "历史: 不去重（同一行反复追加，文件成倍增长）",
        HISTORY,
        "        if not text or text in existing or text in seen:\n            continue",
        "        if not text:\n            continue",
    ),
    (
        "历史: 坏行导致整体读取失败（用户丢掉全部历史）",
        HISTORY,
        "        except json.JSONDecodeError:\n            continue",
        "        except json.JSONDecodeError:\n            return []",
    ),
    (
        "历史: 缺 display 字段的条目也被收下（空条目进历史）",
        HISTORY,
        '        display = parsed.get("display")\n'
        "        if not isinstance(display, str):\n            continue",
        '        display = parsed.get("display") or ""',
    ),
    (
        "历史: 超限时保留最旧的（丢掉刚敲的）",
        HISTORY,
        "        kept = lines[-MAX_ENTRIES:]",
        "        kept = lines[:MAX_ENTRIES]",
    ),
    (
        "历史: 超限时不截断（文件无限增长）",
        HISTORY,
        "    if len(lines) <= MAX_ENTRIES:\n        return",
        "    if True:\n        return",
    ),
    (
        "历史: 空字符串也写进历史",
        HISTORY,
        "        if not text or text in existing or text in seen:",
        "        if text in existing or text in seen:",
    ),
    (
        "web: 4xx 也被当成可重试（重试不会让 404 变成 200，白等）",
        WEB_UTILS,
        "    return status == 429 or 500 <= status < 600",
        "    return True",
    ),
    (
        "web: 5xx 不再重试（暂时性失败直接暴露给用户）",
        WEB_UTILS,
        "    return status == 429 or 500 <= status < 600",
        "    return status == 429",
    ),
    (
        "web: 超时不再算可重试的网络错误",
        WEB_UTILS,
        "    if isinstance(error, (socket.timeout, TimeoutError)):\n        return True",
        "    if False:\n        return True",
    ),
    (
        "web: 状态码重试的退避不翻倍（固定间隔，限流时雪上加霜）",
        WEB_UTILS,
        "        if is_retryable_status(snapshot.status) and attempt < max_retries:\n"
        "            _sleep_ms(BACKOFF_BASE_MS * (2**attempt))\n"
        "            continue",
        "        if is_retryable_status(snapshot.status) and attempt < max_retries:\n"
        "            _sleep_ms(BACKOFF_BASE_MS)\n"
        "            continue",
    ),
    (
        "web: 网络错误重试的退避不翻倍（固定间隔）",
        WEB_UTILS,
        "            if attempt < max_retries and is_retryable_network_error(error):\n"
        "                _sleep_ms(BACKOFF_BASE_MS * (2**attempt))\n"
        "                continue",
        "            if attempt < max_retries and is_retryable_network_error(error):\n"
        "                _sleep_ms(BACKOFF_BASE_MS)\n"
        "                continue",
    ),
    (
        "web: HTTP 错误码不再当作响应（正文丢失）",
        WEB_UTILS,
        "        except urllib.error.HTTPError as error:",
        "        except urllib.error.HTTPError as error:\n            raise WebError(str(error)) from error\n        if False:",
    ),
    (
        "web: 域名匹配用朴素后缀（evil-python.org 通过白名单）",
        WEB_UTILS,
        '    return host == domain or host.endswith(f".{domain}")',
        "    return host.endswith(domain)",
    ),
    (
        "web: 黑名单不再优先（允许列表放行了被排除的域）",
        WEB_UTILS,
        "    if any(matches_domain(host, domain) for domain in blocked):\n        return False",
        "    if False:\n        return False",
    ),
    (
        "web: 解析不出主机名的链接被放行（过滤器形同虚设）",
        WEB_UTILS,
        "    if not host:\n        return False",
        "    if not host:\n        return True",
    ),
    (
        "web: 允许列表为空时反而全部拒绝",
        WEB_UTILS,
        "    if not allowed:\n        return True",
        "    if not allowed:\n        return False",
    ),
    (
        "web: 域名归一化不剥通配前缀（*.example.com 匹配不上）",
        WEB_UTILS,
        '        without_wildcard = re.sub(r"^\\*\\.", "", re.sub(r"^\\.", "", text))',
        "        without_wildcard = text",
    ),
    (
        "web: 搜狗链接分支顺序退回原版（协议相对链接被拼坏）",
        WEB_UTILS,
        '    if href.startswith("//"):\n        return f"https:{href}"\n'
        '    if href.startswith("/"):\n        return f"https://www.sogou.com{href}"',
        '    if href.startswith("/"):\n        return f"https://www.sogou.com{href}"\n'
        '    if href.startswith("//"):\n        return f"https:{href}"',
    ),
    (
        "web: DDG 解析不再校验 result-link class（抓到无关链接）",
        WEB_UTILS,
        '        if not re.search(r"\\bresult-link\\b", class_value, re.IGNORECASE):\n            continue',
        "        pass",
    ),
    (
        "web: 搜索不退回第二个后端（单一后端挂了就整个不可用）",
        WEB_UTILS,
        '            errors.append(f"{provider}: no results")\n        except WebError as error:',
        '            return [], "fallback-empty"\n        except WebError as error:',
    ),
    (
        "web: 后端返回 200 但没有结果也算成功（不再换源）",
        WEB_UTILS,
        "            if organic:\n                return organic, provider",
        "            return organic, provider",
    ),
    (
        "web: 全部后端失败也不报错（用户以为确实没搜到）",
        WEB_UTILS,
        "    if errors:\n"
        "        # **只要有后端失败就抛。** 原版是 `if (errors.length > 0) throw`。",
        "    if False:\n"
        "        # **只要有后端失败就抛。** 原版是 `if (errors.length > 0) throw`。",
    ),
    (
        "web: 默认结果条数从 5 变 8（与原版不符）",
        WEB_UTILS,
        "DEFAULT_MAX_RESULTS = 5",
        "DEFAULT_MAX_RESULTS = 8",
    ),
    (
        "web: 不跟进 HTML 跳转（抓回来是空壳）",
        WEB_UTILS,
        "        if redirect_url and redirect_url != final_url:",
        "        if False:",
    ),
    (
        "web: 正文不截断（超长页面塞爆上下文）",
        WEB_UTILS,
        "    if max_chars and len(content) > max_chars:\n        content = content[:max_chars]",
        "    if False:\n        content = content[:max_chars]",
    ),
    (
        "web 工具: 去掉 allowed/blocked 的互斥校验（语义含糊）",
        WEB_TOOLS,
        "    if allowed and blocked:",
        "    if False:",
    ),
    (
        "web 工具: 域名过滤不传给搜索层（参数被忽略）",
        WEB_TOOLS,
        '            allowed_domains=data.get("allowed_domains"),\n'
        '            blocked_domains=data.get("blocked_domains"),',
        "            allowed_domains=None,\n            blocked_domains=None,",
    ),
    (
        "web 工具: max_results 不再限制上限",
        WEB_TOOLS,
        "        if max_results < 1 or max_results > MAX_MAX_RESULTS:",
        "        if max_results < 1:",
    ),
    (
        "web 工具: 输出格式退回旧版（丢掉 QUERY 行）",
        WEB_TOOLS,
        '    lines: list[str] = [f"QUERY: {query}", ""]',
        "    lines: list[str] = []",
    ),
    (
        "管理: --project 被忽略（项目配置写进用户目录）",
        MANAGE_CLI,
        '    if "--project" in rest:\n        rest.remove("--project")\n        return "project", rest',
        '    if "--project" in rest:\n        rest.remove("--project")\n        return "user", rest',
    ),
    (
        "管理: take_option 不移除已消费的选项（未知参数检查失效）",
        MANAGE_CLI,
        "    value = args[index + 1]\n    del args[index : index + 2]\n    return value",
        "    value = args[index + 1]\n    return value",
    ),
    (
        "管理: 选项缺值时静默取到下一个参数",
        MANAGE_CLI,
        '    if index + 1 >= len(args) or args[index + 1] == "":\n'
        '        raise ManageError(f"Missing value for {name}")\n'
        "    value = args[index + 1]",
        "    if index + 1 >= len(args):\n        raise ManageError(f\"Missing value for {name}\")\n"
        "    value = args[index + 1]",
    ),
    (
        "管理: KEY=VALUE 按所有等号分割（值被静默截断）",
        MANAGE_CLI,
        '        separator = entry.find("=")',
        '        separator = entry.rfind("=")',
    ),
    (
        "管理: -- 之后的内容仍被当作本命令的选项",
        MANAGE_CLI,
        '    if "--" in rest:\n        separator = rest.index("--")\n'
        "        head = rest[:separator]\n        command_parts = rest[separator + 1 :]",
        '    if "--" in rest:\n        separator = rest.index("--")\n'
        "        head = rest[:separator]\n        command_parts = []",
    ),
    (
        "管理: 同时给 --url 与本地命令不再报错（配置无法使用）",
        MANAGE_CLI,
        "    if has_url and has_command:\n"
        '        raise ManageError("Cannot set both --url and local command. Choose one.")',
        "    if False:\n"
        '        raise ManageError("Cannot set both --url and local command. Choose one.")',
    ),
    (
        "管理: 没给 --url 也没给命令不再报错（写出空配置）",
        MANAGE_CLI,
        '    if not has_url and not has_command:\n        raise ManageError("Missing MCP command or --url.")',
        "    if False:\n"
        '        raise ManageError("Missing MCP command or --url.")',
    ),
    (
        "管理: streamable-http 不要求 --url（一定连不上）",
        MANAGE_CLI,
        '    if protocol == "streamable-http" and not has_url:',
        "    if False:",
    ),
    (
        "管理: 未知协议被接受",
        MANAGE_CLI,
        "    if protocol is not None and protocol not in VALID_PROTOCOLS:",
        "    if False:",
    ),
    (
        "管理: 删不存在的服务器被当成错误",
        MANAGE_CLI,
        '    if name not in existing:\n'
        "        # 删一个不存在的**不算错误**：用户的意图（「让它不存在」）已经满足。\n"
        '        print(f"MCP server {name} not found in {path}")\n        return True',
        '    if name not in existing:\n        raise ManageError(f"MCP server {name} not found in {path}")',
    ),
    (
        "管理: login 不校验 token 非空",
        MANAGE_CLI,
        '    if not token:\n        raise ManageError("Missing --token value.")',
        "    if False:\n"
        '        raise ManageError("Missing --token value.")',
    ),
    (
        "管理: 写配置不用缩进（人无法阅读的配置文件）",
        CONFIG,
        '        json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8"',
        '        json.dumps(payload, ensure_ascii=False) + "\\n", encoding="utf-8"',
    ),
    (
        "管理: 非管理 argv 也返回 True（正常启动被吞掉）",
        MANAGE_CLI,
        '    if category not in ("mcp", "skills"):\n        return False',
        '    if category not in ("mcp", "skills"):\n        return True',
    ),
    (
        "管理: 子命令错误不再转成退出码（静默成功）",
        MANAGE_CLI,
        "    except ManageError as error:\n"
        '        print(f"error: {error}", file=sys.stderr)\n'
        "        raise SystemExit(1) from error",
        "    except ManageError as error:\n"
        '        print(f"error: {error}", file=sys.stderr)',
    ),
    (
        "CLI: 管理子命令在 argparse 之后才拦截（-- 让 argparse 报错）",
        CLI,
        "    if maybe_handle_management_command(_cwd_from_argv(raw), _management_argv(raw)):\n        return 0",
        "    if False:\n        return 0",
    ),
    (
        "CLI: 不剥掉本程序自己的选项（--cwd 让子命令识别失败）",
        CLI,
        '        if item in _OWN_VALUE_OPTIONS:\n            index += 2  # 连同它的值一起跳过\n            continue',
        "        if False:\n            index += 2\n            continue",
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


def audit_anchors() -> list[str]:
    """检查每条变异的锚点是否**唯一**，返回有问题的描述。

    ## 为什么需要这一步

    `main()` 用 `str.replace(old, new, 1)` **只替换第一处**。如果锚点在文件里
    出现多次，变异就会落在第一个匹配处——那可能不是这条变异想改的地方。

    真实发生过：`web: 退避不翻倍` 的锚点是
    `"                _sleep_ms(BACKOFF_BASE_MS * (2**attempt))\\n                continue"`
    （16 空格缩进）。文件里有**两处**退避调用，一处 16 空格（网络错误路径）、
    一处 12 空格（状态码路径）。锚点只命中前者，而当时的测试只覆盖后者——
    变异因此存活，看起来像「测试没覆盖退避翻倍」，实际是**锚点指错了地方**。

    有了这一步，这类问题在开跑前就报出来，而不是等 25 分钟后从存活列表里猜。
    """
    problems: list[str] = []
    for label, path, old, _new in MUTATIONS:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as error:
            problems.append(f"{label} [无法读取 {path}: {error}]")
            continue
        count = text.count(old)
        if count > 1:
            problems.append(f"{label} [锚点出现 {count} 次，替换位置有歧义]")
    return problems


def main() -> int:
    healed = heal_leftovers()
    if healed:
        print(f"已从上次中断中恢复：{', '.join(healed)}")

    # 先审锚点：歧义的锚点会让变异落错位置，得出的结论是错的。
    ambiguous = audit_anchors()
    if ambiguous:
        print(f"变异锚点有歧义（{len(ambiguous)} 条），先修好再跑：")
        for item in ambiguous:
            print("  -", item)
        return 1

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
