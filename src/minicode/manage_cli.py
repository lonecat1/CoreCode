"""`minicode mcp ...` 与 `minicode skills ...` 两个管理子命令。

对应原版 `MiniCode/src/manage-cli.ts`（290 行）。

## 它是**独立入口**，不是 REPL 里的斜杠命令

原版 `index.ts` 在启动 REPL **之前**调用 `maybeHandleManagementCommand`：
返回 `true` 就直接退出。因此这是命令行层面的子命令：

    minicode mcp list [--project]
    minicode mcp add <name> [--project] [--protocol ...] [--url ...] [-- <command> [args...]]
    minicode mcp login <name> --token <token>
    minicode mcp logout <name>
    minicode mcp remove <name> [--project]

    minicode skills list
    minicode skills add <path> [--name <name>] [--project]
    minicode skills remove <name> [--project]

**这一点容易漏掉**：斜杠命令（`/mcp`、`/skills`）只能**看**，不能改配置；
要改配置必须走子命令。两者用途不同，都要有。

## `--scope` 的语义

默认 `user`（写 `~/.mini-code/mcp.json`），`--project` 则写 `<cwd>/.mcp.json`。
项目级配置通常要提交给团队，用户级配置只对自己生效。

## `--` 分隔符

`minicode mcp add fs -- npx -y @modelcontextprotocol/server-filesystem /tmp`
里 `--` 之后是**要启动的命令**。没有它，`-y` 之类的参数会被当成
`minicode` 自己的选项解析——这是这类 CLI 的标准做法。
"""

from __future__ import annotations

import sys
from typing import Any

from .config import (
    get_mcp_config_path,
    load_scoped_mcp_servers,
    mcp_tokens_path,
    read_mcp_tokens_file,
    save_mcp_tokens_file,
    save_scoped_mcp_servers,
)

__all__ = [
    "handle_mcp_command",
    "handle_skills_command",
    "maybe_handle_management_command",
    "parse_env_pairs",
    "parse_scope",
    "print_usage",
    "take_option",
    "take_repeat_option",
]

#: 合法的 MCP 协议。`auto` 让 bridge 自己探测。
VALID_PROTOCOLS = frozenset(
    {"auto", "content-length", "newline-json", "streamable-http"}
)

USAGE = """minicode management commands

minicode mcp list [--project]
minicode mcp add <name> [--project] [--protocol <auto|content-length|newline-json|streamable-http>] [--url <endpoint>] [--header KEY=VALUE ...] [--env KEY=VALUE ...] [-- <command> [args...]]
minicode mcp login <name> --token <bearer-token>
minicode mcp logout <name>
minicode mcp remove <name> [--project]

minicode skills list
minicode skills add <path-to-skill-or-dir> [--name <name>] [--project]
minicode skills remove <name> [--project]"""


class ManageError(Exception):
    """管理命令的用法或执行错误。"""


def print_usage(stream: Any = None) -> None:
    print(USAGE, file=stream or sys.stdout)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def parse_scope(args: list[str]) -> tuple[str, list[str]]:
    """取出 `--project`，返回 `(作用域, 其余参数)`。

    **就地移除**：原版用 `splice`，这里返回新列表。剩下的参数交给具体子命令，
    它们不必再关心作用域从哪来。
    """
    rest = list(args)
    if "--project" in rest:
        rest.remove("--project")
        return "project", rest
    return "user", rest


def take_option(args: list[str], name: str) -> str | None:
    """取一个 `--name value` 形式的选项，**并从列表里移除**。

    就地移除让「未知参数检查」变得可靠：所有已知选项被摘掉后，剩下的就是
    真正多余的参数（通常是用户拼错了选项名）。
    """
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args) or args[index + 1] == "":
        raise ManageError(f"Missing value for {name}")
    value = args[index + 1]
    del args[index : index + 2]
    return value


def take_repeat_option(args: list[str], name: str) -> list[str]:
    """取可重复的选项（`--env A=1 --env B=2`）。"""
    values: list[str] = []
    while name in args:
        index = args.index(name)
        if index + 1 >= len(args) or args[index + 1] == "":
            raise ManageError(f"Missing value for {name}")
        values.append(args[index + 1])
        del args[index : index + 2]
    return values


def parse_env_pairs(values: list[str]) -> dict[str, str]:
    """把 `KEY=VALUE` 列表解析成字典。

    **只在第一个 `=` 处分割**：值里可能含 `=`（连接串、base64），
    按所有 `=` 分割会把值截断——而且是静默的。
    """
    result: dict[str, str] = {}
    for entry in values:
        separator = entry.find("=")
        if separator == -1:
            raise ManageError(f"Invalid --env value: {entry}")
        key = entry[:separator].strip()
        if not key:
            raise ManageError(f"Invalid --env value: {entry}")
        result[key] = entry[separator + 1 :]
    return result


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


def handle_mcp_command(cwd: str, args: list[str]) -> bool:
    if not args:
        print_usage()
        return True

    subcommand, rest_args = args[0], args[1:]
    scope, rest = parse_scope(rest_args)

    if subcommand == "list":
        return _mcp_list(cwd, scope)

    if subcommand == "add":
        return _mcp_add(cwd, scope, rest)

    if subcommand == "remove":
        return _mcp_remove(cwd, scope, rest)

    if subcommand == "login":
        return _mcp_login(rest)

    if subcommand == "logout":
        return _mcp_logout(rest)

    print_usage()
    return True


def _mcp_list(cwd: str, scope: str) -> bool:
    servers = load_scoped_mcp_servers(scope, cwd)
    path = get_mcp_config_path(scope, cwd)
    if not servers:
        print(f"No MCP servers configured in {path}.")
        return True

    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        endpoint = str(server.get("url") or "").strip()
        if not endpoint:
            parts = [str(server.get("command") or "")]
            raw_args = server.get("args")
            if isinstance(raw_args, list):
                parts.extend(str(item) for item in raw_args)
            endpoint = " ".join(parts).strip()
        protocol = server.get("protocol")
        suffix = f" protocol={protocol}" if protocol else ""
        print(f"{name}: {endpoint}{suffix}".strip())
    return True


def _mcp_add(cwd: str, scope: str, rest: list[str]) -> bool:
    # `--` 之后是要启动的命令，不是本命令的选项。
    if "--" in rest:
        separator = rest.index("--")
        head = rest[:separator]
        command_parts = rest[separator + 1 :]
    else:
        head = list(rest)
        command_parts = []

    if not head:
        raise ManageError("Missing MCP server name.")
    name = head.pop(0)

    protocol = take_option(head, "--protocol")
    url = (take_option(head, "--url") or "").strip()
    env = parse_env_pairs(take_repeat_option(head, "--env"))
    headers = parse_env_pairs(take_repeat_option(head, "--header"))

    if head:
        raise ManageError(f"Unknown arguments: {' '.join(head)}")

    if protocol is not None and protocol not in VALID_PROTOCOLS:
        raise ManageError(
            f"Unknown protocol: {protocol}. "
            f"Expected one of {', '.join(sorted(VALID_PROTOCOLS))}."
        )

    has_url = bool(url)
    has_command = bool(command_parts)
    if has_url and has_command:
        raise ManageError("Cannot set both --url and local command. Choose one.")
    if not has_url and not has_command:
        raise ManageError("Missing MCP command or --url.")
    if protocol == "streamable-http" and not has_url:
        # 这个组合一定起不来，提前挡住比让它连不上再排查好。
        raise ManageError("Protocol streamable-http requires --url.")

    command = command_parts[0] if command_parts else ""
    command_args = command_parts[1:] if has_command else []

    existing = load_scoped_mcp_servers(scope, cwd)
    entry: dict[str, Any] = {"command": command}
    # 可选字段为空时**省略**，而不是写 `null` / `[]`：配置文件是给人看的，
    # 一堆空字段会掩盖真正有意义的那几个。
    #
    # 例外是 `command`：远端型服务器（只给 `--url`）也会写出 `"command": ""`。
    # 这看着冗余，但**与原版一致**，而且无害——transport 的选择是「有 `url`
    # 就走 HTTP」（`bridge._create_client` 与原版 `mcp.ts:1010` 都是这个判断），
    # 空 command 永远走不到 stdio 分支。
    if has_command and command_args:
        entry["args"] = command_args
    if env:
        entry["env"] = env
    if has_url:
        entry["url"] = url
    if headers:
        entry["headers"] = headers
    if protocol:
        entry["protocol"] = protocol

    existing[name] = entry
    save_scoped_mcp_servers(scope, existing, cwd)
    print(f"Added MCP server {name} to {get_mcp_config_path(scope, cwd)}")
    return True


def _mcp_remove(cwd: str, scope: str, rest: list[str]) -> bool:
    if not rest:
        raise ManageError("Missing MCP server name.")
    name = rest[0]
    existing = load_scoped_mcp_servers(scope, cwd)
    path = get_mcp_config_path(scope, cwd)
    if name not in existing:
        # 删一个不存在的**不算错误**：用户的意图（「让它不存在」）已经满足。
        print(f"MCP server {name} not found in {path}")
        return True
    del existing[name]
    save_scoped_mcp_servers(scope, existing, cwd)
    print(f"Removed MCP server {name} from {path}")
    return True


def _mcp_login(rest: list[str]) -> bool:
    args = list(rest)
    if not args:
        raise ManageError("Missing MCP server name.")
    name = args[0]
    token = (take_option(args, "--token") or "").strip()
    if not token:
        raise ManageError("Missing --token value.")
    if len(args) > 1:
        raise ManageError(f"Unknown arguments: {' '.join(args[1:])}")

    tokens = read_mcp_tokens_file()
    tokens[name] = token
    save_mcp_tokens_file(tokens)
    print(f"Stored MCP token for {name} in {mcp_tokens_path()}")
    return True


def _mcp_logout(rest: list[str]) -> bool:
    if not rest:
        raise ManageError("Missing MCP server name.")
    name = rest[0]
    tokens = read_mcp_tokens_file()
    if name not in tokens:
        print(f"No token found for {name} in {mcp_tokens_path()}")
        return True
    del tokens[name]
    save_mcp_tokens_file(tokens)
    print(f"Removed MCP token for {name} from {mcp_tokens_path()}")
    return True


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def handle_skills_command(cwd: str, args: list[str]) -> bool:
    if not args:
        print_usage()
        return True

    subcommand, rest_args = args[0], args[1:]
    scope, rest = parse_scope(rest_args)

    if subcommand == "list":
        return _skills_list(cwd)

    if subcommand == "add":
        return _skills_add(cwd, scope, rest)

    if subcommand == "remove":
        return _skills_remove(cwd, scope, rest)

    print_usage()
    return True


def _skills_list(cwd: str) -> bool:
    from .skills import discover_skills

    skills = discover_skills(cwd)
    if not skills:
        print("No skills discovered.")
        return True
    for skill in skills:
        print(f"{skill.name}: {skill.description} ({skill.path})")
    return True


def _skills_add(cwd: str, scope: str, rest: list[str]) -> bool:
    from .skills import install_skill

    args = list(rest)
    if not args:
        raise ManageError("Missing skill source path.")
    source_path = args.pop(0)
    name = take_option(args, "--name")

    installed_name, target_path = install_skill(
        cwd, source_path, name=name, scope=scope
    )
    print(f"Installed skill {installed_name} at {target_path}")
    return True


def _skills_remove(cwd: str, scope: str, rest: list[str]) -> bool:
    from .skills import remove_managed_skill

    if not rest:
        raise ManageError("Missing skill name.")
    name = rest[0]
    removed, target_path = remove_managed_skill(cwd, name, scope=scope)
    if not removed:
        print(f"Skill {name} not found at {target_path}")
        return True
    print(f"Removed skill {name} from {target_path}")
    return True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def maybe_handle_management_command(cwd: str, argv: list[str]) -> bool:
    """如果 `argv` 是管理子命令就处理它，返回是否已处理。

    返回 `True` 时调用方应当**直接退出**，不再进入 REPL。

    ## 为什么单独一个函数

    它要能回答「这个 argv 是不是我的」而**不产生副作用**——返回 `False`
    时必须什么都没做，否则普通启动路径会被悄悄影响（比如打印了用法）。

    错误处理放在这一层：子命令内部只负责抛 `ManageError`，这里统一转成
    退出码。这样每个子命令不必各自处理 I/O 与退出。
    """
    category = argv[0] if argv else ""
    if not category:
        return False

    if category in ("help", "--help", "-h"):
        print_usage()
        return True

    if category not in ("mcp", "skills"):
        return False

    handler = handle_mcp_command if category == "mcp" else handle_skills_command
    try:
        handler(cwd, argv[1:])
    except ManageError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    return True
