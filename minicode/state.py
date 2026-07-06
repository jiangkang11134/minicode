"""Zustand 风格的状态管理，用于 MiniCode Python。

提供简单、可预测的状态容器：
- 通过 updater 函数实现不可变更新
- 状态变更时通知订阅者
- 类型安全的泛型存储
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store(Generic[T]):
    """Zustand 风格的状态管理器。

    提供可预测的状态更新和订阅者通知机制。
    受 Claude Code 的 Zustand store 实现启发。
    """
    def __init__(
        self,
        initial_state: T,
        on_change: Callable[[T, T], None] | None = None,
    ):
        """使用初始状态初始化 Store。

        参数:
            initial_state: 初始状态值
            on_change: 可选的回调，在状态变更时被调用（参数为新旧状态）
        """
        self._state = initial_state
        self._listeners: list[Callable[[], None]] = []
        self._on_change = on_change
        self._update_count = 0

    def get_state(self) -> T:
        """获取当前状态。"""
        return self._state

    def set_state(self, updater: Callable[[T], T]) -> None:
        """使用 updater 函数更新状态。

        updater 接收当前状态并返回新状态。如果新旧状态引用相同
        （返回同一对象），则跳过更新。

        参数:
            updater: 接收当前状态并返回新状态的函数
        """
        prev = self._state
        next_state = updater(prev)

        # 跳过空更新（状态引用未变）
        if next_state is prev:
            return

        # 调用变更回调
        if self._on_change:
            self._on_change(next_state, prev)

        self._state = next_state
        self._update_count += 1

        # 通知所有订阅者
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                # 防止订阅者的异常中断状态更新
                pass

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """订阅状态变更通知。

        参数:
            listener: 状态变更时调用的回调函数

        返回:
            取消订阅的函数
        """
        self._listeners.append(listener)

        def unsubscribe():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    @property
    def update_count(self) -> int:
        """获取状态更新的总次数。"""
        return self._update_count

    @property
    def subscriber_count(self) -> int:
        """获取当前活跃的订阅者数量。"""
        return len(self._listeners)


# ---------------------------------------------------------------------------
# AppState
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    """全局应用程序状态。

    受 Claude Code 的 AppState 类型启发，包含会话信息、
    上下文追踪、成本追踪、任务追踪、UI 状态和功能标记等。
    """
    # 会话信息
    session_id: str = ""
    workspace: str = ""
    model: str = "unknown"

    # 上下文追踪
    message_count: int = 0
    tool_call_count: int = 0
    token_usage: int = 0
    context_window_size: int = 128_000
    context_usage_percentage: float = 0.0

    # 成本追踪
    total_cost_usd: float = 0.0
    api_calls: int = 0
    api_errors: int = 0

    # 任务追踪
    active_tasks: int = 0
    completed_tasks: int = 0

    # UI 状态
    is_busy: bool = False
    active_tool: str | None = None
    status_message: str = ""

    # 功能标记
    verbose: bool = False
    skills_enabled: bool = True
    mcp_enabled: bool = True

    # 时间戳
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    # 自定义元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def update_timestamp(self) -> None:
        """更新 last_updated 时间戳为当前时间。"""
        self.last_updated = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_app_store(
    initial: dict[str, Any] | None = None,
    on_change: Callable[[AppState, AppState], None] | None = None,
) -> Store[AppState]:
    """创建一个新的 AppState 状态管理器。

    参数:
        initial: 可选的初始状态覆盖值（字典形式）
        on_change: 可选的状态变更回调

    返回:
        Store[AppState] 实例
    """
    state = AppState()
    if initial:
        for key, value in initial.items():
            if hasattr(state, key):
                setattr(state, key, value)

    return Store(state, on_change)


def format_app_state_summary(state: AppState) -> str:
    """将应用程序状态格式化为人类可读的摘要。

    参数:
        state: 当前的 AppState

    返回:
        格式化后的摘要字符串
    """
    lines = [
        "Application State",
        "=" * 50,
        "",
        "Session:",
        f"  ID: {state.session_id[:8] if state.session_id else 'new'}",
        f"  Model: {state.model}",
        f"  Workspace: {state.workspace}",
        "",
        "Context:",
        f"  Messages: {state.message_count}",
        f"  Tool calls: {state.tool_call_count}",
        f"  Tokens: {state.token_usage:,} / {state.context_window_size:,} "
        f"({state.context_usage_percentage:.1f}%)",
        "",
        "Cost:",
        f"  Total: ${state.total_cost_usd:.4f}",
        f"  API calls: {state.api_calls}",
        f"  API errors: {state.api_errors}",
        "",
        "Tasks:",
        f"  Active: {state.active_tasks}",
        f"  Completed: {state.completed_tasks}",
        "",
        "Status:",
        f"  Busy: {'Yes' if state.is_busy else 'No'}",
        f"  Active tool: {state.active_tool or 'none'}",
        f"  Message: {state.status_message or 'ready'}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State updaters (helper functions)
# ---------------------------------------------------------------------------

def update_message_count(count: int) -> Callable[[AppState], AppState]:
    """创建一个设置消息数量的 updater。

    参数:
        count: 要设置的消息数量

    返回:
        AppState updater 函数
    """
    def updater(state: AppState) -> AppState:
        state.message_count = count
        state.update_timestamp()
        return state
    return updater


def increment_tool_calls() -> Callable[[AppState], AppState]:
    """创建一个增加工具调用计数的 updater。"""
    def updater(state: AppState) -> AppState:
        state.tool_call_count += 1
        state.update_timestamp()
        return state
    return updater


def update_context_usage(
    tokens: int,
    window_size: int | None = None,
) -> Callable[[AppState], AppState]:
    """创建一个更新上下文使用率的 updater。

    参数:
        tokens: 当前 token 使用量
        window_size: 可选的上下文窗口大小，传入时会更新窗口大小

    返回:
        AppState updater 函数
    """
    def updater(state: AppState) -> AppState:
        state.token_usage = tokens
        if window_size is not None:
            state.context_window_size = window_size
        if state.context_window_size > 0:
            state.context_usage_percentage = (
                tokens / state.context_window_size * 100
            )
        state.update_timestamp()
        return state
    return updater


def add_cost(cost_usd: float) -> Callable[[AppState], AppState]:
    """创建一个累加成本的 updater。

    参数:
        cost_usd: 要添加的成本（美元）

    返回:
        AppState updater 函数
    """
    def updater(state: AppState) -> AppState:
        state.total_cost_usd += cost_usd
        state.api_calls += 1
        state.update_timestamp()
        return state
    return updater


def record_api_error() -> Callable[[AppState], AppState]:
    """创建一个记录 API 错误的 updater。"""
    def updater(state: AppState) -> AppState:
        state.api_errors += 1
        state.api_calls += 1
        state.update_timestamp()
        return state
    return updater


def set_busy(tool_name: str | None = None) -> Callable[[AppState], AppState]:
    """创建一个将状态设为忙碌的 updater。

    参数:
        tool_name: 可选的正在使用的工具名称

    返回:
        AppState updater 函数
    """
    def updater(state: AppState) -> AppState:
        state.is_busy = True
        state.active_tool = tool_name
        state.status_message = f"Running {tool_name}..." if tool_name else "Working..."
        state.update_timestamp()
        return state
    return updater


def set_idle() -> Callable[[AppState], AppState]:
    """创建一个将状态设回空闲的 updater。"""
    def updater(state: AppState) -> AppState:
        state.is_busy = False
        state.active_tool = None
        state.status_message = "Ready"
        state.update_timestamp()
        return state
    return updater


# ---------------------------------------------------------------------------
# Global store singleton (merged from state_integration.py)
# ---------------------------------------------------------------------------

_global_store: Store[AppState] | None = None


def get_global_store() -> Store[AppState]:
    """获取或创建全局 Store 单例。"""
    global _global_store
    if _global_store is None:
        _global_store = create_app_store()
    return _global_store


def set_global_store(store: Store[AppState]) -> None:
    """设置全局 Store 实例。

    参数:
        store: Store[AppState] 实例
    """
    global _global_store
    _global_store = store


def handle_state_command() -> str:
    """处理 /state 斜杠命令，返回格式化的当前状态摘要。"""
    return format_app_state_summary(get_global_store().get_state())
