"""模型上下文窗口表。

把模型名映射到 `(上下文窗口, 输出预留, 有效输入)`。有效输入 = 窗口 − 预留，
因为输出 token 也占同一条上下文预算，不能全给输入用。

**匹配是子串包含，且顺序敏感**：先命中者生效。因此更具体的模式必须排在
更宽泛的模式之前。两个经典陷阱：

- `claude-opus-4-6` 必须排在 `opus-4` 之前；
- `gpt-4.1` 必须排在 `gpt-4` 之前（`gpt-4` 是 `gpt-4.1` 的子串）。

未命中的模型落到 `UNKNOWN`（128k 窗口），这是一个保守但可用的默认值。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelContextWindow:
    context_window: int
    output_reserve: int
    effective_input: int


def _rule(context_window: int, output_reserve: int) -> ModelContextWindow:
    return ModelContextWindow(
        context_window=context_window,
        output_reserve=output_reserve,
        effective_input=context_window - output_reserve,
    )


#: 未知模型的兜底：128k 窗口，预留 8k。
UNKNOWN_MODEL_CONTEXT = _rule(128_000, 8_000)

#: 顺序敏感！更具体的模式必须在前。
MODEL_CONTEXT_RULES: tuple[tuple[tuple[str, ...], ModelContextWindow], ...] = (
    # -- Claude 4.x --
    (
        ("claude-opus-4-6", "claude opus 4.6", "opus-4-6"),
        _rule(200_000, 16_000),
    ),
    (
        ("claude-sonnet-4-6", "claude sonnet 4.6", "sonnet-4-6"),
        _rule(200_000, 16_000),
    ),
    (
        ("claude-haiku-4-5", "claude haiku 4.5", "haiku-4-5"),
        _rule(200_000, 16_000),
    ),
    (
        (
            "claude-opus-4-1",
            "claude opus 4.1",
            "opus-4-1",
            "claude-opus-4",
            "claude opus 4",
            "opus-4",
        ),
        _rule(200_000, 16_000),
    ),
    (
        ("claude-sonnet-4", "claude sonnet 4", "sonnet-4"),
        _rule(200_000, 16_000),
    ),
    # -- Claude 3.x --
    (
        ("claude-3-7-sonnet", "claude 3.7 sonnet", "3-7-sonnet"),
        _rule(200_000, 8_192),
    ),
    (
        (
            "claude-3-5-sonnet",
            "claude 3.5 sonnet",
            "3-5-sonnet",
            "claude-3-sonnet",
        ),
        _rule(200_000, 8_192),
    ),
    (
        ("claude-3-5-haiku", "claude 3.5 haiku", "3-5-haiku"),
        _rule(200_000, 8_192),
    ),
    (("claude-3-opus", "claude 3 opus"), _rule(200_000, 4_096)),
    (("claude-3-haiku", "claude 3 haiku"), _rule(200_000, 4_096)),
    # -- OpenAI --
    (
        ("gpt-5-codex", "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5"),
        _rule(128_000, 16_000),
    ),
    (("o4-mini", "o3", "o1-pro", "o1"), _rule(200_000, 16_000)),
    (
        ("gpt-4.1-mini", "gpt-4.1-nano", "gpt-4.1"),
        _rule(1_047_576, 16_000),
    ),
    (("gpt-4o-mini", "gpt-4o"), _rule(128_000, 16_384)),
    (("gpt-4",), _rule(128_000, 8_192)),
    # -- Gemini --
    (("gemini-2.5-pro", "gemini 2.5 pro"), _rule(1_048_576, 16_000)),
    (
        ("gemini-2.5-flash-lite", "gemini 2.5 flash-lite"),
        _rule(1_048_576, 16_000),
    ),
    (("gemini-2.5-flash", "gemini 2.5 flash"), _rule(1_048_576, 16_000)),
    # -- DeepSeek --
    (("deepseek-reasoner",), _rule(128_000, 16_000)),
    (("deepseek-chat",), _rule(128_000, 4_000)),
    # -- MiniMax --
    (("minimax-m3",), _rule(1_000_000, 16_000)),
    (("minimax-m2.7",), _rule(204_800, 16_000)),
)


def get_model_context_window(model: str) -> ModelContextWindow:
    """按子串匹配线性查找。顺序即优先级。"""
    normalized = (model or "").strip().lower()
    for patterns, window in MODEL_CONTEXT_RULES:
        if any(pattern in normalized for pattern in patterns):
            return window
    return UNKNOWN_MODEL_CONTEXT
