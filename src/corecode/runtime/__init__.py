"""运行时：把回合、Goal、Loop、Plan 编排成一个会话。

    plan.py            内存 Todo 清单
    goal.py            Goal 状态机（用户拥有目标，模型只能上报）
    goal_controller.py Goal 的跨回合自动推进
    loop.py            固定延迟的重复任务
    turn.py            单飞闸门（Goal 与 Loop 复用它，因此天然互斥）
    context.py         三种运行时状态的上下文/展示渲染
    session.py         SessionRuntime —— 宿主唯一面对的门面
"""

from .context import (
    build_goal_context,
    build_loop_context,
    format_goal,
    inject_runtime_context,
)
from .goal import (
    ACTION_BLOCKED,
    ACTION_COMPLETED,
    ACTION_SET_CRITERIA,
    GoalDocument,
    GoalError,
    GoalManager,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
)
from .goal_controller import CONTINUATION_TEXT, GoalController
from .loop import (
    DEFAULT_LOOP_INTERVAL_MS,
    MAX_LOOP_INTERVAL_MS,
    MIN_LOOP_INTERVAL_MS,
    LoopClock,
    LoopDocument,
    LoopError,
    LoopScheduler,
    parse_loop_request,
)
from .plan import (
    PlanDocument,
    PlanError,
    PlanManager,
    STATUS_COMPLETED as TODO_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    Todo,
    format_plan,
    inject_plan_context,
)
from .session import RuntimeError_, SessionRuntime
from .turn import TurnBusyError, TurnRequest, TurnRunner

__all__ = [
    "ACTION_BLOCKED",
    "ACTION_COMPLETED",
    "ACTION_SET_CRITERIA",
    "CONTINUATION_TEXT",
    "DEFAULT_LOOP_INTERVAL_MS",
    "GoalController",
    "GoalDocument",
    "GoalError",
    "GoalManager",
    "LoopClock",
    "LoopDocument",
    "LoopError",
    "LoopScheduler",
    "MAX_LOOP_INTERVAL_MS",
    "MIN_LOOP_INTERVAL_MS",
    "PlanDocument",
    "PlanError",
    "PlanManager",
    "RuntimeError_",
    "STATUS_ACTIVE",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_IN_PROGRESS",
    "STATUS_PAUSED",
    "STATUS_PENDING",
    "SessionRuntime",
    "TODO_COMPLETED",
    "Todo",
    "TurnBusyError",
    "TurnRequest",
    "TurnRunner",
    "build_goal_context",
    "build_loop_context",
    "format_goal",
    "format_plan",
    "inject_plan_context",
    "inject_runtime_context",
    "parse_loop_request",
]
