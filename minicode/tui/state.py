from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from minicode.cost_tracker import CostTracker
from minicode.permissions import PermissionManager
from minicode.session import AutosaveManager, SessionData
from minicode.state import AppState, Store
from minicode.tooling import ToolRegistry
from minicode.tui.types import TranscriptEntry
from minicode.types import ChatMessage, ModelAdapter


@dataclass
class TtyAppArgs:
    """TTY 应用的命令行参数和运行依赖的聚合容器。

    汇集所有在 TUI 运行期间需要传递的配置对象、工具注册表、消息列表
    以及可选的 memory/context/prompt/产品快照等扩展模块。
    """

    runtime: dict | None
    tools: ToolRegistry
    model: ModelAdapter
    messages: list[ChatMessage]
    cwd: str
    permissions: PermissionManager
    memory_manager: Any | None = None
    context_manager: Any | None = None
    prompt_bundle: Any | None = None
    product_snapshot: dict[str, Any] | None = None


@dataclass
class PendingApproval:
    """用户待审批操作的上下文状态。

    存储权限审批请求的原始数据、回调函数以及用户在审批界面中的
    交互状态（展开/滚动/选择/反馈）。
    """

    request: dict[str, Any]
    resolve: Callable[[dict[str, Any]], None]
    details_expanded: bool = False
    details_scroll_offset: int = 0
    selected_choice_index: int = 0
    feedback_mode: bool = False
    feedback_input: str = ""


@dataclass
class AggregatedEditProgress:
    """聚合的文件编辑进度跟踪。

    用于在 TUI 界面中展示某个工具对单个文件进行多次 Edit 操作时的
    总体进度：总次数、已完成次数、出错次数和最后一次输出。
    """

    entry_id: int
    tool_name: str
    path: str
    total: int = 1
    completed: int = 0
    errors: int = 0
    last_output: str = ""


@dataclass
class ScreenState:
    """TUI 屏幕的完整可变状态。

    涵盖输入缓冲区、转录（transcript）滚动状态、命令历史、待审批请求、
    会话信息、应用级 Store、成本追踪器等所有界面状态字段。
    """

    input: str = ""
    cursor_offset: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    transcript_scroll_offset: int = 0
    transcript_revision: int = 0
    selected_slash_index: int = 0
    status: str | None = None
    active_tool: str | None = None
    recent_tools: list[dict[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    history_index: int = 0
    history_draft: str = ""
    next_entry_id: int = 1
    pending_approval: PendingApproval | None = None
    is_busy: bool = False
    session: SessionData | None = None
    autosave: AutosaveManager | None = None
    app_state: Store[AppState] | None = None
    cost_tracker: CostTracker | None = None
    agent_thread: Any = None
    agent_result: dict | None = None
    agent_lock: Any = None
    tool_start_time: float | None = None
