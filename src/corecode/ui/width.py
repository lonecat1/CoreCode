"""显示宽度计算 —— 终端里「一个字符占几列」。

这是整个渲染层的地基：面板对齐、截断、换行全部依赖它。算错的后果是
边框错位、中文把面板撑破——而且错得很隐蔽，因为 ASCII 场景永远正确。

## 规则

  - 组合附加符号（U+0300–U+036F 等）宽度 0：它们叠在前一个字符上。
  - 东亚宽字符（CJK、假名、韩文、全角符号、Emoji）宽度 2。
  - 其余宽度 1。

## 与 JS 的一个差异

JS 用 `[...input]` 按**码点**迭代，Python 的 `str` 迭代本身就是码点，
因此两者一致。但 Python 没有 `codePointAt` 越界的概念，需注意空串。
"""

from __future__ import annotations

import re

#: ANSI SGR 序列（颜色/样式）。只在计算可见宽度时剥离。
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: 更宽的 ANSI 序列（光标移动、擦除等），用于「整串是否含控制序列」的判断。
_ANY_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: 组合附加符号：宽度 0。
_COMBINING_RANGES: tuple[tuple[int, int], ...] = (
    (0x0300, 0x036F),  # 组合用附加符号
    (0x0483, 0x0489),  # 西里尔组合符
    (0x0591, 0x05BD),  # 希伯来文注音
    (0x0610, 0x061A),  # 阿拉伯文注音
    (0x064B, 0x065F),
    (0x0670, 0x0670),
    (0x06D6, 0x06DC),
    (0x0E31, 0x0E31),  # 泰文
    (0x0E34, 0x0E3A),
    (0x0E47, 0x0E4E),
    (0x200B, 0x200F),  # 零宽字符与方向标记
    (0x20D0, 0x20F0),  # 组合用符号
    (0xFE00, 0xFE0F),  # 变体选择符
    (0xFE20, 0xFE2F),
)

#: 宽度 2 的范围（与原 TS 实现逐条对应）。
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),  # 韩文字母
    (0x2329, 0x232A),  # 尖括号
    (0x2E80, 0xA4CF),  # CJK 部首 … 注音（0x303F 除外）
    (0xAC00, 0xD7A3),  # 韩文音节
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0xFE10, 0xFE19),  # 竖排标点
    (0xFE30, 0xFE6F),  # CJK 兼容形式
    (0xFF00, 0xFF60),  # 全角形式
    (0xFFE0, 0xFFE6),  # 全角符号
    (0x1F300, 0x1FAF6),  # 各类 Emoji
    (0x20000, 0x3FFFD),  # CJK 扩展 B 及以后
)

#: `0x2E80–0xA4CF` 中的例外：U+303F 宽度为 1。
_WIDE_EXCEPTION = 0x303F

def char_display_width(char: str) -> int:
    """单个字符占几列。空串返回 0。"""
    if not char:
        return 0
    code = ord(char[0])

    if code == _WIDE_EXCEPTION:
        return 1

    for start, end in _COMBINING_RANGES:
        if start <= code <= end:
            return 0

    for start, end in _WIDE_RANGES:
        if start <= code <= end:
            return 2

    return 1

def strip_ansi(text: str) -> str:
    """去掉 SGR 颜色/样式序列。"""
    return _SGR.sub("", text)

def string_display_width(text: str) -> int:
    """可见宽度（忽略 ANSI 序列）。"""
    return sum(char_display_width(char) for char in strip_ansi(text))

def truncate_plain(text: str, width: int) -> str:
    """按**可见宽度**截断，超长时加 `...`。

    注意：截断发生在**含 ANSI 的原文**上，但宽度按可见字符算——
    因此不会把一个颜色序列切成两半（只按完整字符推进）。
    """
    if width <= 0:
        return ""
    if string_display_width(text) <= width:
        return text
    if width <= 3:
        return _hard_slice(text, width)

    target = width - 3
    return _hard_slice(text, target) + "..."

def _hard_slice(text: str, columns: int) -> str:
    """取前 `columns` 列的字符，不切断 ANSI 序列。"""
    out: list[str] = []
    used = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\x1b":
            # 整段跳过转义序列：它不占列，也不该被切断。
            match = _ANY_ESCAPE.match(text, index)
            if match:
                out.append(match.group(0))
                index = match.end()
                continue
        width = char_display_width(char)
        if used + width > columns:
            break
        out.append(char)
        used += width
        index += 1
    return "".join(out)

def pad_plain(text: str, width: int) -> str:
    """右补空格到指定可见宽度。已超出则原样返回。"""
    visible = string_display_width(text)
    if visible >= width:
        return text
    return text + " " * (width - visible)

def wrap_plain(text: str, width: int) -> list[str]:
    """按可见宽度折行，尽量在空格处断。空行保留。

    实现方式：先把段落切成**不可再分的单元**（宽字符单字成单元，ASCII 连着跑，
    空格独立成单元），再把单元装进行。当某个单元放不下时，回退到最近的空格
    单元，从它之后重新开始。

    「空行保留」是刻意的：段落分隔符承载信息。
    """
    if width <= 0:
        return [text] if text else [""]

    result: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            result.append("")
            continue
        result.extend(_wrap_paragraph(paragraph, width))
    return result

def _wrap_units(paragraph: str) -> list[str]:
    """切单元：宽字符一字一单元、空格一单元、其余连续成串。

    中文没有空格，只能按字断；ASCII 单词不该被拆开，因此要连着跑。
    """
    units: list[str] = []
    buffer = ""
    for char in paragraph:
        if char == " ":
            if buffer:
                units.append(buffer)
                buffer = ""
            units.append(" ")
        elif char_display_width(char) == 2:
            if buffer:
                units.append(buffer)
                buffer = ""
            units.append(char)
        else:
            buffer += char
    if buffer:
        units.append(buffer)
    return units

def _wrap_paragraph(paragraph: str, width: int) -> list[str]:
    """折行核心。

    以**词**为单位处理（宽字符则一词一字），而不是逐字符维护断点。
    逐字符维护「最近空格位置」很容易在「空格恰好是超限的那一个字符」时
    算错位置，进而吐出空行或把两个词粘在一起——我实际踩过两次。
    按词处理时，分隔空格是显式加回的，不存在位置漂移。
    """
    lines: list[str] = []
    current = ""
    current_width = 0

    for word in _wrap_units(paragraph):
        word_width = string_display_width(word)

        if word == " ":
            # 行尾空格不保留：它既不占内容，又会让宽度记账和实际不符。
            if current and not current.endswith(" "):
                current += " "
                current_width += word_width
            continue

        if current and current_width + word_width > width:
            lines.append(current.rstrip())
            current = ""
            current_width = 0

        # 单个词就超宽（超长单词或连续中文）：硬切。
        while word_width > width and not current:
            head = _hard_slice(word, width)
            if not head:
                break
            lines.append(head)
            word = word[len(head) :]
            word_width = string_display_width(word)

        current += word
        current_width += word_width

    if current.strip() or not lines:
        lines.append(current.rstrip())

    return lines

def contains_escape(text: str) -> bool:
    """文本里是否含任意 ANSI 转义序列。"""
    return bool(_ANY_ESCAPE.search(text))
