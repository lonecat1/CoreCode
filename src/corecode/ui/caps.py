"""终端能力探测 —— 决定「能不能做动画」。

## 为什么单独一层

动画是**可选增强**，绝不能成为可用性的前提。本项目此前反复失败于
「把可选能力当成必需能力」：全屏 TUI 依赖 raw mode 与读键，在 Git Bash 下
按键永远读不到，界面看起来正常却完全不响应。

因此这里的每个判断都是**保守的**：只要有任何不确定，就返回「不支持」，
退回静态输出。宁可没有动画，也不要出现乱码或卡住的界面。

## 三个条件

  1. **目标流是终端**。重定向到文件时输出 `\\r` 会把文件内容搅乱。
  2. **不是 `TERM=dumb`**，且未被 `NO_COLOR` 关闭
     ——设置它的用户通常想要最小化输出，动画也属于要省掉的部分。
  3. **Windows 上需要启用 VT 处理**，否则 `ESC[<n>A` 会以字面量打印出来。

第 3 点是 Windows 上最容易漏的一步：Windows 10 起支持 ANSI，但**默认关闭**。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any

#: ENABLE_VIRTUAL_TERMINAL_PROCESSING
_VT_FLAG = 0x0004

#: 显式覆盖：`None` = 按环境判断，`False` = 强制关闭动画。
_animation_override: bool | None = None


def set_animation_enabled(enabled: bool | None) -> None:
    """显式开关动画（CLI 的 `--no-animation` 用它）。"""
    global _animation_override
    _animation_override = enabled


def enable_virtual_terminal() -> bool:
    """在 Windows 控制台上启用 ANSI（VT）处理。返回是否可用。

    非 Windows 上恒为 True（终端本来就懂 ANSI）。

    **句柄必须声明为指针宽度**：`ctypes` 默认 `restype` 是 `c_int`（32 位），
    64 位 Windows 上会把控制台句柄截断，`SetConsoleMode` 静默失败，
    于是动画的转义序列会以 `←[1A` 这类形式原样打印出来。
    """
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetConsoleMode.restype = ctypes.c_int

        handle = kernel32.GetStdHandle(-11 & 0xFFFFFFFF)  # STD_OUTPUT_HANDLE
        if not handle:
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        if mode.value & _VT_FLAG:
            return True
        return bool(
            kernel32.SetConsoleMode(ctypes.c_void_p(handle), mode.value | _VT_FLAG)
        )
    except Exception:  # noqa: BLE001
        return False


def is_tty(stream: Any = None) -> bool:
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def supports_animation(stream: Any = None, *, require_vt: bool = False) -> bool:
    """这个流能否安全地做原地动画。

    `require_vt=True` 时额外要求 VT 可用——**需要光标移动的动画必须传它**。
    只用 `\\r` 的动画（转圈）不依赖 VT，可以放宽。
    """
    if _animation_override is False:
        return False
    if _animation_override is None:
        if os.environ.get("NO_COLOR"):
            return False
        if (os.environ.get("TERM") or "").strip().lower() == "dumb":
            return False
        if os.environ.get("CORECODE_NO_ANIMATION"):
            return False

    if not is_tty(stream):
        return False

    if require_vt and not enable_virtual_terminal():
        return False
    return True
