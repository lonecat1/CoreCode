"""Mutation check: do the runtime tests actually protect the four gates?

A green suite only tells us the tests pass. This script deliberately breaks each
guarantee and asserts that the suite goes red. If a mutation survives, the test
covering it is decorative.

Every mutation is applied in memory to a copy of the file, then restored from git.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

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
SESSION_STORE = os.path.join(REPO, "src", "minicode", "session", "store.py")
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
INSTALL = os.path.join(REPO, "src", "minicode", "install.py")
MEMORY = os.path.join(REPO, "src", "minicode", "memory.py")
MCP_HTTP = os.path.join(REPO, "src", "minicode", "mcp", "http_client.py")
MCP_BRIDGE = os.path.join(REPO, "src", "minicode", "mcp", "bridge.py")
PIPELINE = os.path.join(REPO, "src", "minicode", "context", "pipeline.py")
CONSTANTS = os.path.join(REPO, "src", "minicode", "context", "constants.py")
RUNTIME_CONTEXT = os.path.join(REPO, "src", "minicode", "runtime", "context.py")
CHECK_CONSTANTS = os.path.join(REPO, "tools", "check_constants.py")
CHECK_DUPLICATES = os.path.join(REPO, "tools", "check_duplicates.py")
FILE_MAP = os.path.join(REPO, "tools", "file_map.py")
CHECK_DOCS = os.path.join(REPO, "tools", "check_docs.py")
ANTHROPIC_ADAPTER = os.path.join(
    REPO, "src", "minicode", "adapters", "anthropic.py"
)
LOOP = os.path.join(REPO, "src", "minicode", "runtime", "loop.py")
TOOLS_GOAL = os.path.join(REPO, "src", "minicode", "tools", "goal.py")
PLAN = os.path.join(REPO, "src", "minicode", "runtime", "plan.py")
GOAL_RUNTIME = os.path.join(REPO, "src", "minicode", "runtime", "goal.py")

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
        '    return Path.home() / ".claude" / "settings.json"\n\n\n'
        "#: Claude Code 的配置文件路径快照。",
        '    return minicode_home() / ".claude" / "settings.json"\n\n\n'
        "#: Claude Code 的配置文件路径快照。",
    ),
    (
        "配置: settings_path 返回固定路径（MINICODE_HOME 变化后失效）",
        CONFIG,
        'def settings_path() -> Path:\n    return minicode_home() / "settings.json"',
        'def settings_path() -> Path:\n    return Path.home() / ".mini-code" / "settings.json"',
    ),
    (
        "配置: permissions_path 返回固定路径（授权规则静默失效）",
        CONFIG,
        'def permissions_path() -> Path:\n    return minicode_home() / "permissions.json"',
        'def permissions_path() -> Path:\n    return Path.home() / ".mini-code" / "permissions.json"',
    ),
    (
        # **这条变异已删除。** 它把 `minicode_home()` 改成硬编码
        # `Path.home() / ".mini-code"`，于是写配置的测试会**写到用户的真实
        # 家目录**——`tests/test_install.py` 的答案 `["m", "https://x", "t"]`
        # 曾经真的覆盖过 `~/.mini-code/settings.json`，导致之后每次运行都因
        # `https://x` 解析不了而报 `getaddrinfo failed`。
        #
        # 变异检查的目的是「让测试变红」，不是「让测试改用户的机器」。
        # 现在 `_real_home_fingerprint()` 会在任何变异改动真实家目录时立即中止
        # （退出码 2），但这一条**必然**触发它，跑不了，所以从清单里去掉。
        #
        # 留了一个**等价但安全**的替代：只认一种环境变量拼写。它同样让
        # 「家目录可重定向」这条契约失效（D47），但只在临时目录之间走错，
        # 不会越界到用户的真实家目录。
        "配置: 家目录只认 MINICODE_HOME（不认原版拼写 MINI_CODE_HOME）",
        CONFIG,
        '    override = env_value("HOME")',
        '    override = os.environ.get("MINICODE_HOME")',
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
        '            f"compat fallback: {claude_settings_path()}",\n',
        "",
    ),
    (
        # 这里原本有一条「`wrap_values` 被忽略」的变异。`/config-paths` 改成
        # 逐字输出原版四行之后，`wrap_values=True` 再无调用者，那条变异**永远
        # 杀不死**（324/325 就是它）。死代码已删除，变异随之删除。
        # 留着一条恒定存活的变异会污染「0 存活」这个信号——它看起来像一个漏洞，
        # 实际是一个不该存在的检查项。
        "UI: 键值表不再截断超长值（长行溢出终端）",
        UI_RENDER,
        "        lines.append(truncate_plain(head + value, limit))",
        "        lines.append(head + value)",
    ),
    (
        "UI: 横幅不显示 mcp 徽章",
        UI_RENDER,
        '        _color_badge("mcp", f"{mcp_connected}/{mcp_total}", MAGENTA),\n',
        "",
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
        "    if is_timeout_error(error):\n        return True\n"
        "    code = node_error_code(error)",
        "    code = node_error_code(error)",
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
    (
        "安装: PATH 判断写死冒号（Windows 上永远判定不在 PATH）",
        INSTALL,
        '    entries = (os.environ.get("PATH") or "").split(os.pathsep)',
        '    entries = (os.environ.get("PATH") or "").split(":")',
    ),
    (
        "安装: 必填项接受空值（写出空 BASE_URL）",
        INSTALL,
        "        value = answer or default\n        if value:\n            return value",
        "        value = answer or default\n        return value",
    ),
    (
        "安装: EOF 时不回退默认值（管道输入拿到空串）",
        INSTALL,
        "        if incoming is None:\n            # EOF：不再重问（否则管道输入会死循环）。\n            return default",
        '        if incoming is None:\n            return ""',
    ),
    (
        "安装: 模型名只写顶层（env.ANTHROPIC_MODEL 缺失）",
        INSTALL,
        '                "ANTHROPIC_MODEL": model,',
        '                "ANTHROPIC_MODEL": "",',
    ),
    (
        "安装: 留空时清掉已保存的 token（用户被迫每次重填）",
        INSTALL,
        "    token_input = (incoming or \"\").strip()\n    auth_token = token_input or saved_token",
        "    token_input = (incoming or \"\").strip()\n    auth_token = token_input",
    ),
    (
        "安装: token 为空时不报错（写出无法使用的配置）",
        INSTALL,
        "    if not auth_token:\n        raise ValueError(",
        "    if False:\n        raise ValueError(",
    ),
    (
        "安装: PATH 提示不按平台区分（Windows 用户拿到 bash 说明）",
        INSTALL,
        '    if platform.system() == "Windows":\n        return (\n'
        '            "可以把下面这行加入 PowerShell 的 $PROFILE，或"\n'
        '            "在「系统属性 → 环境变量」里追加：\\n"\n'
        "            f'$env:Path = \"{bin_dir};\" + $env:Path\\n'\n        )",
        '    if False:\n        return (\n'
        '            "可以把下面这行加入 PowerShell 的 $PROFILE，或"\n'
        '            "在「系统属性 → 环境变量」里追加：\\n"\n'
        "            f'$env:Path = \"{bin_dir};\" + $env:Path\\n'\n        )",
    ),
    (
        # 锚点刻意选**不含转义序列**的短片段。上一版把整行写进锚点，里面
        # 有 `\r\n` / `\n`——在 `mutation_check.py` 这一层再转义一次极易出错，
        # 实际结果就是三条锚点全部失效（[未找到]）。短片段同样唯一，且不用转义。
        "安装: 启动器不指向模块入口（指向不存在的文件）",
        INSTALL,
        ' -m minicode.cli "$@"',
        ' "minicode.py" "$@"',
    ),
    (
        "安装: .cmd 用 LF（某些 cmd 版本解析异常）",
        INSTALL,
        r'        "@echo off\r\n"',
        r'        "@echo off\n"',
    ),
    (
        "安装: 启动器写入非 ASCII 注释（PowerShell 5.1 乱码）",
        INSTALL,
        "# MiniCode launcher (PowerShell)",
        "# MiniCode 启动器",
    ),
    (
        "安装: 不创建启动器目录",
        INSTALL,
        "        target_bin.mkdir(parents=True, exist_ok=True)",
        "        pass",
    ),
    (
        "配置: 凭据报错不再指明设置位置（用户不知道去哪加）",
        CONFIG,
        '            "未配置凭据。请在 "\n'
        '            f"{_settings_path()} 的 env 里、或进程环境变量中"\n'
        '            "设置 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）。"',
        '            "未配置凭据。"',
    ),
    (
        "配置: 未配置凭据时不再报错（带着空凭据继续跑）",
        CONFIG,
        "    if not auth_token and not api_key:",
        "    if False:",
    ),
    (
        "UI: 横幅内边距算成 width-5（每行都少一列，右边框错开）",
        UI_RENDER,
        "    inner = max(0, width - 4)\n    left_width = string_display_width(left)",
        "    inner = max(0, width - 5)\n    left_width = string_display_width(left)",
    ),
    (
        "UI: 边框少一段横线（顶边比内容行短）",
        UI_RENDER,
        "    inner = max(0, width - 2)\n    left = BOX_TOP_LEFT if top else BOX_BOTTOM_LEFT",
        "    inner = max(0, width - 3)\n    left = BOX_TOP_LEFT if top else BOX_BOTTOM_LEFT",
    ),
    (
        "配置: 兜底输出上限退回旧值 8192（长回答被截断）",
        CONFIG,
        "UNKNOWN_MODEL_MAX_OUTPUT_TOKENS: tuple[int, int] = (32_000, 64_000)",
        "UNKNOWN_MODEL_MAX_OUTPUT_TOKENS: tuple[int, int] = (8_192, 8_192)",
    ),
    (
        "配置: 规则顺序反转（具体模式被宽泛模式吃掉）",
        CONFIG,
        "    normalized = model.strip().lower()\n"
        "    for patterns, default, upper in MODEL_MAX_OUTPUT_TOKEN_RULES:",
        "    normalized = model.strip().lower()\n"
        "    for patterns, default, upper in reversed(MODEL_MAX_OUTPUT_TOKEN_RULES):",
    ),
    (
        "配置: 用默认值覆盖用户配置（我明明设了却没用）",
        CONFIG,
        "    if configured is None or configured <= 0:\n        return default\n"
        "    return min(int(configured), upper)",
        "    return default",
    ),
    (
        "配置: 模型名匹配区分大小写（Claude-Opus-4-6 认不出）",
        CONFIG,
        "    normalized = model.strip().lower()",
        "    normalized = model.strip()",
    ),
    (
        "配置: 加一条粗粒度 deepseek 规则（静默砍掉用户预算）",
        CONFIG,
        '    (("deepseek-reasoner",), 32_000, 64_000),',
        '    (("deepseek",), 8_192, 8_192),\n    (("deepseek-reasoner",), 32_000, 64_000),',
    ),
    (
        "记忆: 引用行判定退回字面空格（制表符分隔被误当引用）",
        MEMORY,
        "        match = _INCLUDE_LINE_RE.fullmatch(stripped)",
        '        match = stripped.startswith("@") and " " not in stripped',
    ),
    (
        "记忆: visited 递归后不移除（重复包含误报为环且内容缺失）",
        MEMORY,
        "        finally:\n"
        "            # 见上面 ②：递归返回后移除，恢复「只对祖先判环」的语义。\n"
        "            visited.discard(key)",
        "        finally:\n            pass",
    ),
    (
        "记忆: 不再拒绝父目录穿越（可读仓库外文件）",
        MEMORY,
        '        if os.path.isabs(reference) or ".." in Path(reference).parts:',
        "        if os.path.isabs(reference):",
    ),
    (
        "记忆: 不再拒绝绝对路径",
        MEMORY,
        "        if os.path.isabs(reference) or \"..\" in Path(reference).parts:",
        '        if ".." in Path(reference).parts:',
    ),
    (
        "记忆: 深度上限不再生效（长链无限递归）",
        MEMORY,
        "        if depth >= MAX_INCLUDE_DEPTH:",
        "        if False:",
    ),
    (
        "MCP-HTTP: accept 声明 SSE（服务器选中我们走不通的路径）",
        MCP_HTTP,
        '            "accept": "application/json",',
        '            "accept": "application/json, text/event-stream",',
    ),
    (
        "MCP-HTTP: 通知也带上 id（不再是 JSON-RPC 通知）",
        MCP_HTTP,
        "        if not notify:\n            payload[\"id\"] = self._next_id\n            self._next_id += 1",
        "        if True:\n            payload[\"id\"] = self._next_id\n            self._next_id += 1",
    ),
    (
        "MCP-HTTP: 已关闭的连接仍然发送（状态失效）",
        MCP_HTTP,
        '        if self._closed:\n            raise McpHttpError("连接已关闭。")',
        "        if False:\n"
        '            raise McpHttpError("连接已关闭。")',
    ),
    (
        "MCP-HTTP: JSON-RPC error 被当成正常结果（错误静默）",
        MCP_HTTP,
        '        rpc_error = parsed.get("error")\n'
        "        if isinstance(rpc_error, dict):\n"
        '            raise McpHttpError(str(rpc_error.get("message") or "unknown error"))',
        '        rpc_error = parsed.get("error")\n        if False:\n'
        '            raise McpHttpError(str(rpc_error.get("message") or "unknown error"))',
    ),
    (
        "MCP-HTTP: HTTP 错误不再带状态码（无法诊断）",
        MCP_HTTP,
        '            raise McpHttpError(" | ".join(parts), status=error.code) from error',
        '            raise McpHttpError(" | ".join(parts)) from error',
    ),
    (
        "MCP-HTTP: 401 不再附鉴权提示（用户不知道去哪授权）",
        MCP_HTTP,
        "            hint = _extract_auth_hint(error.headers)\n"
        "            parts = [f\"HTTP {error.code} {error.reason}\", f\"服务器 {self.name}\"]\n"
        "            if hint:\n                parts.append(f\"AUTH: {hint}\")",
        "            hint = _extract_auth_hint(error.headers)\n"
        "            parts = [f\"HTTP {error.code} {error.reason}\", f\"服务器 {self.name}\"]\n"
        "            if False:\n                parts.append(f\"AUTH: {hint}\")",
    ),
    (
        "MCP-HTTP: 非 JSON 响应不再报错（返回空结果）",
        MCP_HTTP,
        '        except json.JSONDecodeError as error:\n            raise McpHttpError(\n'
        '                f"服务器 {self.name} 返回了非 JSON 内容：{raw.strip()[:200]}"\n'
        "            ) from error",
        "        except json.JSONDecodeError as error:\n            return {}",
    ),
    (
        "MCP-HTTP: bearer token 不再发送（认证失败）",
        MCP_HTTP,
        '        if self.auth_token:\n            request_headers["Authorization"] = f"Bearer {self.auth_token}"',
        "        if False:\n"
        '            request_headers["Authorization"] = f"Bearer {self.auth_token}"',
    ),
    (
        "管线: auto-compact 后替换引用而非原地清空（过期 span 写进会话文件）",
        PIPELINE,
        "                self._sync_collapse_state(ContextCollapseState())",
        "                self.collapse_state = ContextCollapseState()",
    ),
    (
        "管线: collapse 后不同步调用方的 state（共享状态失效）",
        PIPELINE,
        "        self._sync_collapse_state(collapse_result.state)",
        "        self.collapse_state = collapse_result.state",
    ),
    (
        "管线: auto-compact 不再限于 step 0（反复触发）",
        PIPELINE,
        "        if step == 0:",
        "        if True:",
    ),
    (
        "管线: snip 每回合不再限一次（每步都重试）",
        PIPELINE,
        "        if not self.snipped_this_turn:",
        "        if True:",
    ),
    (
        "管线: 没有模型名时仍然尝试压缩（算不出窗口）",
        PIPELINE,
        "        if not model_name:\n"
        "            # 没有模型名就算不出窗口，四级压缩全部跳过。\n"
        "            return current, current",
        "        if False:\n            return current, current",
    ),
    (
        "loop: 间隔文法退回不支持小数（1.5h 被当成 prompt）",
        LOOP,
        'r"^(\\d+(?:\\.\\d+)?)(m|h)$"',
        'r"^(\\d+)(m|h)$"',
    ),
    (
        "loop: 去掉「数字前缀就报错」（写错的间隔被当成 prompt）",
        LOOP,
        "    elif _NUMERIC_LIKE_RE.match(first):\n"
        "        # 见上面：这个分支防止「间隔写错了却被当成 prompt」。\n"
        '        raise LoopError("间隔格式不对。用分钟（Nm）或小时（Nh），至少 1 分钟。")',
        "    elif False:\n"
        '        raise LoopError("间隔格式不对。用分钟（Nm）或小时（Nh），至少 1 分钟。")',
    ),
    (
        "loop: 去掉最小间隔检查（可以设 1 秒循环）",
        LOOP,
        "    if interval < MIN_LOOP_INTERVAL_MS or interval > MAX_LOOP_INTERVAL_MS:",
        "    if interval > MAX_LOOP_INTERVAL_MS:",
    ),
    (
        "loop: 去掉最大间隔检查（超出计时器上限）",
        LOOP,
        "    if interval < MIN_LOOP_INTERVAL_MS or interval > MAX_LOOP_INTERVAL_MS:",
        "    if interval < MIN_LOOP_INTERVAL_MS:",
    ),
    (
        "loop: 小时被当成分钟（2h 变成 2 分钟）",
        LOOP,
        'interval = int(value * (3_600_000 if unit == "h" else 60_000))',
        "interval = int(value * 60_000)",
    ),
    (
        "loop: 指定间隔后没有 prompt 时不再报错",
        LOOP,
        "    if not prompt:\n"
        '        raise LoopError("用法：/loop [Nm|Nh] <prompt>")\n'
        "    return prompt, interval",
        "    return prompt, interval",
    ),
    (
        "update_plan: 输出退回一行摘要（模型拿不到 id）",
        TOOLS_GOAL,
        "            payload[\"explanation\"] = explanation.strip()\n"
        "        return ToolResult.success(json.dumps(payload, ensure_ascii=False))",
        "            payload[\"explanation\"] = explanation.strip()\n"
        "        completed = sum(1 for todo in document.todos if todo.status == \"completed\")\n"
        "        return ToolResult.success(\n"
        "            f\"Plan updated: {completed}/{len(document.todos)} completed.\"\n"
        "        )",
    ),
    (
        "update_plan: 不回显 explanation（模型以为写进去了）",
        TOOLS_GOAL,
        "        if isinstance(explanation, str) and explanation.strip():\n"
        '            payload["explanation"] = explanation.strip()',
        "        if False:\n"
        '            payload["explanation"] = explanation.strip()',
    ),
    (
        "update_plan: 字段名退回 snake_case（与原版表示不同）",
        TOOLS_GOAL,
        '        "schemaVersion": getattr(document, "schema_version", 1),',
        '        "schema_version": getattr(document, "schema_version", 1),',
    ),
    (
        "plan: 空字符串 id 被当成「省略」（想更新却静默变成新增）",
        PLAN,
        "                if not isinstance(raw_id, str) or not raw_id.strip():",
        "                if False:",
    ),
    (
        "get_goal: 只回 goal 不回 plan（模型看不到 todo id）",
        TOOLS_GOAL,
        '                    "plan": _plan_to_json(plan_document),',
        '                    "plan": None,',
    ),
    (
        "update_goal: 输出退回一行摘要（模型看不到结构化结果）",
        TOOLS_GOAL,
        "        return ToolResult.success(json.dumps(payload, ensure_ascii=False))\n\n"
        "    def get_goal",
        "        return ToolResult.success(f\"Goal 已更新（{document.status}）。\")\n\n"
        "    def get_goal",
    ),
    (
        "goal: completion 不再嵌套（形状与原版不同）",
        GOAL_RUNTIME,
        '        payload["completion"] = {\n'
        '            "summary": goal.completion_summary,\n'
        '            "checks": list(goal.completion_checks),\n'
        '            "checkedAt": goal.completion_checked_at or "",\n'
        "        }",
        '        payload["completion"] = goal.completion_summary',
    ),
    (
        "goal: 完成时不再记录 checkedAt",
        GOAL_RUNTIME,
        "            completion_checked_at=_now_iso(),",
        "            completion_checked_at=None,",
    ),
    (
        "goal: 可选字段写成 null（模型以为有值但为空）",
        GOAL_RUNTIME,
        '    if goal.stop_reason is not None:\n        payload["stopReason"] = goal.stop_reason',
        '    payload["stopReason"] = goal.stop_reason',
    ),
    (
        "goal: get_snapshot 退回逐字段构造（新增字段被漏掉）",
        GOAL_RUNTIME,
        "        return replace(\n"
        "            self._document,\n"
        "            completion_criteria=list(self._document.completion_criteria),\n"
        "            completion_checks=list(self._document.completion_checks),\n"
        "        )",
        "        return GoalDocument(\n"
        "            schema_version=self._document.schema_version,\n"
        "            id=self._document.id,\n"
        "            session_id=self._document.session_id,\n"
        "            plan_id=self._document.plan_id,\n"
        "            description=self._document.description,\n"
        "            completion_criteria=list(self._document.completion_criteria),\n"
        "            status=self._document.status,\n"
        "            needs_replan=self._document.needs_replan,\n"
        "            stop_reason=self._document.stop_reason,\n"
        "            completion_summary=self._document.completion_summary,\n"
        "            completion_checks=list(self._document.completion_checks),\n"
        "            updated_at=self._document.updated_at,\n"
        "        )",
    ),
    (
        "bridge: 空白描述不回退（模型拿到空描述）",
        MCP_BRIDGE,
        "        cleaned_description = (descriptor.description or \"\").strip()\n"
        "        description = cleaned_description or (\n"
        '            f"Call MCP tool {remote_name} from server {server}."\n'
        "        )",
        "        description = descriptor.description or "
        'f"Call MCP tool {remote_name} from server {server}."',
    ),
    (
        "bridge: 失败时不报配置的协议（用户看不出试过什么）",
        MCP_BRIDGE,
        "                        protocol=_configured_protocol(config),",
        "                        protocol=None,",
    ),
    (
        "bridge: hydrate 失败不关 client（进程挂着却显示 error）",
        MCP_BRIDGE,
        "            try:\n"
        "                client.close()\n"
        "            except Exception:  # noqa: BLE001 - 关闭失败不该掩盖原始错误\n"
        "                pass",
        "            pass",
    ),
    (
        "bridge: sanitize 不再小写（工具名大小写混乱）",
        MCP_BRIDGE,
        "    lowered = (value or \"\").strip().lower()",
        "    lowered = (value or \"\").strip()",
    ),
    (
        "bridge: sanitize 不去首尾下划线（出现 mcp____x）",
        MCP_BRIDGE,
        '    cleaned = _UNSAFE_SEGMENT.sub("_", lowered).strip("_")',
        '    cleaned = _UNSAFE_SEGMENT.sub("_", lowered)',
    ),
    (
        "bridge: 空片段不再兜底成 tool（工具名变成 mcp____）",
        MCP_BRIDGE,
        '    return cleaned or "tool"',
        "    return cleaned",
    ),
    (
        "bridge: 非对象 schema 不再给宽松默认（远端拒收）",
        MCP_BRIDGE,
        '    return {"type": "object", "additionalProperties": True}',
        '    return {"type": "object"}',
    ),
    (
        "bridge: auto 协议在失败时也被报出来（没有信息量）",
        MCP_BRIDGE,
        '    if protocol in (None, "auto"):\n        return None',
        "    if protocol is None:\n        return None",
    ),
]


#: 文本解码健壮性（D45）。非法 UTF-8 不该让程序起不来，也不该写坏文件。
_DECODING_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "解码: settings.json 读到非法字节就崩（用户起不来）",
        CONFIG,
        '        raw = target.read_text(encoding="utf-8", errors="replace")\n'
        "    except FileNotFoundError:\n"
        "        return {}\n"
        "    except OSError as error:\n"
        '        raise ConfigError(f"无法读取 {target}：{error}") from error',
        '        raw = target.read_text(encoding="utf-8")\n'
        "    except FileNotFoundError:\n"
        "        return {}\n"
        "    except OSError as error:\n"
        '        raise ConfigError(f"无法读取 {target}：{error}") from error',
    ),
    (
        "解码: history 读到非法字节就崩（整个 CLI 起不来）",
        HISTORY,
        'path or history_path(), encoding="utf-8", errors="replace", newline=""',
        'path or history_path(), encoding="utf-8", newline=""',
    ),
    (
        "解码: /init 读到非法字节就崩（只 except OSError）",
        REPO_INIT,
        'gitignore_path, encoding="utf-8", errors="replace", newline=""',
        'gitignore_path, encoding="utf-8", newline=""',
    ),
    (
        "解码: mcp 配置读到非法字节就崩",
        CONFIG,
        '        raw = path.read_text(encoding="utf-8", errors="replace")\n'
        "    except OSError:",
        '        raw = path.read_text(encoding="utf-8")\n    except OSError:',
    ),
    (
        "解码: 非 UTF-8 文件被当成空文件（会覆盖用户文件）",
        FILE_REVIEW,
        "    except UnicodeDecodeError as error:",
        "    except FileNotFoundError as error:",
    ),
    (
        "解码: 非 UTF-8 时漏出原始 codec 消息（用户看不懂）",
        FILE_REVIEW,
        '        raise FileReviewError(\n'
        '            f"{target_path} 不是 UTF-8 文本（{error.reason}），"\n'
        '            "为避免写坏文件，本次修改已取消。"\n'
        "        ) from error",
        "        raise",
    ),
]

MUTATIONS = [*MUTATIONS, *_DECODING_MUTATIONS]


#: 斜杠命令表与补全。这张表同时是 `/help`、Tab 补全、命令分派的唯一事实来源。
_SLASH_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "slash: 无命中时不再列出全部（Tab 什么都没反应）",
        CLI_COMMANDS,
        "    return (hits if hits else all_slash_usages(), line)",
        "    return (hits, line)",
    ),
    (
        "slash: 补全改成「只匹配第一段」（/loop s 匹配不上）",
        CLI_COMMANDS,
        "    hits = find_matching_slash_commands(line)",
        "    hits = find_matching_slash_commands(line.split()[0] if line.split() else line)",
    ),
    (
        "slash: /loop stop 被合并掉（/help 少一行、补全错）",
        CLI_COMMANDS,
        '    SlashCommand("/loop", "/loop stop", "停止 Loop，回合进行中或等待批准时也生效。"),\n',
        "",
    ),
    (
        "slash: /goal pause 被合并掉",
        CLI_COMMANDS,
        '    SlashCommand("/goal", "/goal pause [reason]", "暂停 Goal，回合进行中或等待批准时也生效。"),\n',
        "",
    ),
    (
        "slash: /help 挤成一行（用户读不了）",
        CLI_COMMANDS,
        '    return "\\n".join(f"{item.usage}    {item.description}" for item in SLASH_COMMANDS)',
        '    return " ".join(f"{item.usage}    {item.description}" for item in SLASH_COMMANDS)',
    ),
    (
        "slash: 补全把 delims 留空（readline 按词切，语义不对）",
        CLI_COMMANDS,
        '        readline.set_completer_delims("")',
        "        pass",
    ),
    (
        "slash: readline 不可用时抛错（Windows 起不来）",
        CLI_COMMANDS,
        "    except ImportError:\n        return False",
        "    except ImportError:\n        raise",
    ),
    (
        "slash: 补全器忽略整行、只用 readline 传来的词",
        CLI_COMMANDS,
        "            line = readline.get_line_buffer()",
        "            line = text",
    ),
]

MUTATIONS = [*MUTATIONS, *_SLASH_MUTATIONS]


#: 启动横幅：字段与布局对齐原版（`tui/chrome.ts` 的 `renderBanner`）。
_BANNER_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "横幅: 用中文键值行而不是徽章（字段与原版不同）",
        UI_RENDER,
        '        _color_badge("session", "local", BRIGHT_YELLOW),',
        '        "模型 " + shown_model,',
    ),
    (
        "横幅: 丢掉 provider 徽章（用户看不出连的是哪）",
        UI_RENDER,
        '        _color_badge("provider", shown_provider, CYAN),\n',
        "",
    ),
    (
        "横幅: 丢掉 messages 徽章",
        UI_RENDER,
        '        _color_badge("messages", str(message_count), BRIGHT_CYAN),\n',
        "",
    ),
    (
        "横幅: 丢掉 events 徽章",
        UI_RENDER,
        '        _color_badge("events", str(event_count), BLUE),\n',
        "",
    ),
    (
        "横幅: provider 没值时不回落 offline",
        UI_RENDER,
        "    raw = (provider or \"\").strip()\n    if not raw:\n        return \"offline\"",
        "    raw = (provider or \"\").strip()\n    if not raw:\n        return \"\"",
    ),
    (
        "横幅: 标题后不空一行（与原版布局不同）",
        UI_RENDER,
        '            _panel_row("", panel_width),\n',
        "",
    ),
    (
        "横幅: 宽度不设 60 下限",
        UI_RENDER,
        "    panel_width = max(60, width if width is not None else (terminal_columns() or 100))",
        "    panel_width = max(20, width if width is not None else (terminal_columns() or 100))",
    ),
    (
        "横幅: 宽度被封顶到正文宽度（200 列终端只画 100 列）",
        UI_RENDER,
        "    panel_width = max(60, width if width is not None else (terminal_columns() or 100))",
        "    panel_width = max(60, min(100, width if width is not None else (terminal_columns() or 100)))",
    ),
    (
        "横幅: 放不下时直接截断、不加省略号",
        UI_RENDER,
        '        with_ellipsis = f"{output}{separator}{paint(\'...\', DIM)}"\n'
        "        if string_display_width(with_ellipsis) <= max_width:\n"
        "            return with_ellipsis",
        "        with_ellipsis = output\n"
        "        if string_display_width(with_ellipsis) <= max_width:\n"
        "            return with_ellipsis",
    ),
    (
        "横幅: mcp-wait / mcp-err 无条件出现",
        UI_RENDER,
        "    if mcp_connecting > 0:",
        "    if True:",
    ),
]

MUTATIONS = [*MUTATIONS, *_BANNER_MUTATIONS]


#: 会话文件格式：必须能读原版 TUI 写出来的会话（`tests/test_session_format.py`）。
_SESSION_FORMAT_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "会话: thinking 的事件类型名退回 role 名（原版用 thinking）",
        SESSION_STORE,
        "    ROLE_ASSISTANT_THINKING: EVENT_THINKING,",
        "    ROLE_ASSISTANT_THINKING: ROLE_ASSISTANT_THINKING,",
    ),
    (
        "会话: 摘要的事件类型名退回 role 名（原版用 summary）",
        SESSION_STORE,
        "    ROLE_CONTEXT_SUMMARY: EVENT_SUMMARY,",
        "    ROLE_CONTEXT_SUMMARY: ROLE_CONTEXT_SUMMARY,",
    ),
    (
        "会话: 事件不再写 uuid（原版的链靠它串起来）",
        SESSION_STORE,
        '        "uuid": event_uuid,\n',
        "",
    ),
    (
        "会话: 事件不再写 parentUuid（消息链断掉）",
        SESSION_STORE,
        '        "parentUuid": parent_uuid,\n',
        "",
    ),
    (
        "会话: 事件不再写 sessionId",
        SESSION_STORE,
        '        "sessionId": session_id,\n        "cwd": cwd,\n        "parentUuid": parent_uuid,',
        '        "cwd": cwd,\n        "parentUuid": parent_uuid,',
    ),
    (
        "会话: `unwrap_message` 不覆盖 id（自带 id 与 uuid 冲突）",
        SESSION_STORE,
        '    result["id"] = event.get("uuid")',
        '    result.setdefault("id", event.get("uuid"))',
    ),
    (
        "会话: 不跳过 system 消息（它会下盘，与新版系统提示冲突）",
        SESSION_STORE,
        '        if message.get("role") == ROLE_SYSTEM:\n            continue\n',
        "",
    ),
    (
        "会话: 项目目录名不折叠冒号（与原版目录对不上，读不到旧会话）",
        SESSION_STORE,
        '    collapsed = re.sub(r"[/\\\\:]+", "-", cwd)',
        '    collapsed = re.sub(r"[/\\\\]+", "-", cwd)',
    ),
]

MUTATIONS = [*MUTATIONS, *_SESSION_FORMAT_MUTATIONS]


#: 权限存储与分类器：必须与原版同一份契约（`tests/test_permissions_format.py`）。
_PERMISSION_FORMAT_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "权限: 存储字段改成 snake_case（读不到原版写的规则）",
        PERMISSIONS,
        '"allowedEditPatterns": self.allowed_edit_patterns,',
        '"allowed_edit_patterns": self.allowed_edit_patterns,',
    ),
    (
        "权限: 写盘不再缩进（与原版来回改会整篇重排）",
        PERMISSIONS,
        "json.dumps(store.to_json(), indent=2, ensure_ascii=False) + \"\\n\",",
        "json.dumps(store.to_json(), ensure_ascii=False) + \"\\n\",",
    ),
    (
        "权限: 存储不写空数组（原版不省略）",
        PERMISSIONS,
        "    def to_json(self) -> dict[str, list[str]]:\n        return {",
        "    def to_json(self) -> dict[str, list[str]]:\n"
        "        return {k: v for k, v in {",
    ),
    (
        "权限: 拒绝检查挪到危险门之前（无害命令也被拒）",
        PERMISSIONS,
        "        dangerous_reason = classify_dangerous_command(command, args)\n"
        "        reason = (force_prompt_reason or \"\").strip() or dangerous_reason\n"
        "        if not reason:\n            return",
        "        dangerous_reason = classify_dangerous_command(command, args)\n"
        "        if format_command_signature(command, args) in self._denied_commands:\n"
        '            raise PermissionDenied("denied")\n'
        "        reason = (force_prompt_reason or \"\").strip() or dangerous_reason\n"
        "        if not reason:\n            return",
    ),
    (
        "权限: 编辑选项少一条（自动化脚本的 4 变成未定义）",
        PERMISSIONS,
        '        PermissionChoice("4", "始终允许此文件", "allow_always"),\n',
        "",
    ),
    (
        "权限: git restore --source 不再判为危险",
        PERMISSIONS,
        '        if "restore" in normalized and any(a.startswith("--source") for a in normalized):',
        "        if False and any(a.startswith(\"--source\") for a in normalized):",
    ),
    (
        "权限: publish 不再判为危险",
        PERMISSIONS,
        '    if command in ("npm", "pnpm", "yarn") and "publish" in normalized:',
        '    if command in ("npm", "pnpm", "yarn") and False:',
    ),
    (
        "权限: 加严名单退回只认 npm（pnpm/yarn publish 不再提示）",
        PERMISSIONS,
        '    if command in ("npm", "pnpm", "yarn") and "publish" in normalized:',
        '    if command == "npm" and "publish" in normalized:',
    ),
    (
        "权限: 解释器名单退回原版 5 个（pwsh/powershell 不再提示）",
        PERMISSIONS,
        '    {"node", "python3", "python", "bun", "bash", "sh", "pwsh", "powershell"}',
        '    {"node", "python3", "bun", "bash", "sh"}',
    ),
]

MUTATIONS = [*MUTATIONS, *_PERMISSION_FORMAT_MUTATIONS]


#: 四级压缩的阈值与保留策略（`tests/test_compaction_thresholds.py`）。
#:
#: 这些数字是最容易「差不多就行」的地方：0.85 写成 0.84、`>=` 写成 `>`，
#: 日常使用几乎看不出来，但边界行为完全不同。期望值来自
#: `tools/compaction_compare.py` 的跨实现对照（17/17 一致）。
_COMPACTION_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "压缩: microcompact 门槛从 0.50 提到 0.60",
        CONSTANTS,
        "MICROCOMPACT_UTILIZATION = 0.50",
        "MICROCOMPACT_UTILIZATION = 0.60",
    ),
    (
        "压缩: snip 门槛从 0.70 提到 0.80",
        CONSTANTS,
        "SNIP_COMPACT_THRESHOLD = 0.70",
        "SNIP_COMPACT_THRESHOLD = 0.80",
    ),
    (
        "压缩: auto-compact 门槛从 0.85 降到 0.84",
        CONSTANTS,
        "AUTOCOMPACT_UTILIZATION = 0.85",
        "AUTOCOMPACT_UTILIZATION = 0.84",
    ),
    (
        "压缩: blocked 门槛从 0.95 提到 0.99",
        CONSTANTS,
        "BLOCKED_UTILIZATION = 0.95",
        "BLOCKED_UTILIZATION = 0.99",
    ),
    (
        "压缩: 只保留最近 2 条工具结果（清得更多）",
        CONSTANTS,
        "KEEP_RECENT_TOOL_RESULTS = 3",
        "KEEP_RECENT_TOOL_RESULTS = 2",
    ),
    (
        "压缩: snip 保留最近 8 条（剪得更狠）",
        CONSTANTS,
        "SNIP_KEEP_RECENT_MESSAGES = 12",
        "SNIP_KEEP_RECENT_MESSAGES = 8",
    ),
    (
        "压缩: snip 最少删 2 条（更容易触发）",
        CONSTANTS,
        "SNIP_MIN_MESSAGES_TO_REMOVE = 6",
        "SNIP_MIN_MESSAGES_TO_REMOVE = 2",
    ),
    (
        "压缩: snip 最少省 0 token（门槛失效）",
        CONSTANTS,
        "SNIP_MIN_TOKENS_TO_FREE = 2_000",
        "SNIP_MIN_TOKENS_TO_FREE = 0",
    ),
    (
        "压缩: 工具结果清空标记改成空串（模型看到空白）",
        CONSTANTS,
        'CLEAR_MARKER = "[Output cleared for context space]"',
        'CLEAR_MARKER = ""',
    ),
]

MUTATIONS = [*MUTATIONS, *_COMPACTION_MUTATIONS]


#: MCP 通用工具的注册与路由（`tests/test_mcp.py` 的 BridgeTests）。
#:
#: 这一组防的是「按服务器各注册一份」那个真实缺陷：两台服务器都发布资源时，
#: 模型会收到两组同名工具声明，而 `find()` 只取第一个 —— 第二台静默消失。
_MCP_ROUTING_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "MCP: 通用工具退回「按服务器各注册一份」（两台服务器 → 重名）",
        MCP_BRIDGE,
        "        if self._published_resources:\n"
        "            self.tools.extend(self._build_resource_tools())",
        "        for _n, _c in self._published_resources.items():\n"
        "            self.tools.extend(self._build_resource_tools())",
    ),
    (
        "MCP: 描述里写死第一台服务器名",
        MCP_BRIDGE,
        '                description=(\n'
        '                    "List optional MCP resources exposed by connected MCP "\n'
        '                    "servers when a server actually publishes them."\n'
        "                ),",
        '                description=(\n'
        '                    f"列出 MCP 服务器 {next(iter(self._published_resources))} 发布的资源。"\n'
        "                ),",
    ),
    (
        "MCP: 列表行不带服务器名前缀（跨服务器时分不清归属）",
        MCP_BRIDGE,
        '                    lines.append(f"{name}: {item.uri}{shown_name}{described}")',
        '                    lines.append(f"{item.uri}{shown_name}{described}")',
    ),
    (
        "MCP: 缺 server 时只列第一台（不遍历全部）",
        MCP_BRIDGE,
        "            return sorted(self._published_resources.items())",
        "            return sorted(self._published_resources.items())[:1]",
    ),
    (
        "MCP: 未发布时不给那条「不代表工具不可用」的说明",
        MCP_BRIDGE,
        "            return ToolResult.success(\n"
        '                "Connected MCP servers did not publish any MCP resources. "\n'
        '                "This does not mean MCP tools are unavailable."\n'
        "            )",
        '            return ToolResult.success("没有资源。")',
    ),
    (
        "MCP: read_mcp_resource 的 server 变成可选",
        MCP_BRIDGE,
        '                    "required": ["server", "uri"],',
        '                    "required": ["uri"],',
    ),
    (
        "MCP: 未知服务器的报错文案换成中文（契约不同）",
        MCP_BRIDGE,
        '                return ToolResult.failure(f"Unknown MCP server: {server}")\n'
        "            try:\n"
        "                result = client.read_resource(uri)",
        '                return ToolResult.failure(f"没有这个服务器：{server}")\n'
        "            try:\n"
        "                result = client.read_resource(uri)",
    ),
    (
        "MCP: 提示行的必填参数不再标星",
        MCP_BRIDGE,
        "                        f\"{arg.get('name')}{'*' if arg.get('required') else ''}\"",
        "                        str(arg.get('name'))",
    ),
    (
        "MCP: get_mcp_prompt 的 arguments 缺 additionalProperties",
        MCP_BRIDGE,
        '                            "type": "object",\n'
        '                            "additionalProperties": {"type": "string"},',
        '                            "type": "object",',
    ),
]

MUTATIONS = [*MUTATIONS, *_MCP_ROUTING_MUTATIONS]


#: 运行时上下文注入（`tests/test_runtime_context.py`）。
#:
#: 这两段文本被拼进 system 消息，是模型判断「我在什么模式、该做什么」的全部依据。
#: 说明文案改一个字都可能改变模型取舍，而且不会有任何测试变红——除非钉住全文。
_CONTEXT_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "上下文: Goal 少一条说明（模型不知道要核对什么）",
        RUNTIME_CONTEXT,
        '    "Check explanations must describe actual results; this MVP checks structure, '
        'not evidence references.",\n',
        "",
    ),
    (
        "上下文: Loop 少一条说明",
        RUNTIME_CONTEXT,
        '    "The user can stop this Loop with /loop stop. '
        'There is no model-side stop tool in this MVP.",\n',
        "",
    ),
    (
        "上下文: Loop 快照不省略空值（写出 nextRunAt: null）",
        RUNTIME_CONTEXT,
        "        json.dumps(_loop_to_dict(snapshot), ensure_ascii=False),",
        "        json.dumps(snapshot or {}, ensure_ascii=False),",
    ),
    (
        "上下文: Goal 注入改回自己那份字段映射（与工具输出形状不一致）",
        RUNTIME_CONTEXT,
        "    from .goal import goal_to_json\n\n    return goal_to_json(goal)",
        "    from .goal import goal_to_json\n\n    return {**goal_to_json(goal), "
        '"stopReason": None} if goal is not None else None',
    ),
    (
        "上下文: Goal 块的闭合标签拼错",
        RUNTIME_CONTEXT,
        '    lines = ["<current_goal>", json.dumps(_goal_to_dict(snapshot), '
        'ensure_ascii=False), "</current_goal>"]',
        '    lines = ["<current_goal>", json.dumps(_goal_to_dict(snapshot), '
        'ensure_ascii=False), "</current_goals>"]',
    ),
    (
        "上下文: format_goal 不在需要规划时提示",
        RUNTIME_CONTEXT,
        '    if goal.needs_replan:\n        lines.append("Planning required.")',
        "    if False:\n        lines.append(\"Planning required.\")",
    ),
    (
        "上下文: format_goal 丢掉 waiting 后缀",
        RUNTIME_CONTEXT,
        '    waiting_suffix = " (waiting for your answer)" if waiting else ""',
        '    waiting_suffix = ""',
    ),
]

MUTATIONS = [*MUTATIONS, *_CONTEXT_MUTATIONS]


#: 网络错误码与文案（`tests/test_web.py` 的 NodeErrorCodeTests 等）。
#:
#: 这一层把 Python 的异常翻译成 **Node 风格的错误码**。它存在的唯一理由是
#: 用户看到的文案要与原版一致——所以每一条映射和分支顺序都要有测试盯着。
_ERROR_CODE_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "错误码: 临时 DNS 故障不再映射成 EAI_AGAIN",
        WEB_UTILS,
        '            if getattr(candidate, "errno", None) == socket.EAI_AGAIN:\n'
        '                return "EAI_AGAIN"\n',
        "            if False:\n"
        '                return "EAI_AGAIN"\n',
    ),
    (
        "错误码: 域名解析失败一律算 ENOTFOUND（丢掉 EAI_AGAIN）",
        WEB_UTILS,
        '            if getattr(candidate, "errno", None) == socket.EAI_AGAIN:\n'
        '                return "EAI_AGAIN"\n'
        '            return "ENOTFOUND"',
        '            return "ENOTFOUND"',
    ),
    (
        "错误码: 不再按类型识别连接被拒",
        WEB_UTILS,
        '            (ConnectionRefusedError, "ECONNREFUSED"),\n',
        "",
    ),
    (
        "错误码: 不再解开 URLError 的 reason",
        WEB_UTILS,
        '        reason = getattr(candidate, "reason", None)\n'
        "        candidate = reason if isinstance(reason, BaseException) else candidate.__cause__\n"
        "\n    return None",
        "        candidate = None\n"
        "\n    return None",
    ),
    (
        "文案: 超时不再优先（落进「有码」分支）",
        WEB_UTILS,
        '    if is_timeout_error(error):\n'
        '        return f"request timed out after {timeout_ms}ms for {url}"\n\n'
        "    code = node_error_code(error)\n"
        "    if code is not None:\n"
        '        return f"request failed ({code}) for {url}"',
        "    code = node_error_code(error)\n"
        "    if code is not None:\n"
        '        return f"request failed ({code}) for {url}"',
    ),
    (
        "文案: 有码时不写码",
        WEB_UTILS,
        '        return f"request failed ({code}) for {url}"',
        '        return f"request failed for {url}"',
    ),
    (
        "文案: 去掉最内层 message 分支",
        WEB_UTILS,
        "    message = _innermost_message(error)\n"
        "    if message:\n"
        '        return f"{message} ({url})"',
        "    message = _innermost_message(error)\n"
        "    if False:\n"
        '        return f"{message} ({url})"',
    ),
    (
        "文案: 兜底不写 URL",
        WEB_UTILS,
        '    return f"request failed for {url}"',
        '    return "request failed"',
    ),
    (
        "重试: 判据退回只看码而不管超时",
        WEB_UTILS,
        "    if is_timeout_error(error):\n        return True\n"
        "    code = node_error_code(error)",
        "    code = node_error_code(error)",
    ),
    (
        "重试: is_timeout_error 不再解包 reason",
        WEB_UTILS,
        '        reason = getattr(candidate, "reason", None)\n'
        "        candidate = reason if isinstance(reason, BaseException) else candidate.__cause__\n"
        "    return False",
        "        candidate = None\n"
        "    return False",
    ),
    (
        "错误码: 不剥 Windows 的 WSA 前缀（整张表在 Windows 上落空）",
        WEB_UTILS,
        '            if name.startswith("WSA"):\n                name = name[3:]',
        "            if False:\n                name = name",
    ),
    (
        "错误码: 映射表少一项 EHOSTUNREACH",
        WEB_UTILS,
        '    "EHOSTUNREACH": "EHOSTUNREACH",\n',
        "",
    ),
]

MUTATIONS = [*MUTATIONS, *_ERROR_CODE_MUTATIONS]


#: 检查器**本身**的逻辑（`tests/test_checkers.py`）。
#:
#: 检查器是「其它代码是否可信」的依据。它们判断错了，结论就是错的，而且错得很
#: 安静——一个永远回「全部通过」的检查器看起来和健康的一模一样。
_CHECKER_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "检查器: 伪引用检测不再区分 docstring（注释提及算引用）",
        CHECK_CONSTANTS,
        "    if doc_ranges_by_path is None:\n"
        "        doc_ranges_by_path = docstring_ranges(sources)",
        "    doc_ranges_by_path = {path: [] for path in sources}",
    ),
    (
        "检查器: docstring 区间一律为空",
        CHECK_CONSTANTS,
        "        ranges[path] = spans\n    return ranges",
        "        ranges[path] = []\n    return ranges",
    ),
    (
        "检查器: `#` 注释不再算注释",
        CHECK_CONSTANTS,
        '            is_comment = stripped.startswith("#") or in_doc',
        "            is_comment = in_doc",
    ),
    (
        "检查器: 不再识别遮蔽的 def（只记第一个）",
        CHECK_DUPLICATES,
        "        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n"
        "            bindings.append((node.name, node.lineno, \"def\"))",
        "        if False:\n"
        "            bindings.append((node.name, node.lineno, \"def\"))",
    ),
    (
        "检查器: 把函数体内的 def 也算作模块级（误报）",
        CHECK_DUPLICATES,
        "    for node in tree.body:",
        "    for node in ast.walk(tree):",
    ),
    (
        "映射: 表里漏掉一个 TS 文件",
        FILE_MAP,
        '    "abort.ts": ("abort.py",),\n',
        "",
    ),
    (
        "映射: TUI 排除清单漏掉 ui.ts 转出桶",
        FILE_MAP,
        '    "ui.ts": "TUI 的纯转出桶（28 行 export … from \'./tui/index.js\'）",\n',
        "",
    ),
    (
        "映射: 映射到一个不存在的 .py 也能通过",
        FILE_MAP,
        '            if not (PY_ROOT / target).exists():',
        "            if False:",
    ),
]

MUTATIONS = [*MUTATIONS, *_CHECKER_MUTATIONS]


#: 四条诊断命令的输出形状（`tests/test_command_output.py`）。
#:
#: 形状走样不影响任何程序运行，所以普通测试不会发现——但用户会以为信息丢了，
#: 脚本也 grep 不到。每一条形状约束都要有测试盯着。
_DIAGNOSTIC_SHAPE_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "形状: /config-paths 退成表格（带标题与分隔线）",
        CLI_COMMANDS,
        '    return "\\n".join(\n        [\n            f"mini-code settings: {settings_path()}",',
        '    from .ui import render_separator, render_status_line\n'
        '    return "\\n".join(\n        [\n            render_status_line(label="配置路径"),\n'
        '            render_separator(),\n'
        '            f"mini-code settings: {settings_path()}",',
    ),
    (
        "形状: /config-paths 少一行（丢掉 permissions）",
        CLI_COMMANDS,
        '            f"mini-code permissions: {permissions_path()}",\n',
        "",
    ),
    (
        "形状: /config-paths 用缩进表格而不是 label: value",
        CLI_COMMANDS,
        '            f"mini-code settings: {settings_path()}",',
        '            f"  mini-code settings   {settings_path()}",',
    ),
    (
        "形状: /status 不再输出原版五行",
        CLI_COMMANDS,
        "    original = [\n"
        '        f"model: {config.model if config else \'(offline mock)\'}",',
        "    original = [\n"
        '        f"模型: {config.model if config else \'(offline mock)\'}",',
    ),
    (
        "形状: /status 的 auth 行恒为 API_KEY",
        CLI_COMMANDS,
        '            "ANTHROPIC_AUTH_TOKEN"\n'
        "            if config and config.auth_token\n"
        '            else "ANTHROPIC_API_KEY"',
        '            "ANTHROPIC_API_KEY"',
    ),
    (
        "形状: /status 的 mcp servers 计数写死 0",
        CLI_COMMANDS,
        '        f"mcp servers: {len(config.mcp_servers) if config else 0}",',
        '        f"mcp servers: 0",',
    ),
    (
        "形状: /permissions 少了 permission store 那行",
        CLI_COMMANDS,
        '        f"permission store: {permissions_path()}",\n',
        "",
    ),
    (
        "形状: /permissions 不再解析 summary（全部 none）",
        CLI_COMMANDS,
        "        for part in summary:\n"
        "            if part.startswith(prefix):\n"
        "                return part[len(prefix):].strip() or \"none\"",
        "        for part in []:\n"
        "            if part.startswith(prefix):\n"
        "                return part[len(prefix):].strip() or \"none\"",
    ),
    (
        "形状: /memory 去掉 preview 字段",
        MEMORY,
        '                    f"   preview: {entry[\'preview\']}",\n',
        "",
    ),
    (
        "形状: /memory 的 lines 不再先 trim",
        MEMORY,
        "        trimmed = item.content.strip()\n"
        "        result.append(\n"
        "            {\n"
        '                "path": relative,\n'
        '                "scope": item.scope,\n'
        '                "lines": len(trimmed.split("\\n")) if trimmed else 0,',
        "        trimmed = item.content.strip()\n"
        "        result.append(\n"
        "            {\n"
        '                "path": relative,\n'
        '                "scope": item.scope,\n'
        '                "lines": len(item.content.split("\\n")),',
    ),
    (
        "形状: /memory 的 chars 写成 trim 后的长度",
        MEMORY,
        '                "chars": len(item.content),',
        '                "chars": len(item.content.strip()),',
    ),
    (
        # 早先这里写的是「预览不再回退到 `<empty>`」——那条**无法被杀死**，
        # 因为该回退分支在原版与本实现里**都不可达**（内容 trim 后非空，第一行
        # 就不可能是空；全空白的文件在读入阶段已被判为不存在）。留一条永远存活
        # 的变异会污染「0 存活」这个信号，所以换成一条真正可观测的。
        "形状: /memory 的预览取最后一行而不是第一行",
        MEMORY,
        '                "preview": (trimmed.split("\\n")[0] if trimmed else "") or "<empty>",',
        '                "preview": (trimmed.split("\\n")[-1] if trimmed else "") or "<empty>",',
    ),
    (
        "读取: 指令文件不用 newline=\"\"（CRLF 被折成 LF，chars 变少）",
        MEMORY,
        '        with open(path, encoding="utf-8", errors="replace", newline="") as handle:',
        '        with open(path, encoding="utf-8", errors="replace") as handle:',
    ),
    (
        "读取: 用 splitlines 而不是 split(\"\\n\")（吃掉尾部换行）",
        MEMORY,
        '    for raw in content.split("\\n"):',
        "    for raw in content.splitlines():",
    ),
    (
        "形状: /memory 抬头与条目之间少一层空行",
        MEMORY,
        '    parts: list[str] = [f"Memory files loaded: {len(entries)}", ""]',
        '    parts: list[str] = [f"Memory files loaded: {len(entries)}"]',
    ),
    (
        "形状: 来源摘要缺 Claude 回退那一段",
        CONFIG,
        '            f"config: {settings_path or _settings_path()} > "\n'
        '            f"{claude_settings_path()} > process.env"',
        '            f"config: {settings_path or _settings_path()} > process.env"',
    ),
]

MUTATIONS = [*MUTATIONS, *_DIAGNOSTIC_SHAPE_MUTATIONS]


#: 文档数字核对（`tests/test_checkers.py` 的 `CheckDocsTests`）。
#:
#: 这一组防的是「检查器自己失效」：如果 `check_docs` 的句式或比对逻辑坏了，
#: 文档里的数字就会重新开始无声漂移——而那正是它存在的理由。
_CHECK_DOCS_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "文档核对: 句式放得太宽（把单模块的常量数当成全仓总数）",
        CHECK_DOCS,
        '    (r"(\\d+) 个常量，零孤儿", "constants", "个常量，零孤儿"),',
        '    (r"(\\d+) 个常量", "constants", "个常量"),',
    ),
    (
        "文档核对: 取不到实测值时不再跳过（会瞎报）",
        CHECK_DOCS,
        "        actual = stats.get(key)\n"
        "        if actual is None:\n"
        "            continue",
        "        actual = stats.get(key, 0)",
    ),
    (
        "文档核对: 生成块与实测不一致时不报",
        CHECK_DOCS,
        "    elif block not in text:\n"
        '        problems.append("生成块与实测不一致（用 --write 重写）")',
        "    elif False:\n"
        '        problems.append("生成块与实测不一致（用 --write 重写）")',
    ),
    (
        "文档核对: 手写数字的不一致被吞掉",
        CHECK_DOCS,
        "    problems.extend(hand_written_problems(text, stats))",
        "    hand_written_problems(text, stats)",
    ),
    (
        "文档核对: 生成块的标记写错（--write 会追加到末尾）",
        CHECK_DOCS,
        'BEGIN = "<!-- BEGIN GENERATED STATS -->"',
        'BEGIN = "<!-- BEGIN GENERATED STAT -->"',
    ),
]

MUTATIONS = [*MUTATIONS, *_CHECK_DOCS_MUTATIONS]


#: 网络错误的处理（`tests/test_adapters.py` 的 `NetworkErrorTests`）。
#:
#: 这一组防的是两个具体缺陷：**重试一次 DNS 失败**（白等约 9 秒，而原版根本不
#: 重试网络错误），以及**把 Python 内部包装漏给用户**
#: （`<urlopen error [Errno 11001] getaddrinfo failed>`，没有地址也没有码）。
_ADAPTER_NETWORK_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "适配器: 网络错误又变成「重试到上限」",
        ANTHROPIC_ADAPTER,
        "            except (urllib.error.URLError, OSError) as error:\n"
        "                # **不重试，翻译后直接抛出。** 见上面 docstring 里的原版对照。",
        "            except (urllib.error.URLError, OSError) as error:\n"
        "                if attempt < self._max_retries:\n"
        "                    _sleep_interruptible(retry_delay_seconds(attempt + 1, None), signal)\n"
        "                    continue\n"
        "                # **不重试，翻译后直接抛出。** 见上面 docstring 里的原版对照。",
    ),
    (
        "适配器: 网络错误不翻译（直接抛 Python 原始异常）",
        ANTHROPIC_ADAPTER,
        "                raise AnthropicNetworkError(\n"
        "                    format_web_error_message(url, error, 600_000),",
        "                raise error from error\n"
        "                raise AnthropicNetworkError(\n"
        "                    format_web_error_message(url, error, 600_000),",
    ),
    (
        "适配器: 只捕 URLError（裸 socket.timeout 逃出去）",
        ANTHROPIC_ADAPTER,
        "            except (urllib.error.URLError, OSError) as error:",
        "            except urllib.error.URLError as error:",
    ),
    (
        "适配器: 错误消息不带 URL",
        ANTHROPIC_ADAPTER,
        "                raise AnthropicNetworkError(\n"
        "                    format_web_error_message(url, error, 600_000),\n"
        "                    code=node_error_code(error),\n"
        "                    url=url,",
        "                raise AnthropicNetworkError(\n"
        "                    format_web_error_message(\"\", error, 600_000),\n"
        "                    code=node_error_code(error),\n"
        "                    url=url,",
    ),
    (
        "适配器: 超时不标记（timed_out 恒 False）",
        ANTHROPIC_ADAPTER,
        "                    timed_out=is_timeout_error(error),",
        "                    timed_out=False,",
    ),
]

MUTATIONS = [*MUTATIONS, *_ADAPTER_NETWORK_MUTATIONS]


#: 启动器脚本（`tests/test_install.py` 的 `LauncherTests`）。
#:
#: 这一组防的是一个**真的发布出去过**的缺陷：`launcher_scripts(repo_root)` 收下
#: `repo_root` 却从未使用，生成的脚本不设 `PYTHONPATH`。从源码使用时一执行就是
#: `ModuleNotFoundError: No module named 'minicode'`——而用户会以为「没装」。
_LAUNCHER_MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "启动器: 不设 PYTHONPATH（从源码运行时找不到包）",
        INSTALL,
        '        f\'PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\\n\'\n'
        '        "export PYTHONPATH\\n"',
        '        f\'PYTHONPATH=""\\n\'\n'
        '        "export PYTHONPATH\\n"',
    ),
    (
        "启动器: cmd 版不设 PYTHONPATH",
        INSTALL,
        '        f\'set "PYTHONPATH={src_dir};%PYTHONPATH%"\\r\\n\'',
        '        "set \\"PYTHONPATH=%PYTHONPATH%\\"\\r\\n"',
    ),
    (
        "启动器: ps1 版不设 PYTHONPATH",
        INSTALL,
        '        f\'$env:PYTHONPATH = "{src_dir};" + $env:PYTHONPATH\\n\'',
        '        "$env:PYTHONPATH = $env:PYTHONPATH\\n"',
    ),
    (
        "启动器: src 排在已有 PYTHONPATH 之后（已安装的包先命中）",
        INSTALL,
        '        f\'PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\\n\'',
        '        f\'PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}{src_dir}"\\n\'',
    ),
    (
        "启动器: posix 版忘记 export（子进程看不到 PYTHONPATH）",
        INSTALL,
        '        "export PYTHONPATH\\n"',
        '        ": PYTHONPATH\\n"',
    ),
    (
        "启动器: ps1 不传退出码（失败看起来像成功）",
        INSTALL,
        '        "$code = $LASTEXITCODE\\n"',
        '        "$code = 0\\n"',
    ),
]

MUTATIONS = [*MUTATIONS, *_LAUNCHER_MUTATIONS]


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


#: 断点文件：`{变异标签: "killed" | "survived"}`。
#:
#: ## 为什么需要它
#:
#: 一轮全量约 3 小时。它被打断过**两次**——一次是工具调用被 abort（把正在跑基线
#: 的测试子进程一起杀了），一次是会话跨夜结束。两次都得从头再跑三小时，而第二次
#: 甚至留下了**已应用的变异**（`local_tool_shortcuts.py` 的护栏被删掉）。
#:
#: 有了断点文件，重跑会跳过已完成的条目，于是「被打断」的代价变成几分钟。
PROGRESS_PATH = os.path.join(REPO, "tests", ".mutation_progress.json")


def home_changes(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """两份指纹之间**变了**的文件名（新增、删除、内容或 mtime 变化）。

    抽成独立函数是为了可测：abort 那条路径要跑完整一轮变异才触发，测不到；
    而「比较逻辑本身对不对」是这条防线唯一会出错的地方。
    """
    changed = set(after) ^ set(before)
    changed |= {
        name
        for name in set(after) & set(before)
        if after[name] != before[name]
    }
    return sorted(changed)


def _real_home_fingerprint() -> dict[str, tuple[int, int]]:
    """真实 `~/.mini-code` 的指纹：`{文件名: (大小, mtime_ns)}`。

    ## 为什么需要它（一次真实事故）

    变异里有这一条：

        "配置: minicode_home 忽略 MINICODE_HOME（测试隔离与运行时改配置失效）"
        `return _home_dir()`  →  `return Path.home() / ".mini-code"`

    名字里写着「测试隔离失效」，但**没有人想到它的破坏性后果**：在那 20 秒里，
    所有写配置的测试都落到了**用户的真实家目录**。

    `tests/test_install.py` 的答案是 `["m", "https://x", "t"]`，于是真实的
    `~/.mini-code/settings.json` 被写成了 `model: "m"`、`ANTHROPIC_BASE_URL:
    "https://x"`、`ANTHROPIC_AUTH_TOKEN: "t"`——用户的真实配置被测试夹具覆盖，
    而之后每次运行都会因为 `https://x` 解析不了而报 `getaddrinfo failed`。

    **变异检查的目的只是「让测试变红」，不是「让测试去改用户的机器」。**
    这个指纹就是那道闸：变异跑完发现真实家目录变了，立即中止，而不是继续跑
    剩下的两小时。
    """
    home = Path(os.path.expanduser("~")) / ".mini-code"
    if not home.is_dir():
        return {}
    fingerprint: dict[str, tuple[int, int]] = {}
    for path in home.rglob("*"):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint[str(path.relative_to(home))] = (stat.st_size, stat.st_mtime_ns)
    return fingerprint


def load_progress() -> dict[str, str]:
    try:
        with open(PROGRESS_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def save_progress(progress: dict[str, str]) -> None:
    """写断点。**失败不影响检查**——它只是加速手段，不是正确性前提。"""
    try:
        with open(PROGRESS_PATH, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(progress, handle, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    reset = "--reset" in argv

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

    progress = {} if reset else load_progress()
    if reset:
        try:
            os.remove(PROGRESS_PATH)
        except OSError:
            pass

    # 断点只对**当前清单里仍存在**的标签有效：清单改过之后，旧标签会失效。
    labels = {label for label, _path, _old, _new in MUTATIONS}
    done = {label: verdict for label, verdict in progress.items() if label in labels}
    remaining = [item for item in MUTATIONS if item[0] not in done]

    if done:
        print(
            f"从断点继续：已完成 {len(done)} 条，剩余 {len(remaining)} 条"
            "（`--reset` 从头跑）"
        )

    # 只有还有剩余时才需要基线；全部做完就直接汇总。
    if remaining:
        baseline, baseline_timeout = run_suite()
        print(f"baseline: {'PASS' if baseline else 'FAIL'}")
        if not baseline:
            print("基线不通过，无法做变异检查。" + ("（基线超时）" if baseline_timeout else ""))
            return 1

    survived: list[str] = [label for label, verdict in done.items() if verdict != "killed"]

    for label, path, old, new in remaining:
        original = open(path, encoding="utf-8").read()
        if old not in original:
            print(f"  SKIP  {label}  (未找到待变异的代码片段)")
            survived.append(label + " [片段不匹配]")
            done[label] = "survived"
            save_progress(done)
            continue

        mutated = original.replace(old, new, 1)
        if mutated == original:
            # 变异没有改变任何字节 —— 检查会得出「测试无效」的错误结论。
            # 真实发生过：目标片段不带行首缩进，先匹配到了另一个分支，
            # 替换后那一段与原来**完全等价**。
            print(f"  ERROR {label}  (变异是空操作，检查配置有误)")
            survived.append(label + " [空操作变异]")
            done[label] = "survived"
            save_progress(done)
            continue

        try:
            # 先留备份，再改源码——顺序不能反，否则「刚改完就被杀」
            # 会留下没有备份的变异。
            with open(backup_path(path), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(original)
            open(path, "w", encoding="utf-8", newline="\n").write(mutated)
            home_before = _real_home_fingerprint()
            passed, timed_out = run_suite()
            killed = not passed
            home_after = _real_home_fingerprint()
        finally:
            open(path, "w", encoding="utf-8", newline="\n").write(original)
            try:
                os.remove(backup_path(path))
            except OSError:
                pass

        # **真实家目录被改过就立即中止。** 见 `_real_home_fingerprint` 的说明：
        # 那条「minicode_home 忽略 MINICODE_HOME」的变异会让写配置的测试落到
        # 用户的真实 `~/.mini-code` 上，把 `settings.json` 覆盖成测试夹具的值。
        # 这不是「变异存活」，是沙箱逃逸——继续跑剩下几百条只会扩大破坏。
        if home_after != home_before:
            changed = home_changes(home_before, home_after)
            print()
            print("!! 中止：这条变异改动了**真实家目录** ~/.mini-code")
            print(f"!! 变异: {label}")
            print(f"!! 被改动的文件: {', '.join(changed) or '（新增/删除）'}")
            print()
            print("   这不是「变异存活」，是**沙箱逃逸**：测试本该只写临时目录。")
            print("   继续跑剩下的条目只会扩大破坏。请先：")
            print("     1. 从 tests/_tmp/real-home-backup/ 恢复被覆盖的配置")
            print("     2. 修好隔离（或删掉这条变异）后再跑")
            return 2

        if timed_out:
            print(f"  KILLED  {label}  (超时——测试发现该变异会阻塞)")
        else:
            print(f"  {'KILLED' if killed else 'SURVIVED'}  {label}")
        if not killed:
            survived.append(label)
        done[label] = "killed" if killed else "survived"
        save_progress(done)

    print()
    total = len(MUTATIONS)
    print(f"变异检查：{total - len(survived)}/{total} 被测试杀死")
    if survived:
        print("存活（说明对应测试形同虚设）：")
        for item in survived:
            print("  -", item)
        return 1
    # 全绿时清掉断点，下次从头来（清单可能已经变了）。
    try:
        os.remove(PROGRESS_PATH)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    force_utf8_output()
    raise SystemExit(main(sys.argv[1:]))