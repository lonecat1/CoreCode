"""TUI 运行循环 —— 把界面接到会话宿主上。

## 线程模型

回合在**后台线程**里跑，主线程只负责「读键 → 改状态 → 重绘」。这样模型思考
的几秒里界面仍然能响应翻页与中断——不然 TUI 相比行模式就只多了颜色。

跨线程地更新同一个 `AppState` 会踩数据竞争，因此所有来自回合线程的输出都
经过一把锁 + 一个待处理队列，只在主线程的重绘点并入状态。**回合线程永远
不直接写终端**。

## 为什么 `run_tui` 这么短

因为界面逻辑都在 `app.py`（纯函数）和 `frames.py`（纯函数）里。这里只剩
「接线」：读键、派发、收尾。无法在自动化环境里验证的代码越少越好。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .app import AppState, BannerStats, compose_frame, handle_key
from .terminal import (
    KEY_CTRL_C,
    KEY_CTRL_D,
    Terminal,
    TerminalSize,
    detect_size,
    is_tty,
)


@dataclass
class HostBridge:
    """回合执行与本地命令的注入点。

    刻意用回调而不是直接依赖 `SessionHost`：这样运行循环可以脱离真实宿主
    单独测试，也避免 `tui` 包反向依赖整个应用层。
    """

    run_turn: Callable[[str], None]
    run_local: Callable[[str], str | None]
    is_busy: Callable[[], bool] = lambda: False
    summary: Callable[[], BannerStats] | None = None


@dataclass
class UiEvents:
    """来自回合线程的输出。用锁保护，主线程在重绘点取走。"""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: list[tuple[str, str, bool | None]] = field(default_factory=list)
    _finished: bool = False

    def push(self, kind: str, text: str, ok: bool | None = None) -> None:
        with self._lock:
            self._pending.append((kind, text, ok))

    def mark_finished(self) -> None:
        with self._lock:
            self._finished = True

    def drain(self) -> tuple[list[tuple[str, str, bool | None]], bool]:
        with self._lock:
            items = self._pending
            self._pending = []
            finished = self._finished
            self._finished = False
            return items, finished


def run_tui(
    state: AppState,
    bridge: HostBridge,
    *,
    terminal: Terminal | None = None,
    max_iterations: int | None = None,
) -> int:
    """交互式循环。返回进程退出码。

    `max_iterations` 只给测试用——真实交互不设上限。
    """
    term = terminal or Terminal()
    if not term.interactive:
        return -1  # 调用方应回退到行模式

    events = UiEvents()
    worker: threading.Thread | None = None
    running = False
    iterations = 0

    def apply_events() -> None:
        nonlocal running
        items, finished = events.drain()
        for kind, text, ok in items:
            if kind == "tool_start":
                state.push("tool", text, ok=None)
            elif kind == "tool_result":
                # 尝试收尾对应的「运行中」条目；找不到就单开一条。
                if not state.update_running_tool(text, ok):
                    state.push("tool", text, ok=ok)
            else:
                state.push(kind, text)
        if finished:
            running = False
            state.running = False
            state.status = None
        if bridge.summary is not None:
            state.banner = bridge.summary()

    term.enable_raw_mode()
    term.enter()
    try:
        while True:
            apply_events()
            size = detect_size()
            frame = compose_frame(state, size, mcp_waiting=running and not state.status)
            term.render(frame, size)

            key = term.read_key()
            if key is None:
                # 非阻塞读取拿到空：让出 CPU，避免忙等烧满一核。
                threading.Event().wait(0.03)
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    return 0
                continue

            iterations += 1
            # Ctrl-C 在回合进行中是「中断回合」，不是「退出程序」。
            if key == KEY_CTRL_C and running:
                state.status = "正在中断…"
                continue

            outcome = handle_key(state, key, size)
            if outcome.quit:
                return 0
            if outcome.interrupt:
                state.status = "已取消输入"
                continue
            if outcome.submit is not None:
                _dispatch(state, bridge, events, outcome.submit, running)
                running = running or bridge.is_busy()
                if running:
                    state.status = "思考中…"

            if max_iterations is not None and iterations >= max_iterations:
                return 0
    finally:
        # 无论如何都要恢复终端：留在一个没有光标的交替屏幕里是最糟的失败方式。
        term.disable_raw_mode()
        term.leave()


def _dispatch(
    state: AppState,
    bridge: HostBridge,
    events: UiEvents,
    text: str,
    running: bool,
) -> None:
    """处理一条提交的输入。"""
    state.push("user", text)

    if text.startswith("/"):
        reply = bridge.run_local(text)
        if reply is not None:
            state.push("notice", reply)
            return
        if text.strip() in ("/exit", "/quit"):
            return

    if running:
        state.push("error", "上一个回合还在运行。用 Ctrl-C 中断它。")
        return

    def work() -> None:
        try:
            bridge.run_turn(text)
        except Exception as error:  # noqa: BLE001 - 回合失败要显示而不是崩掉界面
            events.push("error", f"回合失败：{error}")
        finally:
            events.mark_finished()

    threading.Thread(target=work, name="minicode-turn", daemon=True).start()


def build_banner_stats(host: Any) -> BannerStats:
    """从会话宿主收集横幅数据。

    放在这里而不是 `app.py`：`app.py` 不该知道宿主的存在（那是它可测的原因）。
    """
    stats = BannerStats()

    runtime = getattr(host, "runtime_config", None)
    stats.model = getattr(runtime, "model", "") or "未配置（离线 mock）"
    stats.session_id = getattr(host, "session_id", "")
    stats.cwd = getattr(host, "cwd", "")

    messages = getattr(host, "messages", []) or []
    stats.message_count = len(messages)
    stats.transcript_count = sum(
        1 for m in messages if m.get("role") == "assistant_tool_call"
    )

    tools = getattr(host, "tools", None)
    if tools is not None:
        try:
            stats.skill_count = len(tools.skills())
        except Exception:  # noqa: BLE001
            stats.skill_count = 0
        try:
            servers = tools.mcp_servers()
            stats.mcp_total = len(servers)
            stats.mcp_connected = sum(
                1 for s in servers if s.get("status") == "connected"
            )
            stats.mcp_connecting = sum(
                1 for s in servers if s.get("status") == "connecting"
            )
            stats.mcp_error = sum(1 for s in servers if s.get("status") == "error")
        except Exception:  # noqa: BLE001
            pass

    return stats


def make_terminal_events(events: UiEvents) -> dict[str, Callable[..., None]]:
    """构造给 `HostCallbacks` 用的回调集合。"""
    return {
        "on_assistant": lambda text, is_final: events.push("assistant", text),
        "on_progress": lambda text: events.push("progress", text),
        "on_tool_start": lambda name, data: events.push("tool_start", str(name)),
        "on_tool_result": lambda name, output, is_error: events.push(
            "tool_result", str(name), not is_error
        ),
        "on_notice": lambda text: events.push("notice", text),
    }
