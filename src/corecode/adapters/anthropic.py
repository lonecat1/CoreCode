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
from typing import Any, Callable, Iterator

from ..abort import throw_if_aborted
from ..config import RuntimeConfig, resolve_max_output_tokens, resolve_max_retries
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

#: 所有收尾标签。
#:
#: **必须显式列出，不能从 `RESPONSE_MARKERS` 的类型名推导**——那里的第二个
#: 元素是 `"final"` / `"progress"` 这样的类型名，拿它去剥会把 `</final>`
#: 剥成 `</>`（我实际踩过这个坑）。
CLOSING_TAGS: tuple[str, ...] = (
    "</final>",
    "[/FINAL]",
    "</progress>",
    "[/PROGRESS]",
)

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
#:
#:
#: 早先这里是三段自拟文案（`[Earlier conversation was snipped to save context
#: space]` + 「do not assume anything about the removed content」）。语义不同：
#:
#:     鼓励它直接用现有上下文继续；
#:   - 自拟版在**警告**模型：别对删掉的内容做任何假设。
#:
#: 两者对同一次裁剪给出不同的行为暗示，而这是模型在被裁剪处看到的**唯一**提示。
#: 逐字相同才能谈行为对等。
_SNIP_BOUNDARY_TEXT = (
    "[Snipped earlier conversation segment]\n"
    "\n"
    "A middle portion of the earlier conversation was removed to preserve context space.\n"
    "The recent conversation and active task context are preserved."
)

# ---------------------------------------------------------------------------
# wire 响应 -> AgentStep
# ---------------------------------------------------------------------------

def parse_assistant_text(content: str) -> tuple[str, str | None]:
    """剥掉 `<final>` / `<progress>` 前缀，返回 `(正文, kind)`。

    ## 为什么收尾标签要**无条件**剥

    早先的实现只在开头匹配到标记时才去剥收尾标签。于是
    `"回答内容</final>"`（模型漏写了开头标记，或开头标记被上游吞掉）
    会把 `</final>` 原样留在正文里——用户看到回答末尾挂着一串协议标签。
    这是真实发生过的。

    现在无论开头有没有标记，都统一剥掉收尾标签。
    """
    trimmed = content.strip()
    if not trimmed:
        return "", None

    kind: str | None = None
    body = trimmed

    for prefix, marker_kind in RESPONSE_MARKERS:
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            kind = marker_kind
            break

    # 无条件剥掉所有收尾标签（不只是与开头匹配的那一个）。
    body = _strip_closing_tags(body).strip()
    return body, kind

def _strip_closing_tags(text: str) -> str:
    """剥掉文本中出现的收尾标签。

    ## 这里有一个我刚踩过的坑

    `RESPONSE_MARKERS` 的第二个元素是**类型名**（`"final"` / `"progress"`），
    **不是**收尾标签。我第一版直接拿它去 `find`，于是 `"</final>"` 里的
    `final` 被剥掉，只剩下 `"</>"`——正文末尾挂着一串残渣。

    收尾标签必须显式定义（见 `CLOSING_TAGS`），不能从类型名推。
    """
    cleaned = text
    for closing in CLOSING_TAGS:
        while True:
            lowered = cleaned.lower()
            index = lowered.find(closing.lower())
            if index == -1:
                break
            cleaned = cleaned[:index] + cleaned[index + len(closing) :]
    return cleaned

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

class AnthropicNetworkError(Exception):
    """连不上（DNS 解析失败 / 连接被拒 / 超时 / TLS 失败）。

    ## 为什么要单独一个类型

    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        url: str = "",
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.url = url
        self.timed_out = timed_out

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
        max_retries: int | None = None,
        opener: Any | None = None,
    ) -> None:
        self._config_provider = config_provider
        # `max_retries=None` → 先问环境变量（`CORECODE_MAX_RETRIES` /
        # `CORECODE_MAX_RETRIES`），再退回本模块的 `DEFAULT_MAX_RETRIES`。
        #
        # 值不是有限数或小于 0 时用默认值。以前这里**只接受构造参数**，
        # 环境变量完全没接——用户设了也没用，且不报错。
        if max_retries is None:
            # **必须显式判 None，不能用 `or`。**
            #
            # `CORECODE_MAX_RETRIES=0` 是合法值（「不重试」），而 `0` 在
            # Python 里是假值——`resolve_max_retries() or DEFAULT` 会把它
            from_env = resolve_max_retries()
            max_retries = DEFAULT_MAX_RETRIES if from_env is None else from_env
        self._max_retries = max_retries
        self._opener = opener or urllib.request.build_opener()

    def next(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Any] | None = None,
        signal: Any | None = None,
        on_delta: Callable[[str], None] | None = None,
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

        # 扩展思考开关：仅在配置了才发。
        #
        # 不发时保持各端点的默认行为——Anthropic 官方默认不思考；
        # 而百炼的 qwen3.8-max 默认**开启**思考，思考 token 与正文共享
        # `max_tokens`，预算小时会把正文挤成空字符串。所以面向百炼时
        # 应当在 settings.json 里显式写 "thinking": "disabled"。
        thinking = config.thinking_payload()
        if thinking is not None:
            body["thinking"] = thinking

        # 流式优先，失败退回非流式。
        #
        # **流式是体验增强，不是功能前提**：某些服务商/代理不支持 SSE，
        # 或中途连接中断。此时静默退回一次性请求——用户看不到逐字效果，
        # 但功能完全不受影响。这与动画、与 MCP 的处理取向一致。
        payload: dict[str, Any] | None = None
        if on_delta is not None:
            try:
                payload = self._post_streaming(config, body, signal, on_delta)
            except Exception:  # noqa: BLE001 - 退回非流式
                payload = None
        if payload is None:
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

    def _post_streaming(
        self,
        config: RuntimeConfig,
        body: dict[str, Any],
        signal: Any | None,
        on_delta: Callable[[str], None],
    ) -> dict[str, Any] | None:
        """用 SSE 流式请求，边收边回调，最后拼出与非流式等价的 payload。

        返回 `None` 表示「这次流式不可用」——调用方会退回非流式。**不抛异常**，
        因为流式失败不该让整个回合失败。

        ## 为什么最后还要拼出完整 payload

        `next()` 的解析逻辑（工具调用、思考块、用量统计）是针对**完整响应**
        写的。与其维护两套解析，不如把 SSE 增量重建成同样的结构，然后走同一条
        解析路径。这样「流式」与「非流式」的行为差异被压到最小。
        """
        request_body = {**body, "stream": True}
        encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            config.messages_endpoint(),
            data=encoded,
            headers={**config.headers(), "accept": "text/event-stream"},
            method="POST",
        )

        try:
            with self._opener.open(request, timeout=600) as response:
                content_type = (response.headers.get("content-type") or "").lower()
                if "text/event-stream" not in content_type:
                    # 服务端忽略了 stream 参数，直接返回完整 JSON。
                    # 这不是错误——当成非流式结果用即可。
                    raw = response.read().decode("utf-8")
                    parsed = json.loads(raw)
                    return parsed if isinstance(parsed, dict) else None
                return self._consume_sse(response, signal, on_delta)
        except Exception:  # noqa: BLE001 - 流式不可用时静默退回
            return None

    def _consume_sse(
        self,
        response: Any,
        signal: Any | None,
        on_delta: Callable[[str], None],
    ) -> dict[str, Any] | None:
        """解析 SSE 事件流，重建完整响应，并逐段回调正文。

        Anthropic 的事件类型里，只有 `content_block_delta` 的 `text_delta`
        是**正文**。`thinking_delta` 是思考过程，**不能**当正文回调——
        否则用户会看到模型的内心独白被当成回答打出来。
        """
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        thinking_blocks: list[dict[str, Any]] = []
        stop_reason: str | None = None
        usage: dict[str, Any] | None = None
        model_name: str | None = None
        message_id: str | None = None
        #: 工具调用的 input 是**分片**到达的 JSON 字符串，需要按 index 累积。
        tool_buffers: dict[int, dict[str, Any]] = {}
        #: 思考块的文本同样分片到达。
        thinking_buffers: dict[int, list[str]] = {}

        for event in _iter_sse_events(response):
            throw_if_aborted(signal)
            kind = event.get("type")

            if kind == "message_start":
                message = event.get("message") or {}
                model_name = message.get("model")
                message_id = message.get("id")
                if isinstance(message.get("usage"), dict):
                    usage = dict(message["usage"])
                continue

            if kind == "content_block_start":
                block = event.get("content_block") or {}
                index = int(event.get("index", 0))
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_buffers[index] = {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "json": "",
                    }
                elif block_type == "thinking":
                    thinking_buffers[index] = []
                    thinking_blocks.append(
                        {"type": "thinking", "thinking": "", "signature": None}
                    )
                elif block_type == "redacted_thinking":
                    thinking_blocks.append(
                        {"type": "redacted_thinking", "data": block.get("data", "")}
                    )
                continue

            if kind == "content_block_delta":
                index = int(event.get("index", 0))
                delta = event.get("delta") or {}
                delta_type = delta.get("type")

                if delta_type == "text_delta":
                    piece = delta.get("text") or ""
                    if piece:
                        text_parts.append(piece)
                        on_delta(piece)
                elif delta_type == "input_json_delta":
                    buffer = tool_buffers.get(index)
                    if buffer is not None:
                        buffer["json"] += delta.get("partial_json") or ""
                elif delta_type == "thinking_delta":
                    # 思考过程**不回调**给界面——那不是给用户看的正文。
                    buffer = thinking_buffers.get(index)
                    if buffer is not None:
                        buffer.append(delta.get("thinking") or "")
                elif delta_type == "signature_delta":
                    for block in thinking_blocks:
                        if block.get("type") == "thinking" and block.get("signature") is None:
                            block["signature"] = delta.get("signature")
                            break
                continue

            if kind == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                if isinstance(event.get("usage"), dict):
                    # message_delta 里的 usage 是**增量**（主要是 output_tokens），
                    # 与 message_start 的合并而不是覆盖。
                    usage = {**(usage or {}), **event["usage"]}
                continue

            if kind == "error":
                # 流中途报错：放弃这次流式，让调用方退回非流式重试。
                return None

        # 把分片的工具参数拼成对象
        for index in sorted(tool_buffers):
            buffer = tool_buffers[index]
            raw_json = buffer.pop("json", "")
            parsed_input: Any = {}
            if raw_json.strip():
                try:
                    parsed_input = json.loads(raw_json)
                except json.JSONDecodeError:
                    parsed_input = {}
            tool_calls.append(
                {
                    "type": "tool_use",
                    "id": buffer.get("id"),
                    "name": buffer.get("name"),
                    "input": parsed_input,
                }
            )

        # 把分片的思考文本拼回去
        for index, pieces in thinking_buffers.items():
            text = "".join(pieces)
            for block in thinking_blocks:
                if block.get("type") == "thinking" and not block.get("thinking"):
                    block["thinking"] = text
                    break

        # 重建与非流式等价的响应结构，交给同一条解析路径。
        content: list[dict[str, Any]] = []
        if text_parts:
            content.append({"type": "text", "text": "".join(text_parts)})
        content.extend(thinking_blocks)
        content.extend(tool_calls)

        return {
            "id": message_id,
            "model": model_name,
            "role": "assistant",
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage,
        }

    def _post_with_retry(
        self, config: RuntimeConfig, body: dict[str, Any], signal: Any | None
    ) -> dict[str, Any]:
        """发一次请求，**只在 HTTP 状态码可重试时**重试。

            for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
              response = await fetch(url, {...})       // ← 没有 try/catch
              if (response.ok) break
              if (!shouldRetryStatus(response.status) || attempt >= maxRetries) break
              await abortableDelay(...)
            }

        本实现早先无条件重试 `URLError`——DNS 解析失败会白等 **4 次退避、约 9 秒**
        才报错，而重试一个「hostname 解析不了」不会有任何机会成功。

        ## 错误消息也翻译过

        """
        from ..utils.web import (
            format_web_error_message,
            is_timeout_error,
            node_error_code,
        )

        url = config.messages_endpoint()
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")

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
                delay = retry_delay_seconds(
                    attempt + 1, parse_retry_after(error.headers.get("retry-after"))
                )
            except (urllib.error.URLError, OSError) as error:
                #
                # 捕获范围是 `URLError` **加上** `OSError`：`socket.timeout` 与
                # `ConnectionResetError` 这类异常**不一定**被 urllib 包成
                # `URLError`——读响应体时超时就是一个裸 `socket.timeout`。
                # 只写 `except URLError` 会让这类错误原样冒泡，
                # 用户看到的又是一句没有地址、没有码的 Python 内部信息。
                raise AnthropicNetworkError(
                    format_web_error_message(url, error, 600_000),
                    code=node_error_code(error),
                    url=url,
                    timed_out=is_timeout_error(error),
                ) from error

            _sleep_interruptible(delay, signal)

        raise RuntimeError("请求失败且没有记录到错误")

def _iter_sse_events(response: Any) -> Iterator[dict[str, Any]]:
    """把 SSE 响应流解析成一个个 JSON 事件。

    这是**纯解析逻辑**，因此可以脱离网络单独测试——流式最容易出错的地方
    就是分帧（一条事件可能跨多次 read 到达，也可能一次到达多条）。

    SSE 格式：:

        event: content_block_delta
        data: {"type":"content_block_delta",...}

        （空行分隔事件）

    实现上只关心 `data:` 行：Anthropic 的 `event:` 字段与 data 里的 `type`
    重复，因此以 data 为准即可。`[DONE]` 是 OpenAI 风格的结束标记，
    这里也顺带容忍，避免遇到兼容端点时把 `[DONE]` 当 JSON 解析而报错。
    """
    buffer = ""
    for raw_chunk in response:
        if not raw_chunk:
            continue
        buffer += raw_chunk.decode("utf-8", errors="replace")

        # 事件以空行分隔。兼容 \r\n 与 \n。
        while True:
            separator = _find_event_separator(buffer)
            if separator is None:
                break
            block, buffer = buffer[: separator[0]], buffer[separator[1] :]
            event = _parse_sse_block(block)
            if event is not None:
                yield event

    # 流结束时可能还有一段没有以空行收尾的事件
    tail = _parse_sse_block(buffer)
    if tail is not None:
        yield tail

def _find_event_separator(buffer: str) -> tuple[int, int] | None:
    """找到事件分隔符，返回 `(事件结束位置, 下一事件起始位置)`。"""
    for marker in ("\r\n\r\n", "\n\n", "\r\r"):
        index = buffer.find(marker)
        if index != -1:
            return index, index + len(marker)
    return None

def _parse_sse_block(block: str) -> dict[str, Any] | None:
    """解析一个 SSE 事件块。只取 `data:` 行并解析成 JSON。"""
    payload_lines: list[str] = []
    for line in block.splitlines():
        if not line or line.startswith(":"):
            continue  # 空行与注释（SSE 用心跳）
        if line.startswith("data:"):
            payload_lines.append(line[len("data:") :].lstrip())

    if not payload_lines:
        return None
    raw = "\n".join(payload_lines).strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

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
