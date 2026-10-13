"""诊断 TUI 的输入通道 —— 在真实终端里跑这个，把输出发给我。

它逐项检查「按键为什么读不到」，并给出结论。

用法：
    python tools/tui_doctor.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def main() -> int:
    section("1. 基本环境")
    print(f"  os.name            = {os.name}")
    print(f"  sys.platform       = {sys.platform}")
    print(f"  Python             = {sys.version.split()[0]}")
    print(f"  TERM               = {os.environ.get('TERM')!r}")
    print(f"  TERM_PROGRAM       = {os.environ.get('TERM_PROGRAM')!r}")
    print(f"  WT_SESSION         = {os.environ.get('WT_SESSION')!r}  (Windows Terminal 会有值)")
    print(f"  MSYSTEM            = {os.environ.get('MSYSTEM')!r}  (Git Bash / MSYS2 会有值)")
    print(f"  NO_COLOR           = {os.environ.get('NO_COLOR')!r}")

    section("2. 流是否为终端")
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            isatty = stream.isatty()
        except Exception as error:  # noqa: BLE001
            isatty = f"<{type(error).__name__}>"
        encoding = getattr(stream, "encoding", None)
        print(f"  {name:6} isatty={isatty}  encoding={encoding}")

    section("3. Windows 控制台句柄（**关键**）")
    if os.name != "nt":
        print("  非 Windows，跳过")
    else:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetConsoleMode.restype = ctypes.c_int

        for label, which in (("STDIN", -10), ("STDOUT", -11)):
            handle = kernel32.GetStdHandle(which & 0xFFFFFFFF)
            mode = ctypes.c_uint32()
            ok = bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
            print(f"  {label:6} handle={handle}  是控制台={ok}"
                  + (f"  mode=0x{mode.value:04X}" if ok else ""))

        try:
            import msvcrt

            os_handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            mode = ctypes.c_uint32()
            ok = bool(
                kernel32.GetConsoleMode(ctypes.c_void_p(os_handle), ctypes.byref(mode))
            )
            print(f"  msvcrt.get_osfhandle(0) = {os_handle}  是控制台={ok}")
            print()
            if ok:
                print("  => 结论：stdin 是真正的控制台，msvcrt 按键接口应当可用")
            else:
                print("  => 结论：**stdin 不是控制台**。")
                print("     msvcrt.kbhit() 会永远返回 False —— 这正是「按键无反应」的原因。")
                print("     常见于：Git Bash / MSYS2 / Cygwin、或 stdin 被重定向。")
        except Exception as error:  # noqa: BLE001
            print(f"  msvcrt 探测失败: {type(error).__name__}: {error}")

    section("4. 我们自己的判定结果")
    from minicode.tui.terminal import (
        Terminal,
        enable_windows_vt,
        is_real_windows_console,
        is_tty,
    )

    print(f"  is_tty()                     = {is_tty()}")
    print(f"  is_real_windows_console(-10) = {is_real_windows_console(-10)}  (stdin 是真控制台？)")
    print(f"  is_real_windows_console(-11) = {is_real_windows_console(-11)}  (stdout 是真控制台？)")
    print(f"  enable_windows_vt()          = {enable_windows_vt()}")
    terminal = Terminal()
    print(f"  Terminal.interactive         = {terminal.interactive}")
    print(f"  Terminal.ansi                = {terminal.ansi}")
    print(f"  Terminal._use_msvcrt         = {terminal._use_msvcrt}  (是否走「无需回车」快路径)")
    print()
    print(f"  输入通道自检: {terminal.input_diagnosis()!r}")

    section("5. 实际读一个键（请按任意键，5 秒超时）")
    terminal.interactive = True
    print("  等待按键…（方向键、字母、Esc 都试试）")
    deadline = time.monotonic() + 5.0
    seen: list[str] = []
    while time.monotonic() < deadline and len(seen) < 3:
        key = terminal.read_key()
        if key is not None:
            seen.append(key)
            print(f"    读到: {key!r}")
        time.sleep(0.02)
    terminal.stop_reader()

    section("6. 结论")
    if seen:
        print("  ✅ 输入通道正常，按键能被读到。")
        print("     若 TUI 里仍无反应，问题在渲染循环而非取键 —— 请把上面的")
        print("     完整输出发给我。")
    else:
        print("  ❌ 没有读到任何按键。")
        print()
        print("  解决办法（按推荐顺序）：")
        print("    1. 换用 Windows Terminal 或 PowerShell 运行（不要用 Git Bash / MSYS2）")
        print("    2. 直接用行模式：minicode   （功能完整，只少全屏界面）")
        print()
        print("  如果上面第 3 节显示「是控制台=False」，那就是环境问题：")
        print("  msvcrt 的按键接口在这个环境下不可用。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
