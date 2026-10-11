"""运行时配置。

配置来源与优先级（后者覆盖前者）：

    **进程环境变量  <  settings.json 的 env**  <  显式传入

配置文件**优先于**进程环境变量，这一点刻意如此。理由：机器上往往长期以来
就存在 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 等变量（用户为别的工具
配的）。若让它们压过本工具的 settings.json，那么写在这个文件里的配置会被
静默作废——「专门的配置文件」也就失去了意义，而且失效时毫无提示。

模型解析顺序：`MINICODE_MODEL` > `settings.model` > `ANTHROPIC_MODEL`。
`MINICODE_*` 是本工具专有前缀，因此它始终优先（见下方 `_resolve`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def _home_dir() -> Path:
    override = os.environ.get("MINICODE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".mini-code"


# ---------------------------------------------------------------------------
# 路径解析：动态 vs 导入期
#
# 这是一个反复踩到的坑，所以显式分成两套：
#
#   `minicode_home()` / `projects_dir()` …  **每次调用重新求值**。
#       运行时代码一律用这些。原因是模块级常量在 import 时就被固定了，
#       之后再设 `MINICODE_HOME` 完全无效——测试隔离与运行时改配置都会静默失效。
#
#   `MINICODE_HOME` / `PROJECTS_DIR` … 导入期快照，**仅供展示**（如 /status 打印路径）。
#       不要用它去读写文件。
# ---------------------------------------------------------------------------


def minicode_home() -> Path:
    """当前生效的家目录。每次调用重新读取环境变量。"""
    return _home_dir()


def projects_dir() -> Path:
    return minicode_home() / "projects"


def tool_results_dir() -> Path:
    return minicode_home() / "tool-results"


def settings_path() -> Path:
    return minicode_home() / "settings.json"


def permissions_path() -> Path:
    return minicode_home() / "permissions.json"


def history_path() -> Path:
    return minicode_home() / "history.jsonl"


def mcp_path() -> Path:
    return minicode_home() / "mcp.json"


def mcp_tokens_path() -> Path:
    return minicode_home() / "mcp-tokens.json"


#: 导入期快照，仅用于展示。
MINICODE_HOME = _home_dir()
SETTINGS_PATH = MINICODE_HOME / "settings.json"
HISTORY_PATH = MINICODE_HOME / "history.jsonl"
PERMISSIONS_PATH = MINICODE_HOME / "permissions.json"
MCP_PATH = MINICODE_HOME / "mcp.json"
MCP_TOKENS_PATH = MINICODE_HOME / "mcp-tokens.json"
PROJECTS_DIR = MINICODE_HOME / "projects"
TOOL_RESULTS_DIR = MINICODE_HOME / "tool-results"

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: 未知模型的兜底输出上限。
DEFAULT_MAX_OUTPUT_TOKENS = 8192


# ---------------------------------------------------------------------------
# 输出 token 上限
# ---------------------------------------------------------------------------

#: 模型名子串 -> (默认值, 上限)。按顺序匹配，先命中者生效。
#: 上限用于夹住用户配置值，避免配出一个服务端必然拒绝的数字。
MAX_OUTPUT_TOKENS_RULES: tuple[tuple[str, int, int], ...] = (
    ("claude-opus-4", 16_000, 32_000),
    ("claude-sonnet-4", 16_000, 64_000),
    ("claude-3-7", 16_000, 64_000),
    ("claude-3-5", 8_192, 8_192),
    ("claude-3", 4_096, 8_192),
    ("gpt-5", 16_000, 128_000),
    ("gpt-4.1", 16_000, 32_768),
    ("gpt-4o", 8_192, 16_384),
    ("deepseek", 8_192, 8_192),
    # 阿里云百炼的 Qwen 系列：思考与正文共享 max_tokens，
    # 因此给足预算，避免长回答被截断。
    ("qwen", 16_000, 32_768),
)


def resolve_max_output_tokens(model: str, configured: int | None = None) -> int:
    """决定本次请求的 max_tokens。"""
    default, upper = DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS
    lowered = model.lower()
    for needle, rule_default, rule_upper in MAX_OUTPUT_TOKENS_RULES:
        if needle in lowered:
            default, upper = rule_default, rule_upper
            break

    if configured is None or configured <= 0:
        return default
    return min(configured, upper)


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------


@dataclass
class RuntimeConfig:
    model: str
    base_url: str = DEFAULT_BASE_URL
    auth_token: str | None = None
    api_key: str | None = None
    max_output_tokens: int | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    #: 扩展思考开关：`"enabled"` / `"disabled"` / None（不发该字段）。
    #:
    #: 为什么需要它：Anthropic 官方模型**默认不思考**，因此不发这个字段没问题；
    #: 但阿里云百炼的 `qwen3.8-max` 系列**默认开启**思考，思考 token 与
    #: 正文共享 `max_tokens` 预算。预算小的时候思考会把它吃光，
    #: 表现为「模型返回空内容」——一个非常难诊断的症状。
    #:
    #: 默认 None（不发），以保持与 Anthropic 官方端点的行为完全一致。
    thinking: str | None = None
    #: 配置来源摘要，用于 `/status` 展示。
    source_summary: str = ""

    def headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        elif self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def messages_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/messages"

    def thinking_payload(self) -> dict[str, str] | None:
        """转成请求体的 `thinking` 字段；未配置或不合法时返回 None。"""
        if self.thinking in ("enabled", "disabled"):
            return {"type": self.thinking}
        return None


class ConfigError(Exception):
    """配置缺失或不合法。"""


def read_settings_file(path: Path | None = None) -> dict[str, Any]:
    target = path or SETTINGS_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigError(f"无法读取 {target}：{error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigError(f"{target} 不是合法 JSON：{error}") from error
    return parsed if isinstance(parsed, dict) else {}


def merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """浅合并，但 `env` 与每个 MCP server 的 `env` 是**深合并**。

    深合并 env 是必要的：用户往往只在项目级配置里补一个变量，
    而不想重写整份 env。
    """
    merged = {**base, **override}

    merged_env = {**(base.get("env") or {}), **(override.get("env") or {})}
    if merged_env:
        merged["env"] = merged_env

    base_servers = base.get("mcpServers") or {}
    override_servers = override.get("mcpServers") or {}
    if base_servers or override_servers:
        servers = {**base_servers}
        for name, server in override_servers.items():
            if isinstance(server, dict) and isinstance(servers.get(name), dict):
                servers[name] = {
                    **servers[name],
                    **server,
                    "env": {
                        **(servers[name].get("env") or {}),
                        **(server.get("env") or {}),
                    },
                }
            else:
                servers[name] = server
        merged["mcpServers"] = servers

    return merged


def save_settings(updates: dict[str, Any], path: Path | None = None) -> Path:
    """读-合并-重写。只覆盖本次给出的 key。"""
    target = path or SETTINGS_PATH
    existing = read_settings_file(target)
    merged = merge_settings(existing, updates)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def resolve_env_value(
    key: str,
    settings: dict[str, Any],
    environ: dict[str, str] | None = None,
) -> str:
    """取一个配置值：settings.json 的 env 优先，其后是进程环境变量。

    `MINICODE_*` 前缀的变量是**本工具专有**的，不会被别的工具设置，
    因此始终以进程环境为准（便于临时覆盖，例如 `MINICODE_MODEL=x minicode`）。
    """
    source = environ if environ is not None else os.environ
    from_settings = (settings.get("env") or {}).get(key)

    if key.startswith("MINICODE_"):
        return str(source.get(key) or from_settings or "").strip()
    return str(from_settings or source.get(key) or "").strip()


def load_runtime_config(settings_path: Path | None = None) -> RuntimeConfig:
    """装载一次运行所需的全部配置。缺关键项就抛 ConfigError。"""
    settings = read_settings_file(settings_path)

    def value(key: str) -> str:
        return resolve_env_value(key, settings)

    model = (
        value("MINICODE_MODEL")
        or str(settings.get("model") or "").strip()
        or value("ANTHROPIC_MODEL")
    )
    if not model:
        raise ConfigError(
            "未配置模型。请设置 MINICODE_MODEL 环境变量，"
            f"或在 {settings_path or SETTINGS_PATH} 中写入 \"model\"。"
        )

    base_url = value("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL

    # 凭据必须**成对**决定，不能逐个回退。
    #
    # 否则会出现「端点取自 settings，密钥取自环境」的混搭：用户明明在配置
    # 文件里写好了完整的 key/URL，环境里遗留的 ANTHROPIC_AUTH_TOKEN（可能是
    # 另一个服务商的）却把它顶掉了。这种混搭既连不通，又可能把不该外发的
    # 密钥送到别的端点。
    settings_env = settings.get("env") or {}
    settings_has_credentials = bool(
        settings_env.get("ANTHROPIC_AUTH_TOKEN") or settings_env.get("ANTHROPIC_API_KEY")
    )
    if settings_has_credentials:
        auth_token = str(settings_env.get("ANTHROPIC_AUTH_TOKEN") or "").strip() or None
        api_key = str(settings_env.get("ANTHROPIC_API_KEY") or "").strip() or None
    else:
        auth_token = value("ANTHROPIC_AUTH_TOKEN") or None
        api_key = value("ANTHROPIC_API_KEY") or None

    if not auth_token and not api_key:
        raise ConfigError(
            "未配置凭据。请设置 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）。"
        )

    raw_max = value("MINICODE_MAX_OUTPUT_TOKENS") or settings.get("maxOutputTokens")
    max_output_tokens: int | None = None
    if raw_max is not None:
        try:
            parsed = int(raw_max)
            if parsed > 0:
                max_output_tokens = parsed
        except (TypeError, ValueError):
            max_output_tokens = None

    # 思考开关：环境变量优先于 settings。
    raw_thinking = (
        value("MINICODE_THINKING") or str(settings.get("thinking") or "")
    ).strip().lower()
    thinking: str | None = raw_thinking if raw_thinking in ("enabled", "disabled") else None

    return RuntimeConfig(
        model=model,
        base_url=base_url,
        auth_token=auth_token,
        api_key=api_key,
        max_output_tokens=max_output_tokens,
        mcp_servers=load_mcp_servers(cwd=os.getcwd(), settings=settings),
        thinking=thinking,
        source_summary=f"settings: {settings_path or SETTINGS_PATH} > process.env",
    )


def read_mcp_config_file(path: Path) -> dict[str, Any]:
    """读一个 `mcp.json` 形态的文件：`{"mcpServers": {...}}`。

    缺失、损坏、或结构不对 → 返回 `{}`（不抛）。MCP 配置坏掉不该让程序起不来。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    servers = parsed.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def load_mcp_servers(
    cwd: str | None = None, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """汇总全部 MCP 服务器配置。

    优先级（低 → 高）：

        `<MINICODE_HOME>/mcp.json`    用户级
        `<cwd>/.mcp.json`             项目级
        `settings.json` 的 mcpServers 最高

    之所以同时支持三种来源：`mcp.json` 是 MCP 生态的惯例文件名，
    而 `settings.json` 是本工具的主配置。两者都要能用，且同名时后者胜。
    """
    merged: dict[str, Any] = {}

    for source in (minicode_home() / "mcp.json",):
        merged.update(read_mcp_config_file(source))

    if cwd:
        merged.update(read_mcp_config_file(Path(cwd) / ".mcp.json"))

    from_settings = (settings or {}).get("mcpServers")
    if isinstance(from_settings, dict):
        merged.update(from_settings)

    return merged


def try_load_runtime_config(settings_path: Path | None = None) -> RuntimeConfig | None:
    """宽容版：配置不全时返回 None，让宿主降级到离线模式而不是崩溃。"""
    try:
        return load_runtime_config(settings_path)
    except ConfigError:
        return None
