"""回合反思与记忆提炼。

这一层只做确定性分析：不额外调用模型，因此不会让每个回合多付一次 API
费用，也能在离线模式下完整工作。它把执行轨迹拆成四类可复用信号：

    episodic    本次任务是什么、做了什么、结果如何
    procedural  哪条成功工具序列可以再次使用
    repair      哪个错误之后采取了什么动作才恢复
    profile     用户明确表达的长期偏好或约束

`reflection` 与 `extraction` 是两步而不是一步：先形成对结果的结构化判断，
再由判断生成资产。这样可以单独测试「是否理解正确」，而不是只看最终文本。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..types import (
    ROLE_ASSISTANT,
    ROLE_ASSISTANT_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ChatMessage,
)
from .index import tokenize
from .types import (
    MEMORY_KIND_EPISODIC,
    MEMORY_KIND_PROCEDURAL,
    MEMORY_KIND_PROFILE,
    MEMORY_KIND_REPAIR,
    MEMORY_SCOPE_GLOBAL,
    MEMORY_SCOPE_PROJECT,
    MemoryAsset,
    now_ms,
)

MAX_EPISODIC_CONTENT = 5_000
MAX_PROCEDURAL_CONTENT = 3_500
MAX_REPAIR_CONTENT = 2_500
MAX_PROFILE_CONTENT = 500

_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_SECRET_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

_PROFILE_CUES = re.compile(
    r"(以后|今后|后续|每次|总是|始终|一向|习惯|喜欢|偏好|优先|"
    r"记住|请记住|我希望|我要求|对我来说|"
    r"我的风格|我的偏好|我叫|我的名字是|"
    r"\b(always|never|prefer|from now on|remember that|remember:|"
    r"call me|my name is)\b)",
    re.IGNORECASE,
)

_PROCEDURE_TOOL_WEIGHTS = {
    "read_file": 1,
    "list_files": 1,
    "grep_files": 1,
    "web_search": 1,
    "web_fetch": 1,
    "load_skill": 1,
    "write_file": 2,
    "edit_file": 3,
    "patch_file": 3,
    "modify_file": 3,
    "run_command": 3,
    "update_plan": 1,
}

_GENERIC_TAGS = {
    "file",
    "files",
    "code",
    "task",
    "tool",
    "tools",
    "result",
    "success",
    "failed",
    "working",
    "workflow",
    "please",
    "需要",
    "可以",
    "这个",
    "进行",
    "一个",
    "然后",
}


@dataclass(slots=True)
class ToolObservation:
    call_id: str
    tool_name: str
    input: Any
    output: str
    is_error: bool
    position: int


@dataclass(slots=True)
class TurnReflection:
    request: str
    outcome: str
    final_text: str = ""
    tools: list[ToolObservation] = field(default_factory=list)
    errors: list[ToolObservation] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    assets: list[MemoryAsset] = field(default_factory=list)


def redact_sensitive(text: str) -> str:
    """在记忆落盘前移除常见凭据，避免长期库扩大泄漏面。"""
    value = str(text or "")
    value = _SECRET_VALUE.sub(r"\1=[REDACTED]", value)
    value = _SECRET_TOKEN.sub("[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return value


def _truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 12)].rstrip() + "\n…（已截断）"


def _message_text(message: ChatMessage) -> str:
    content = message.get("content")
    return redact_sensitive(content if isinstance(content, str) else "")


def _message_id(message: ChatMessage) -> str:
    value = message.get("id")
    return str(value) if value else ""


def _last_assistant_text(messages: Iterable[ChatMessage]) -> str:
    for message in reversed(list(messages)):
        if message.get("role") == ROLE_ASSISTANT:
            text = _message_text(message)
            if text.strip():
                return text.strip()
    return ""


def _collect_tools(messages: Iterable[ChatMessage]) -> list[ToolObservation]:
    calls: dict[str, ChatMessage] = {}
    for message in messages:
        if message.get("role") != ROLE_ASSISTANT_TOOL_CALL:
            continue
        call_id = str(message.get("toolUseId") or "")
        if call_id:
            calls[call_id] = message

    observations: list[ToolObservation] = []
    for position, message in enumerate(messages):
        if message.get("role") != ROLE_TOOL_RESULT:
            continue
        call_id = str(message.get("toolUseId") or "")
        call = calls.get(call_id, {})
        observations.append(
            ToolObservation(
                call_id=call_id,
                tool_name=str(
                    message.get("toolName")
                    or call.get("toolName")
                    or "unknown"
                ),
                input=call.get("input"),
                output=_message_text(message),
                is_error=bool(message.get("isError")),
                position=position,
            )
        )
    return observations


def _input_brief(tool_input: Any, limit: int = 220) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "path", "query", "url", "name", "question"):
            if key in tool_input and str(tool_input[key]).strip():
                return redact_sensitive(
                    f"{key}={_truncate(str(tool_input[key]), limit)}"
                )
        rendered = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(tool_input or "")
    return redact_sensitive(_truncate(rendered, limit))


def _tool_signature(observation: ToolObservation) -> str:
    """提炼跨任务稳定的步骤特征，不把临时绝对路径写进去重键。"""
    name = observation.tool_name
    payload = observation.input if isinstance(observation.input, dict) else {}
    if name == "run_command":
        command = str(payload.get("command") or "").strip()
        executable = command.split()[0] if command else "command"
        return f"{name}:{Path(executable).name.lower()}"
    if name in {"read_file", "write_file", "edit_file", "patch_file", "modify_file"}:
        path = str(payload.get("path") or "")
        suffix = Path(path).suffix.lower()
        return f"{name}:{suffix}" if suffix else name
    if name in {"grep_files", "list_files"}:
        return name
    return name


def _stable_key(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _tags(text: str, tools: Iterable[str] = ()) -> list[str]:
    counts = Counter(token for token in tokenize(text) if token not in _GENERIC_TAGS)
    tags = [token for token, _count in counts.most_common(18)]
    for tool in tools:
        if tool and tool not in tags:
            tags.append(tool)
    return tags[:24]


def _split_sentences(text: str) -> list[str]:
    # 保留句末标点；记忆里原话比改写后的摘要更容易判断上下文。
    pieces = re.split(r"(?<=[。！？!?；;\n])", str(text or ""))
    return [piece.strip() for piece in pieces if piece.strip()]


def _profile_assets(
    user_text: str,
    *,
    cwd: str,
    session_id: str,
    evidence: list[str],
) -> list[MemoryAsset]:
    if not user_text.strip():
        return []
    assets: list[MemoryAsset] = []
    for sentence in _split_sentences(user_text):
        normalized = sentence.strip().strip("-* ")
        if not normalized or len(normalized) < 4 or len(normalized) > MAX_PROFILE_CONTENT:
            continue
        if normalized.endswith(("?", "？")) or normalized.startswith(
            ("为什么", "怎么", "如何", "what", "why", "how")
        ):
            continue
        if not _PROFILE_CUES.search(normalized):
            continue
        identity = bool(
            re.search(r"(我叫|我的名字是|call me|my name is)", normalized, re.I)
        )
        category = "identity" if identity else "preference"
        assets.append(
            MemoryAsset(
                kind=MEMORY_KIND_PROFILE,
                title=f"User {category}: {_truncate(normalized, 72)}",
                content=normalized,
                summary=normalized,
                tags=["profile", category, *_tags(normalized)],
                scope=MEMORY_SCOPE_GLOBAL,
                project="",
                session_id=session_id,
                evidence=evidence,
                confidence=0.88 if identity else 0.82,
                importance=0.92 if identity else 0.84,
                dedupe_key=f"profile:{_stable_key(normalized.lower())}",
                metadata={"category": category, "sourceCwd": cwd},
            )
        )
    return assets


def _episodic_asset(
    reflection: TurnReflection,
    *,
    cwd: str,
    session_id: str,
    evidence: list[str],
    anchor: str,
) -> MemoryAsset | None:
    request = reflection.request.strip()
    if not request:
        return None

    lines = [f"Request: {_truncate(request, 1_200)}", f"Outcome: {reflection.outcome}"]
    if reflection.tools:
        lines.append("Actions:")
        for observation in reflection.tools:
            status = "error" if observation.is_error else "ok"
            lines.append(
                f"- {observation.tool_name} ({status}): "
                f"{_input_brief(observation.input)}"
            )
    if reflection.lessons:
        lines.append("Reflection:")
        lines.extend(f"- {lesson}" for lesson in reflection.lessons)
    if reflection.final_text:
        lines.append(f"Result: {_truncate(reflection.final_text, 1_000)}")

    content = _truncate("\n".join(lines), MAX_EPISODIC_CONTENT)
    title_request = _truncate(" ".join(request.split()), 80)
    errors = sum(1 for item in reflection.tools if item.is_error)
    tags = _tags(
        " ".join([request, reflection.final_text, *(o.tool_name for o in reflection.tools)]),
        [observation.tool_name for observation in reflection.tools],
    )
    return MemoryAsset(
        kind=MEMORY_KIND_EPISODIC,
        title=f"Episode: {title_request}",
        content=content,
        summary=f"{title_request} → {reflection.outcome}",
        tags=tags,
        scope=MEMORY_SCOPE_PROJECT,
        project=cwd,
        session_id=session_id,
        evidence=evidence,
        confidence=0.66 if errors else 0.6,
        importance=0.68 if errors else 0.48,
        dedupe_key=(
            f"episodic:"
            f"{_stable_key(session_id, anchor or str(now_ms()), request, reflection.outcome)}"
        ),
        metadata={
            "outcome": reflection.outcome,
            "toolCount": len(reflection.tools),
            "errorCount": errors,
        },
    )


def _procedural_asset(
    reflection: TurnReflection,
    *,
    cwd: str,
    session_id: str,
    evidence: list[str],
) -> MemoryAsset | None:
    if reflection.outcome not in {"final", "controlled_stop"}:
        return None
    successful = [item for item in reflection.tools if not item.is_error]
    if len(successful) < 2:
        return None
    if any(item.is_error for item in reflection.tools) and reflection.outcome == "failed":
        return None

    signatures = [_tool_signature(item) for item in successful]
    weight = sum(_PROCEDURE_TOOL_WEIGHTS.get(item.tool_name, 1) for item in successful)
    if weight < 3:
        return None

    steps = []
    for index, observation in enumerate(successful, 1):
        steps.append(
            f"{index}. {observation.tool_name}: {_input_brief(observation.input)}"
        )
    content = _truncate(
        "\n".join(
            [
                f"Observed successful workflow for: {_truncate(reflection.request, 500)}",
                "Steps:",
                *steps,
                "Verification:",
                f"- Turn outcome: {reflection.outcome}",
                f"- Final result: {_truncate(reflection.final_text, 600) or 'not recorded'}",
            ]
        ),
        MAX_PROCEDURAL_CONTENT,
    )
    sequence = " -> ".join(item.tool_name for item in successful)
    return MemoryAsset(
        kind=MEMORY_KIND_PROCEDURAL,
        title=f"Workflow: {_truncate(sequence, 96)}",
        content=content,
        summary=f"Successful tool sequence: {_truncate(sequence, 240)}",
        tags=_tags(
            " ".join([reflection.request, sequence]),
            [observation.tool_name for observation in successful],
        ),
        scope=MEMORY_SCOPE_PROJECT,
        project=cwd,
        session_id=session_id,
        evidence=evidence,
        confidence=0.72 if weight >= 6 else 0.64,
        importance=0.7 if weight >= 6 else 0.56,
        dedupe_key=f"procedural:{_stable_key(*signatures)}",
        metadata={
            "steps": [
                {
                    "tool": observation.tool_name,
                    "input": _input_brief(observation.input, 400),
                }
                for observation in successful
            ],
            "sequence": sequence,
            "weight": weight,
            "examples": [_truncate(reflection.request, 220)],
            "outcome": reflection.outcome,
        },
    )


def _normalize_error(text: str) -> str:
    value = redact_sensitive(text).strip()
    value = re.sub(r"[A-Za-z]:\\[^\s:]+", "<path>", value)
    value = re.sub(r"(?:/[\w.-]+){3,}", "<path>", value)
    value = re.sub(r"\bline\s+\d+\b", "line <n>", value, flags=re.IGNORECASE)
    value = re.sub(r":\d+(?::\d+)?", ":<n>", value)
    value = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", value)
    first = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return _truncate(first, 260)


def _repair_assets(
    reflection: TurnReflection,
    *,
    cwd: str,
    session_id: str,
    evidence: list[str],
) -> list[MemoryAsset]:
    assets: list[MemoryAsset] = []
    for error in reflection.errors:
        recovery = next(
            (
                item
                for item in reflection.tools
                if item.position > error.position and not item.is_error
            ),
            None,
        )
        if recovery is None:
            continue
        signature = _normalize_error(error.output)
        if not signature:
            continue
        recovery_steps = [
            item
            for item in reflection.tools
            if error.position < item.position <= recovery.position and not item.is_error
        ]
        lines = [
            f"Error signature: {signature}",
            f"Failed tool: {error.tool_name}",
            f"Failed input: {_input_brief(error.input)}",
            "Successful recovery:",
        ]
        for index, item in enumerate(recovery_steps, 1):
            lines.append(f"{index}. {item.tool_name}: {_input_brief(item.input)}")
        lines.append("Use this recovery sequence when the same error signature recurs.")
        assets.append(
            MemoryAsset(
                kind=MEMORY_KIND_REPAIR,
                title=f"Repair: {error.tool_name}: {_truncate(signature, 72)}",
                content=_truncate("\n".join(lines), MAX_REPAIR_CONTENT),
                summary=f"{error.tool_name} failed; {recovery.tool_name} recovered it.",
                tags=_tags(
                    f"{signature} {error.tool_name} {recovery.tool_name}",
                    [error.tool_name, recovery.tool_name, "error", "repair"],
                ),
                scope=MEMORY_SCOPE_PROJECT,
                project=cwd,
                session_id=session_id,
                evidence=evidence,
                confidence=0.86,
                importance=0.9,
                dedupe_key=(
                    "repair:"
                    + _stable_key(
                        error.tool_name,
                        signature,
                        *(_tool_signature(item) for item in recovery_steps),
                    )
                ),
                metadata={
                    "errorSignature": signature,
                    "failedTool": error.tool_name,
                    "recoveryTools": [item.tool_name for item in recovery_steps],
                },
            )
        )
    return assets


def _lessons(reflection: TurnReflection) -> list[str]:
    data: list[str] = []
    if not reflection.tools:
        data.append(f"Turn finished with outcome={reflection.outcome} without tool use.")
        return data

    errors = [item for item in reflection.tools if item.is_error]
    if not errors:
        data.append(
            "All tool calls succeeded: "
            + " -> ".join(item.tool_name for item in reflection.tools)
        )
        return data

    for error in errors:
        recovery = next(
            (
                item
                for item in reflection.tools
                if item.position > error.position and not item.is_error
            ),
            None,
        )
        if recovery is None:
            data.append(f"{error.tool_name} failed and no later successful action recovered it.")
            continue
        data.append(
            f"{error.tool_name} failed; {recovery.tool_name} completed a later recovery step."
        )
    return data


def reflect_turn(
    turn_messages: list[ChatMessage],
    *,
    cwd: str,
    session_id: str,
    user_text: str,
    outcome: str,
) -> TurnReflection:
    """反思一个回合并提炼记忆资产。"""
    clean_user_text = redact_sensitive(user_text)
    tools = _collect_tools(turn_messages)
    evidence = [item for item in (_message_id(message) for message in turn_messages) if item]
    reflection = TurnReflection(
        request=clean_user_text,
        outcome=outcome,
        final_text=_last_assistant_text(turn_messages),
        tools=tools,
        errors=[item for item in tools if item.is_error],
    )
    reflection.lessons = _lessons(reflection)

    assets: list[MemoryAsset] = []
    request_anchor = next(
        (
            _message_id(message)
            for message in turn_messages
            if message.get("role") == ROLE_USER and not message.get("internal")
        ),
        "",
    )
    assets.extend(
        _profile_assets(
            clean_user_text,
            cwd=cwd,
            session_id=session_id,
            evidence=evidence,
        )
    )
    episodic = _episodic_asset(
        reflection,
        cwd=cwd,
        session_id=session_id,
        evidence=evidence,
        anchor=request_anchor,
    )
    if episodic is not None:
        assets.append(episodic)
    procedural = _procedural_asset(
        reflection,
        cwd=cwd,
        session_id=session_id,
        evidence=evidence,
    )
    if procedural is not None:
        assets.append(procedural)
    assets.extend(
        _repair_assets(
            reflection,
            cwd=cwd,
            session_id=session_id,
            evidence=evidence,
        )
    )
    reflection.assets = assets
    return reflection


__all__ = [
    "MAX_EPISODIC_CONTENT",
    "MAX_PROFILE_CONTENT",
    "ToolObservation",
    "TurnReflection",
    "redact_sensitive",
    "reflect_turn",
]
