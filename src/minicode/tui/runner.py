"""TUI 运行循环 —— 内联实时区域。

## 线程模型

回合在**后台线程**里跑，主线程只负责「读键 → 改状态 → 重绘」。这样模型思考
的几秒里界面仍然响应翻页与中断——不然 TUI 相比行模式就只多了颜色。

跨线程更新同一个 `AppState` 会踩数据竞争，因此所有来自回合线程的输出都经过
一把锁 + 一个待处理队列，只在主线程的重绘点并入状态。**回合线程永远不直接
写终端。**

## 降级

`Terminal.interactive` 为假时返回 `-1`，调用方回退到行模式。因此
`minicode --tui` 在管道/CI 里仍然可用。

## 退出时等待收尾

按 Ctrl-D 退出时先等后台回合结束（有上限），否则「提交提问 → 立刻 Ctrl-D」
会丢掉正在生成的回答：主循环返回、进程退出、daemon 线程被连带杀掉。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .app import AppState, BannerStats, compose_region, handle_key
from .terminal import KEY_CTRL_C, KEY_CTRL_D, Terminal, TerminalSize, detect_size

#: 无按键时的重绘间隔（秒）。30ms 约等于 33 帧/秒，转圈动画足够顺，
#: 又不至于让 CPU 空转。
_FRAME_INTERVAL = 0.03

#: 退出时等待后台回合的上限（秒）。超时就放弃——不能让用户按了 Ctrl-D
#: 之后卡在一个无响应的界面里。
_EXIT_WAIT_SECONDS = 30.0

#: 输入通道保底时间（秒）。自检可疑时，若这么久都没收到任何按键，
#: 就主动退回行模式。10 秒足够让用户意识到「按了没反应」并放弃。
_INPUT_DEADMAN_SECONDS = 10.0


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
    _pending: list[tuple[str, str, bool | None, str]] = field(default_factory=list)
    _finished: bool = False
    #: 最近一次提交启动的回合线程。退出时用它等待收尾。
    current_worker: threading.Thread | None = None

    def push(
        self, kind: str, text: str, ok: bool | None = None, detail: str = ""
    ) -> None:
        with self._lock:
            self._pending.append((kind, text, ok, detail))

    def mark_finished(self) -> None:
        with self._lock:
            self._finished = True

    def drain(self) -> tuple[list[tuple[str, str, bool | None, str]], bool]:
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
    """交互式循环。返回进程退出码；`-1` 表示「请调用方回退到行模式」。"""
    term = terminal or Terminal()
    if not term.interactive:
        return -1

    events = UiEvents()
    running = False
    iterations = 0

    # 输入通道自检。坏掉时的表现是「按键完全没反应」，用户会以为程序卡死，
    # 完全不知道是环境问题。提前把原因摆在界面上，比让人对着不响应的界面
    # 反复重试要好得多。
    diagnosis = term.input_diagnosis()
    input_suspect = diagnosis is not None
    if input_suspect:
        state.push("error", f"输入通道可能不可用：\n{diagnosis}")

    #: 是否曾成功读到过按键；以及首帧时间。二者用于「输入通道保底」：
    #: 自检可疑且若干秒内一个键都没收到，就主动退回行模式——
    #: 绝不能把用户留在一个不响应的界面里。
    received_any_key = False
    started_at = time.monotonic()

    def apply_events() -> None:
        nonlocal running
        items, finished = events.drain()
        for kind, text, ok, detail in items:
            if kind == "tool_start":
                state.push("tool", text, ok=None)
            elif kind == "tool_result":
                # 收尾对应的「运行中」条目；找不到就单开一条。
                if not state.update_running_tool(text, ok, detail):
                    state.push("tool", text, ok=ok, detail=detail)
            else:
                state.push(kind, text, detail=detail)
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
            size = term.size()
            region = compose_region(state, size.columns, size.rows)
            term.render_region(
                region.lines,
                cursor_row=region.cursor_row,
                cursor_column=region.cursor_column,
            )

            key = term.read_key()
            if key is None:
                # 没有按键：让出 CPU 一小会儿，同时推进转圈动画。
                state.animation_frame += 1
                time.sleep(_FRAME_INTERVAL)
                iterations += 1
                # 保底：自检可疑且迟迟收不到任何按键 → 主动退出，让调用方
                # 回退到行模式。用户至少能继续干活，而不是对着死界面。
                if (
                    input_suspect
                    and not received_any_key
                    and time.monotonic() - started_at > _INPUT_DEADMAN_SECONDS
                ):
                    term.leave()
                    return -1
                if max_iterations is not None and iterations >= max_iterations:
                    return 0
                continue

            received_any_key = True
            iterations += 1
            # Ctrl-C 在回合进行中是「中断回合」，不是「退出程序」。
            if key == KEY_CTRL_C and running:
                state.status = "正在中断…"
                continue

            outcome = handle_key(state, key, size)
            if outcome.quit:
                _wait_for_worker(events, state)
                return 0
            if outcome.interrupt:
                state.status = "已取消输入"
                continue
            if outcome.submit is not None:
                # 首条消息之后收起启动横幅，把空间让给对话。
                state.show_banner = False
                _dispatch(state, bridge, events, outcome.submit, running)
                running = running or bridge.is_busy()
                if running:
                    state.status = "思考中…"

            if max_iterations is not None and iterations >= max_iterations:
                return 0
    finally:
        # 无论如何都要恢复终端：留在没有光标的终端里是最糟的失败方式。
        term.disable_raw_mode()
        term.leave()


def _wait_for_worker(events: UiEvents, state: AppState) -> None:
    """等待后台回合收尾，避免丢掉正在生成的回答。"""
    worker = events.current_worker
    if worker is None or not worker.is_alive():
        return
    state.status = "正在收尾…"
    worker.join(_EXIT_WAIT_SECONDS)


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
        stripped = text.strip()
        reply = bridge.run_local(text)
        if reply is not None:
            state.push("notice", reply)
            return
        # `run_local` 返回 None 表示「不是可处理的本地命令」。
        #
        # 这里必须**明确报错并停止**，不能落到下面的模型回合：行模式下未知
        # 斜杠命令会提示「未知命令」，若 TUI 把它当普通提问发给模型，两种模式
        # 行为就不一致了，而且模型会对着一串命令瞎猜。
        state.push("error", f"未知命令：{stripped}。输入 /help 查看可用命令。")
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

    thread = threading.Thread(target=work, name="minicode-turn", daemon=True)
    events.current_worker = thread
    thread.start()


def build_banner_stats(host: Any) -> BannerStats:
    """从会话宿主收集横幅数据。

    放在这里而不是 `app.py`：`app.py` 不该知道宿主的存在（那是它可测的原因）。
    """
    stats = BannerStats()

    runtime = getattr(host, "runtime_config", None)
    stats.model = getattr(runtime, "model", "") or "离线 mock"
    stats.session_id = getattr(host, "session_id", "")
    stats.cwd = getattr(host, "cwd", "")

    messages = getattr(host, "messages", []) or []
    stats.message_count = len(messages)

    tools = getattr(host, "tools", None)
    if tools is not None:
        try:
            stats.tool_count = len(tools.list())
        except Exception:  # noqa: BLE001
            stats.tool_count = 0
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
            stats.mcp_error = sum(1 for s in servers if s.get("status") == "error")
        except Exception:  # noqa: BLE001
            pass

    # 上下文占用：从最近的 provider usage 推算，失败就不显示。
    try:
        from ..context.tokens import compute_context_stats

        if runtime is not None and messages:
            computed = compute_context_stats(messages, runtime.model)
            stats.context_percent = round(computed.utilization * 100)
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
