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
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 环境变量：**两套拼写都认**
#
# 原版用的是 `MINI_CODE_*`（MINI 与 CODE 之间**有下划线**）：
#
#     config.ts:36        process.env.MINI_CODE_HOME
#     index.ts            process.env.MINI_CODE_MODEL_MODE
#     anthropic-adapter   process.env.MINI_CODE_MAX_RETRIES
#     等
#
# 本实现历史上的拼写是 `MINICODE_*`（没有下划线），测试与文档都按它写。
#
# 一个从原版过来的用户设 `MINI_CODE_HOME` 却发现程序仍写 `~/.mini-code`——
# 而且**不报错**。这类「配置被静默忽略」是最难自查的。
#
# 所以两个都认，**原版拼写优先**：它是上游的契约，别名只是照顾既有测试。
# ---------------------------------------------------------------------------


def env_value(suffix: str) -> str | None:
    """读取 `<PREFIX>_<suffix>`，两套前缀都认，原版拼写优先。

    `suffix` 传大写名字，如 `"HOME"` / `"MAX_RETRIES"`。
    空字符串视为未设置（和原版的 `process.env.X` 假值判断一致）。
    """
    for prefix in ("MINI_CODE", "MINICODE"):
        raw = os.environ.get(f"{prefix}_{suffix}")
        if raw:
            return raw
    return None


def _home_dir() -> Path:
    override = env_value("HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".mini-code"


# ---------------------------------------------------------------------------
# 路径解析：**只有动态访问器**
#
# 这是一个反复踩到的坑。模块级常量在 `import` 时就被固定了，之后再设
# `MINICODE_HOME` 完全无效——测试隔离与运行时改配置都会**静默失效**。
#
# 曾经这里还有一套「导入期快照」（`SETTINGS_PATH` / `PROJECTS_DIR` /
# `MCP_PATH` …），注释说是「仅供展示，如 /status 打印路径」。核对常量表时
# 发现：**它们零引用**——`/config-paths` 与 `/permissions` 用的都是下面这些
# 动态函数，没有任何地方展示过快照。
#
# 于是整块删掉。理由不只是「死代码」：
#   - 那句「仅供展示」的注释是**错的**，而错注释比没有注释更糟——它让人
#     相信这些名字有正当用途，从而在需要展示路径时**正好选中它们**，
#     重新引入「显示了一个并不生效的路径」这个 bug。
#   - 删掉之后，「拿导入期快照做 I/O」这类缺陷在结构上不可能再出现。
#
# 新增路径时请一律加一个 `xxx_path()` 动态函数。
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


#: `settings_path()` 的别名，供**参数同名**的函数内部使用。
#:
#: `load_runtime_config(settings_path=...)` 的参数名与这个函数同名，函数体内
#: 直接写 `settings_path()` 会得到 `'WindowsPath' object is not callable`
#: ——参数遮蔽了模块级函数。这个别名让那种地方仍能拿到动态路径。
_settings_path = settings_path


def permissions_path() -> Path:
    return minicode_home() / "permissions.json"


def history_path() -> Path:
    return minicode_home() / "history.jsonl"


def mcp_path() -> Path:
    return minicode_home() / "mcp.json"


def mcp_tokens_path() -> Path:
    return minicode_home() / "mcp-tokens.json"


def claude_settings_path() -> Path:
    """Claude Code 的配置文件路径，作为**最低优先级**的配置来源。

    对应原版 `config.ts:45` 的 `CLAUDE_SETTINGS_PATH`。

    ## 为什么要有它

    原版的 `loadEffectiveSettings()` 会读三处配置并按优先级合并：

        ~/.claude/settings.json     最低（兼容 Claude Code）
        ~/.mini-code/mcp.json       MCP 服务器
        ./.mcp.json                 项目级 MCP 服务器
        ~/.mini-code/settings.json  最高

    少了第一项，从 Claude Code 迁移过来的用户会发现**他已经配好的东西全部
    失效**——模型名、端点、密钥都得重新写一遍，而程序不会给出任何提示。

    刻意**不跟随 `MINICODE_HOME`**：它是 Claude Code 的目录，不是本项目的
    家目录。把它重定向到本项目家目录下会读到自己的配置，形成自我引用。
    """
    return Path.home() / ".claude" / "settings.json"


#: Claude Code 的配置文件路径快照。
#:
#: **这是唯一保留的快照**，因为它与 `MINICODE_HOME` 无关——它恒等于
#: `Path.home() / ".claude" / "settings.json"`，不会因为环境变量变化而失效。
#: 测试用它断言 `claude_settings_path()` 没有跟随 `MINICODE_HOME`。
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

DEFAULT_BASE_URL = "https://api.anthropic.com"


# ---------------------------------------------------------------------------
# 输出 token 上限
# ---------------------------------------------------------------------------

#: 未知模型的兜底 `(默认值, 上限)`。
#: 对应原版 `utils/context.ts:11-14` 的 `UNKNOWN_MODEL_MAX_OUTPUT_TOKENS`。
UNKNOWN_MODEL_MAX_OUTPUT_TOKENS: tuple[int, int] = (32_000, 64_000)

#: 模型名模式 -> `(默认值, 上限)`。**按顺序匹配，先命中者生效。**
#:
#: 逐条对应原版 `utils/context.ts:16-89` 的 `MODEL_MAX_OUTPUT_TOKEN_RULES`（18 条）。
#: 刻意**保留原版的精确模式**，不用粗粒度子串：
#:
#: 粗粒度子串会**静默截断用户配置**。真实例子——用户设
#: `maxOutputTokens: 16000`、模型 `deepseek-v4-pro`：
#:
#:   原版：不匹配任何规则 → 兜底 (32000, 64000) → min(16000, 64000) = **16000**
#:   粗粒度 `deepseek` 规则 (8192, 8192) → min(16000, 8192) = **8192**
#:
#: 后者把用户明确设的预算砍掉一半且不报错，症状是「长回答被莫名截断」。
#: 上限的作用是**夹住**用户配置（避免配出服务端必然拒绝的数字），不是覆盖它。
#:
#: 顺序敏感：更具体的模式必须在前（如 `claude-opus-4-6` 在 `opus-4` 之前）。
MODEL_MAX_OUTPUT_TOKEN_RULES: tuple[tuple[tuple[str, ...], int, int], ...] = (
    (("claude-opus-4-6", "claude opus 4.6", "opus-4-6"), 128_000, 128_000),
    (("claude-sonnet-4-6", "claude sonnet 4.6", "sonnet-4-6"), 64_000, 64_000),
    (("claude-haiku-4-5", "claude haiku 4.5", "haiku-4-5"), 64_000, 64_000),
    (
        (
            "claude-opus-4-1",
            "claude opus 4.1",
            "opus-4-1",
            "claude-opus-4",
            "claude opus 4",
            "opus-4",
        ),
        32_000,
        32_000,
    ),
    (("claude-sonnet-4", "claude sonnet 4", "sonnet-4"), 64_000, 64_000),
    (("claude-3-7-sonnet", "claude 3.7 sonnet", "3-7-sonnet"), 8_192, 8_192),
    (
        ("claude-3-5-sonnet", "claude 3.5 sonnet", "3-5-sonnet", "claude-3-sonnet"),
        8_192,
        8_192,
    ),
    (("claude-3-5-haiku", "claude 3.5 haiku", "3-5-haiku"), 8_192, 8_192),
    (("claude-3-opus", "claude 3 opus"), 4_096, 4_096),
    (("claude-3-haiku", "claude 3 haiku"), 4_096, 4_096),
    (("gpt-5-codex", "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5"), 128_000, 128_000),
    (("o4-mini", "o3", "o1-pro", "o1"), 100_000, 100_000),
    (("gpt-4.1-mini", "gpt-4.1-nano", "gpt-4.1"), 32_768, 32_768),
    (("gpt-4o-mini", "gpt-4o"), 16_384, 16_384),
    (("gpt-4",), 8_192, 8_192),
    (
        (
            "gemini-2.5-pro",
            "gemini 2.5 pro",
            "gemini-2.5-flash-lite",
            "gemini 2.5 flash-lite",
            "gemini-2.5-flash",
            "gemini 2.5 flash",
        ),
        65_536,
        65_536,
    ),
    (("deepseek-reasoner",), 32_000, 64_000),
    (("deepseek-chat",), 4_000, 8_000),
)

# 这里曾有 `MAX_OUTPUT_TOKENS_RULES` 与 `DEFAULT_MAX_OUTPUT_TOKENS` 两个
# 「兼容旧名字」的别名。核对常量表时发现**没有任何代码或测试引用它们**——
# 这个项目没有需要兼容的历史调用方，别名只是我自己加出来的噪音。
#
# 零引用常量的代价不是那几行代码，而是后来者会以为它生效。


def get_model_max_output_tokens(model: str) -> tuple[int, int]:
    """按模型名取 `(默认值, 上限)`。对应原版 `getModelMaxOutputTokens`。"""
    normalized = model.strip().lower()
    for patterns, default, upper in MODEL_MAX_OUTPUT_TOKEN_RULES:
        if any(pattern in normalized for pattern in patterns):
            return default, upper
    return UNKNOWN_MODEL_MAX_OUTPUT_TOKENS


def resolve_max_output_tokens(model: str, configured: int | None = None) -> int:
    """决定本次请求的 max_tokens。对应原版 `resolveMaxOutputTokens`。

    **上限是夹子，不是覆盖。** 用户显式配置时取 `min(配置值, 上限)`；
    没配才用默认值。反过来会让「我明明设了却没用」变成静默行为。
    """
    default, upper = get_model_max_output_tokens(model)
    if configured is None or configured <= 0:
        return default
    return min(int(configured), upper)


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
    # 默认走**动态**访问器而不是模块级常量 `SETTINGS_PATH`：
    # 后者是导入期快照，`MINICODE_HOME` 在导入后变化（测试就会这么做）时
    # 它会指向错误的位置，于是「读自己的配置」变成「读别人的配置」。
    target = path or settings_path()
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
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
    """读-合并-重写。只覆盖本次给出的 key。

    默认写向**动态**访问器 `settings_path()`，理由同 `read_settings_file`：
    用导入期快照会让 `/model` 在 `MINICODE_HOME` 变化后写错文件——
    那是「看起来成功、其实写到了别处」，比报错更难发现。
    """
    target = path or settings_path()
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

    `MINICODE_*` / `MINI_CODE_*` 前缀的变量是**本工具专有**的，不会被别的工具
    设置，因此始终以进程环境为准（便于临时覆盖，例如
    `MINICODE_MODEL=x minicode`）。
    """
    source = environ if environ is not None else os.environ
    from_settings = (settings.get("env") or {}).get(key)

    if key.startswith(("MINICODE_", "MINI_CODE_")):
        return str(source.get(key) or from_settings or "").strip()
    return str(from_settings or source.get(key) or "").strip()


def resolve_dedicated(suffix: str, settings: dict[str, Any]) -> str:
    """本工具专有的配置项：两套拼写都试，原版拼写优先。

    见上面的「环境变量：两套拼写都认」。`suffix` 传大写名字，如 `"MODEL"`。
    """
    for prefix in ("MINI_CODE", "MINICODE"):
        value = resolve_env_value(f"{prefix}_{suffix}", settings)
        if value:
            return value
    return ""


def load_runtime_config(settings_path: Path | None = None) -> RuntimeConfig:
    """装载一次运行所需的全部配置。缺关键项就抛 ConfigError。

    配置来源按优先级合并（低 → 高）：

        ~/.claude/settings.json        ← 兼容 Claude Code（此前缺失）
        <MINICODE_HOME>/mcp.json
        <MINICODE_HOME>/settings.json

    见 `load_effective_settings()`。
    """
    settings = load_effective_settings(settings_path)

    def value(key: str) -> str:
        return resolve_env_value(key, settings)

    model = (
        resolve_dedicated("MODEL", settings)
        or str(settings.get("model") or "").strip()
        or value("ANTHROPIC_MODEL")
    )
    if not model:
        raise ConfigError(
            "未配置模型。请设置 MINICODE_MODEL 环境变量，"
            f"或在 {settings_path or _settings_path()} 中写入 \"model\"。"
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
        # **必须说明「去哪设置」。** 只说「未配置凭据」时，用户知道缺什么但
        # 不知道该往哪写——原版的信息里带了位置，这里跟上。
        #
        # 注意：只认这两个名字。别的常见变量（例如 `DASHSCOPE_API_KEY`）在原版
        # 与本实现里都**不被识别**，所以这里不列举它们——列举反而会让人以为
        # 写上就行。（真实踩过：settings.json 里放着 `DASHSCOPE_API_KEY`，
        # 两边都忽略它。）
        raise ConfigError(
            "未配置凭据。请在 "
            f"{_settings_path()} 的 env 里、或进程环境变量中"
            "设置 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）。"
        )

    raw_max = resolve_dedicated("MAX_OUTPUT_TOKENS", settings) or settings.get("maxOutputTokens")
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
        resolve_dedicated("THINKING", settings) or str(settings.get("thinking") or "")
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
        # 与原版 `config.ts:249` **逐字一致**：
        #
        #     `config: ${MINI_CODE_SETTINGS_PATH} > ${CLAUDE_SETTINGS_PATH} > process.env`
        #
        # 三段的顺序就是**实际查找顺序**，用户据此判断「我改了为什么不生效」。
        # 早先这里写的是 `settings: … > process.env`——少了 Claude 回退那一段，
        # 于是最常见的一种困惑（配置其实来自 Claude 的 settings.json）在
        # `/status` 里看不出来。
        source_summary=(
            f"config: {settings_path or _settings_path()} > "
            f"{claude_settings_path()} > process.env"
        ),
    )


# ---------------------------------------------------------------------------
# 三个只从**进程环境**读的开关（原版同样直接读 `process.env`）
# ---------------------------------------------------------------------------


def resolve_max_retries() -> int | None:
    """`MINI_CODE_MAX_RETRIES` / `MINICODE_MAX_RETRIES` → 重试次数。

    对应原版 `getRetryLimit()`（`anthropic-adapter.ts:12-18`）：

        const value = Number(process.env.MINI_CODE_MAX_RETRIES)
        if (!Number.isFinite(value) || value < 0) return DEFAULT_MAX_RETRIES
        return Math.floor(value)

    返回 `None` 表示「没设或设得不对，用默认值」——默认值是适配器自己的常量
    （`DEFAULT_MAX_RETRIES = 4`），不在这里复制一份。**这里是刻意的**：
    config 不能导入 adapters（会形成循环导入），而把 4 抄成第二份又会让两处
    各自漂移。

    注意这里**没有**上限校验：原版允许 `999`，只是会重试很多次。也不读
    settings.json——它是给调试用的进程级开关。
    """
    raw = env_value("MAX_RETRIES")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return math.floor(value)


def resolve_model_mode() -> str | None:
    """`MINI_CODE_MODEL_MODE` / `MINICODE_MODEL_MODE`。

    原版用它选适配器（`index.ts`）：

        process.env.MINI_CODE_MODEL_MODE === 'mock'
          ? new MockModelAdapter()
          : new AnthropicModelAdapter(...)

    **原版没有 `--mock` 标志**，这是它唯一的 mock 入口。本实现两者都支持：
    `--mock` 给人用，这个环境变量给脚本/测试用。
    """
    return env_value("MODEL_MODE")


def is_autocompact_debug() -> bool:
    """`MINI_CODE_DEBUG_AUTOCOMPACT=1` → 往 stderr 打 auto-compact 的调试信息。"""
    return env_value("DEBUG_AUTOCOMPACT") == "1"


def read_mcp_config_file(path: Path) -> dict[str, Any]:
    """读一个 `mcp.json` 形态的文件：`{"mcpServers": {...}}`。

    缺失、损坏、或结构不对 → 返回 `{}`（不抛）。MCP 配置坏掉不该让程序起不来。
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
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


def project_mcp_path(cwd: str) -> Path:
    """项目级 MCP 配置。原版 `PROJECT_MCP_PATH` 是 `cwd/.mcp.json`。

    用 `.mcp.json` 而不是 `.mini-code/mcp.json`：这是 MCP 生态的惯例文件名，
    别的工具也认它。
    """
    return Path(cwd) / ".mcp.json"


def get_mcp_config_path(scope: str, cwd: str | None = None) -> Path:
    """按作用域取 MCP 配置文件路径。原版 `getMcpConfigPath`。"""
    if scope == "project":
        return project_mcp_path(cwd or os.getcwd())
    return mcp_path()


def load_scoped_mcp_servers(scope: str, cwd: str | None = None) -> dict[str, Any]:
    """按作用域读 MCP 服务器。原版 `loadScopedMcpServers`。

    刻意**不合并**另一个作用域：`mcp list` 要能如实显示「这个作用域里到底有什么」，
    合并后用户无法判断某条是项目级还是用户级，也就没法正确地 `remove`。
    """
    return read_mcp_config_file(get_mcp_config_path(scope, cwd))


def save_scoped_mcp_servers(
    scope: str, servers: dict[str, Any], cwd: str | None = None
) -> Path:
    """按作用域写 MCP 服务器。返回写入的路径。

    整体写成 `{"mcpServers": {...}}` 并保留 2 空格缩进——原版如此，
    而且这是人也会直接编辑的文件，缩进影响可读性。
    """
    target = get_mcp_config_path(scope, cwd)
    _write_json_pretty(target, {"mcpServers": servers})
    return target


def read_mcp_tokens_file(path: Path | None = None) -> dict[str, Any]:
    """读 MCP 的 token 文件。缺失 → `{}`；损坏 → 抛错。

    **与其他配置读取不同，这里损坏时抛错而不是返回空。**

    理由是后果不对称：返回空会让 `login` 之后的 `logout` 静默地什么都没做，
    用户以为 token 已删除而它还在；抛错则让人立刻知道文件坏了。
    原版也是这个行为（只吞 ENOENT）。
    """
    target = path or mcp_tokens_path()
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}
    return parsed


def save_mcp_tokens_file(tokens: dict[str, Any], path: Path | None = None) -> Path:
    target = path or mcp_tokens_path()
    _write_json_pretty(target, tokens)
    return target


def _write_json_pretty(target: Path, payload: Any) -> None:
    """写 JSON：建父目录、2 空格缩进、结尾换行。

    结尾换行是刻意的——没有它，`git diff` 会显示 `\\ No newline at end of file`，
    而这个文件通常是要提交的项目配置。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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


def load_effective_settings(
    settings_path: Path | None = None,
    claude_path: Path | None = None,
) -> dict[str, Any]:
    """按优先级合并全部配置来源，对应原版 `config.ts:173-188`。

    优先级（低 → 高）：

        ~/.claude/settings.json      兼容 Claude Code
        <MINICODE_HOME>/mcp.json     用户级 MCP
        <MINICODE_HOME>/settings.json  本项目的主配置

    原版把「项目级 `.mcp.json`」也并进这里（`PROJECT_MCP_PATH`），但本项目
    的 `load_mcp_servers(cwd)` 已经按 cwd 处理了项目级来源，重复合并会让
    「谁胜」取决于调用顺序。因此这里只管**不依赖 cwd** 的三处，
    项目级仍由 `load_mcp_servers` 负责。

    `claude_path` 只为测试注入而存在——它不该跟随 `MINICODE_HOME`，
    见 `claude_settings_path()`。
    """
    claude = read_settings_file(claude_path or claude_settings_path())
    global_mcp = read_mcp_config_file(minicode_home() / "mcp.json")
    own = read_settings_file(settings_path)

    merged = merge_settings(claude, {"mcpServers": global_mcp})
    return merge_settings(merged, own)


