"""Anthropic Messages API 适配器。

这个模块只负责一件事：把内部的 `ChatMessage` 列表翻译成 Messages API 的
wire 格式，再把响应翻译回 `AgentStep`。所有循环控制、重试语义都在别处。

两个必须做对的细节：

1. **thinking 块必须原样回传。** 带扩展思考的响应会包含 `thinking` /
   `redacted_thinking` 块。它们在下一次请求里必须作为 assistant 内容原样
   送回（含签名），否则模型会丢失推理上下文，或直接被服务端拒绝。
   因此 `assistant_thinking` 消息不拼进文本，而是展开成独立的内容块。

2. **相邻同角色消息必须合并。** Messages API 要求 user / assistant 严格交替。
   而内部消息序列里，连续的 tool_result 会各自成为一条 user 消息，
   tool_call + thinking + 文本也会各自成为 assistant 消息。合并规则是
   「最后一条角色相同就追加内容块，否则新起一条」。

重试策略：429 与 5xx 退避重试，尊重 `Retry-After`，最多 4 次。
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ..abort import throw_if_aborted
from ..config import RuntimeConfig, resolve_max_output_tokens
from ..types import AgentStep, ProviderUsage, StepDiagnostics, ToolCall

DEFAULT_MAX_RETRIES = 4
BASE_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 8.0

#: 模型用这些前缀声明这段文本是最终答复还是中途进度。
RESPONSE_MARKERS: tuple[tuple[str, str], ...] = (
    ("<final>", "final"),
    ("[FINAL]", "final"),
    ("<progress>", "progress"),
    ("[PROGRESS]", "progress"),
)

#: 进度消息回传给模型时的包裹格式。
PROGRESS_TAG_OPEN = "<progress>"
PROGRESS_TAG_CLOSE = "</progress>"


# ---------------------------------------------------------------------------
# 内部消息 -> wire 格式
# ---------------------------------------------------------------------------


def to_text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def append_wire_message(
    messages: list[dict[str, Any]], role: str, block: dict[str, Any]
) -> None:
    """追加一个内容块，必要时先合并同角色的相邻消息。"""
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
        return
    messages.append({"role": role, "content": [block]})


def to_assistant_text(message: dict[str, Any]) -> str:
    """assistant_progress 回传给模型时要带上进度标签，否则模型会以为
    自己之前给出的是最终答复。"""
    content = str(message.get("content", ""))
    if message.get("role") == "assistant_progress":
        return f"{PROGRESS_TAG_OPEN}\n{content}\n{PROGRESS_TAG_CLOSE}"
    return content


def to_wire_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """拆成 `(system, messages)`。system 必须单独传，不能留在 messages 里。"""
    system = "\n\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )

    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")

        if role == "system":
            continue

        if role == "user":
            append_wire_message(converted, "user", to_text_block(str(message.get("content", ""))))
            continue

        if role == "assistant_thinking":
            # 展开成独立块并保持原样，签名不能丢。
            for block in message.get("blocks") or []:
                append_wire_message(converted, "assistant", block)
            continue

        if role in ("assistant", "assistant_progress"):
            append_wire_message(converted, "assistant", to_text_block(to_assistant_text(message)))
            continue

        if role == "assistant_tool_call":
            append_wire_message(
                converted,
                "assistant",
                {
                    "type": "tool_use",
                    "id": message.get("toolUseId"),
                    "name": message.get("toolName"),
                    "input": message.get("input"),
                },
            )
            continue

        if role == "context_summary":
            append_wire_message(
                converted,
                "user",
                to_text_block(
                    "[Context Summary from earlier conversation]\n"
                    + str(message.get("content", ""))
                ),
            )
            continue

        if role == "snip_boundary":
            append_wire_message(converted, "user", to_text_block(_SNIP_BOUNDARY_TEXT))
            continue

        if role == "tool_result":
            append_wire_message(
                converted,
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("toolUseId"),
                    "content": str(message.get("content", "")),
                    "is_error": bool(message.get("isError")),
                },
            )
            continue

    return system, converted


#: 取代被 snip 删除的那段历史。模型看到它才知道「中间有内容被裁剪了」，
#: 而不是以为对话本来就断开。
_SNIP_BOUNDARY_TEXT = (
    "[Earlier conversation was snipped to save context space]\n"
    "Some messages between the earlier part of this conversation and this point were "
    "removed to free up context. The conversation continues from here; do not assume "
    "anything about the removed content."
)


# ---------------------------------------------------------------------------
# wire 响应 -> AgentStep
# ---------------------------------------------------------------------------


def parse_assistant_text(content: str) -> tuple[str, str | None]:
    """剥掉 `<final>` / `<progress>` 前缀，返回 `(正文, kind)`。"""
    trimmed = content.strip()
    if not trimmed:
        return "", None

    for prefix, kind in RESPONSE_MARKERS:
        if trimmed.startswith(prefix):
            body = trimmed[len(prefix) :].strip()
            closing = "</progress>" if kind == "progress" else "</final>"
            # 大小写不敏感地去掉收尾标签。
            lowered = body.lower()
            index = lowered.rfind(closing)
            if index != -1:
                body = (body[:index] + body[index + len(closing) :]).strip()
            return body, kind

    return trimmed, None


def normalize_usage(usage: dict[str, Any] | None) -> Any:
    """把 Anthropic 的 usage 归一化成 ProviderUsage。

    缓存相关的 token 也算 input：它们同样占用上下文，
    漏掉会让上下文利用率被严重低估。
    """
    if not usage:
        return None
    input_tokens = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    output_tokens = int(usage.get("output_tokens") or 0)
    total = input_tokens + output_tokens
    if total <= 0:
        return None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        source="anthropic",
    )


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def should_retry_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        if seconds >= 0:
            return seconds
    except ValueError:
        pass
    # 也接受 HTTP-date 形式。
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        return max(0.0, target.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def retry_delay_seconds(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    base = min(BASE_RETRY_DELAY_SECONDS * (2 ** max(0, attempt - 1)), MAX_RETRY_DELAY_SECONDS)
    return base + random.random() * 0.25 * base


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class AnthropicHttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AnthropicModelAdapter:
    """通过 HTTP 调用 Anthropic 兼容的 `/v1/messages`。

    `config_provider` 允许每次请求重新读配置，因此 `/model` 切换模型后
    无需重建适配器。
    """

    name = "anthropic"

    def __init__(
        self,
        config_provider: Callable[[], RuntimeConfig],
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        opener: Any | None = None,
    ) -> None:
        self._config_provider = config_provider
        self._max_retries = max_retries
        self._opener = opener or urllib.request.build_opener()

    def next(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Any] | None = None,
        signal: Any | None = None,
    ) -> Any:
        throw_if_aborted(signal)
        config = self._config_provider()
        system, wire_messages = to_wire_messages(messages)

        body: dict[str, Any] = {
            "model": config.model,
            "system": system,
            "messages": wire_messages,
            "max_tokens": resolve_max_output_tokens(config.model, config.max_output_tokens),
        }
        if tools:
            body["tools"] = list(tools)

        payload = self._post_with_retry(config, body, signal)

        # -- 解析响应 ---------------------------------------------------------
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        thinking_blocks: list[dict[str, Any]] = []
        block_types: list[str] = []
        ignored: list[str] = []

        for block in payload.get("content") or []:
            block_type = block.get("type")
            block_types.append(str(block_type))

            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "tool_use" and isinstance(block.get("id"), str):
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        toolName=str(block.get("name", "")),
                        input=block.get("input"),
                    )
                )
            elif block_type in ("thinking", "redacted_thinking"):
                thinking_blocks.append(block)
            else:
                ignored.append(str(block_type))

        text, kind = parse_assistant_text("\n".join(text_parts).strip())
        diagnostics = StepDiagnostics(
            stop_reason=payload.get("stop_reason"),
            block_types=block_types,
            ignored_block_types=ignored,
        )
        usage = normalize_usage(payload.get("usage"))

        if tool_calls:
            return AgentStep(
                type="tool_calls",
                content=text,
                content_kind="progress" if kind == "progress" else None,
                calls=tool_calls,
                thinking_blocks=thinking_blocks,
                diagnostics=diagnostics,
                usage=usage,
            )

        return AgentStep(
            type="assistant",
            content=text,
            kind=kind,  # type: ignore[arg-type]
            thinking_blocks=thinking_blocks,
            diagnostics=diagnostics,
            usage=usage,
        )

    # -- HTTP ---------------------------------------------------------------

    def _post_with_retry(
        self, config: RuntimeConfig, body: dict[str, Any], signal: Any | None
    ) -> dict[str, Any]:
        url = config.messages_endpoint()
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            throw_if_aborted(signal)
            request = urllib.request.Request(
                url, data=encoded, headers=config.headers(), method="POST"
            )
            try:
                with self._opener.open(request, timeout=600) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = _read_error_body(error)
                if not should_retry_status(error.code) or attempt == self._max_retries:
                    raise AnthropicHttpError(error.code, detail) from error
                last_error = AnthropicHttpError(error.code, detail)
                delay = retry_delay_seconds(
                    attempt + 1, parse_retry_after(error.headers.get("retry-after"))
                )
            except urllib.error.URLError as error:
                last_error = error
                if attempt == self._max_retries:
                    raise
                delay = retry_delay_seconds(attempt + 1, None)

            _sleep_interruptible(delay, signal)

        raise last_error or RuntimeError("请求失败且没有记录到错误")


def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return f"HTTP {error.code} {error.reason}"
    if not raw:
        return f"HTTP {error.code} {error.reason}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        inner = parsed.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
        if parsed.get("message"):
            return str(parsed["message"])
    return raw


def _sleep_interruptible(seconds: float, signal: Any | None) -> None:
    """可被取消的等待：把长退避切成小片，以便 Ctrl-C 能立即生效。"""
    remaining = max(0.0, seconds)
    while remaining > 0:
        throw_if_aborted(signal)
        chunk = min(0.2, remaining)
        time.sleep(chunk)
        remaining -= chunk
