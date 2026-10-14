"""后台 shell 任务的注册表。

对应原版 `MiniCode/src/background-tasks.ts`。

## 这个模块解决什么

`run_command` 支持以 `&` 结尾的 shell 片段——它不等待命令结束，立刻返回一个
任务句柄。但「已启动」和「还在跑」是两件事：后台进程可能早就退出了。因此需要
一个注册表记住它们，并能**查询进程是否还活着**。

## Windows 上的一个致命陷阱

检查进程是否存活，POSIX 的惯用做法是 `os.kill(pid, 0)`。

**在 Windows 上绝不能这样做。** Python 文档明确写着：Windows 下 `os.kill` 对
除了 `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` 之外的任何信号，都会**无条件调用
`TerminateProcess` 杀掉目标进程**。

也就是说 `os.kill(pid, 0)` 在 Windows 上不是「探测」，而是「击杀」——它会
直接杀掉用户刚启动的后台任务。这个错误很隐蔽：代码看起来完全正常，
只在 Windows 上表现为「后台任务莫名其妙就 completed/failed 了」。

因此 Windows 分支改用 `OpenProcess` + `GetExitCodeProcess`，这是真正的只读探测。
"""

from __future__ import annotations

import ctypes
import os
import random
import string
import threading
import time
from dataclasses import replace

from .tooling import BackgroundTaskResult

__all__ = [
    "get_background_task",
    "list_background_tasks",
    "register_background_shell_task",
    "reset_background_tasks",
    "is_process_alive",
]

#: 任务表。用锁保护——后台线程与主线程都会碰它。
_TASKS: dict[str, BackgroundTaskResult] = {}
_LOCK = threading.Lock()

_BASE36 = string.digits + string.ascii_lowercase

#: Windows 上「进程仍在运行」的退出码。
_STILL_ACTIVE = 259
#: OpenProcess 因权限不足失败——这**不代表进程不存在**，反而说明它活着。
_ERROR_ACCESS_DENIED = 5


def _base36(value: int) -> str:
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36[remainder])
    return "".join(reversed(digits))


def _make_task_id() -> str:
    """`shell_<base36 毫秒时间>_<6 位随机>`，与原版格式一致。"""
    stamp = _base36(int(time.time() * 1000))
    suffix = "".join(random.choices(_BASE36, k=6))
    return f"shell_{stamp}_{suffix}"


def is_process_alive(pid: int) -> bool | None:
    """进程是否还活着。**无法判断时返回 `None`**，而不是猜一个。

    返回 `None` 的情形（例如 Windows 上句柄打不开且不是权限问题）会让调用方
    保持原状态，而不是把任务错标成 completed/failed。
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_is_alive(pid)
    return _posix_is_alive(pid)


def _posix_is_alive(pid: int) -> bool | None:
    """POSIX：`os.kill(pid, 0)` 是纯探测，不会发送信号。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不是我们的进程。
        return True
    except OSError:
        return None
    return True


def _windows_is_alive(pid: int) -> bool | None:
    """Windows：只读探测，**绝不用 `os.kill`**（见模块文档）。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,  # dwDesiredAccess
        ctypes.c_int,  # bInheritHandle
        ctypes.c_uint32,  # dwProcessId
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    # PROCESS_QUERY_LIMITED_INFORMATION 在 XP 之后可用，且权限要求最低。
    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_ACCESS_DENIED:
            # 打不开是因为权限不足 → 进程存在。
            return True
        # 其余情况（典型是 ERROR_INVALID_PARAMETER）视为不存在。
        return False

    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _refresh(record: BackgroundTaskResult) -> BackgroundTaskResult:
    """把一次快照刷新到当前状态。

    只有 `running` 才需要探测——已结束的任务状态是终态，重复探测没有意义，
    而且如果 PID 被系统复用，反而会把一个已完成的任务错标回 running。
    """
    if record.status != "running":
        return record

    alive = is_process_alive(record.pid)
    if alive is None:
        return record  # 判断不了就保持原状，不乱改
    return replace(record, status="running" if alive else "completed")


def register_background_shell_task(
    *, command: str, pid: int, cwd: str
) -> BackgroundTaskResult:
    """登记一个刚启动的后台 shell 任务。"""
    task = BackgroundTaskResult(
        task_id=_make_task_id(),
        command=command,
        pid=pid,
        status="running",
        started_at=time.time(),
        type="local_bash",
        cwd=cwd,
    )
    with _LOCK:
        _TASKS[task.task_id] = task
    return task


def list_background_tasks() -> list[BackgroundTaskResult]:
    """列出全部后台任务（同时刷新存活状态）。"""
    with _LOCK:
        records = list(_TASKS.values())
    refreshed = [_refresh(record) for record in records]
    with _LOCK:
        for record in refreshed:
            _TASKS[record.task_id] = record
    return refreshed


def get_background_task(task_id: str) -> BackgroundTaskResult | None:
    with _LOCK:
        record = _TASKS.get(task_id)
    if record is None:
        return None
    refreshed = _refresh(record)
    with _LOCK:
        _TASKS[refreshed.task_id] = refreshed
    return refreshed


def reset_background_tasks() -> None:
    """清空任务表。**仅供测试使用**——它是一个进程级全局。"""
    with _LOCK:
        _TASKS.clear()
