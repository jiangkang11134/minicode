"""SmartCode Python TTY 应用程序。 本模块实现了 SmartCode 的全屏终端用户界面，包括：
- 带有工具输出折叠功能的实时转录渲染
- 交互式权限批准提示
- 后台代理线程管理
- 键盘事件处理与命令路由
- 会话持久化与自动保存
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from minicode.permissions import PermissionManager
from minicode.tooling import ToolRegistry
from minicode.tui.chrome import _cached_terminal_size
from minicode.tui.event_flow import _handle_event as _handle_tty_event
from minicode.tui.input_handler import _handle_input, _RawModeContext, _win_read_one_key
from minicode.tui.input_parser import (
    KeyEvent,
    ParsedInputEvent,
    TextEvent,
    parse_input_chunk,
)
from minicode.tui.renderer import _render_screen
from minicode.tui.runtime_control import (
    _ThrottledRenderer,
    enter_tty_runtime,
    exit_tty_runtime,
    install_sigwinch_rerender,
)
from minicode.tui.session_flow import (
    build_tty_runtime_state,
    finalize_tty_session,
    handle_session_listing,
    install_permission_prompt,
    load_or_create_session,
)

# ---------------------------------------------------------------------------
from minicode.tui.state import ScreenState
from minicode.tui.tool_helpers import _apply_tool_result_visual_state as _shared_apply_tool_result_visual_state
from minicode.tui.tool_helpers import _mark_unfinished_tools as _shared_mark_unfinished_tools
from minicode.tui.tool_helpers import _save_transcript as _shared_save_transcript
from minicode.tui.tool_helpers import _summarize_collapsed_tool_body, _summarize_tool_input
from minicode.tui.types import TranscriptEntry
from minicode.types import ChatMessage, ModelAdapter

# Terminal size — use unified cache from chrome module
# ---------------------------------------------------------------------------

# Alias to the single canonical implementation in chrome.py
_get_terminal_size = _cached_terminal_size


# ---------------------------------------------------------------------------
# Main event-driven TTY app
# ---------------------------------------------------------------------------


def run_tty_app(
    *,
    runtime: dict | None,
    tools: ToolRegistry,
    model: ModelAdapter,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager,
    resume_session: str | None = None,
    list_sessions_only: bool = False,
    memory_manager: Any | None = None,
    context_manager: Any | None = None,
    prompt_bundle: Any | None = None,
    product_snapshot: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    """运行事件驱动的全屏 TTY 应用程序。

    该函数是 SmartCode 终端界面的主入口点。它从 TypeScript 版本移植而来，
    管理完整的终端生命周期，包括：初始化会话、渲染界面、
    处理用户输入、管理后台代理线程、处理权限请求，
    以及最终完成会话并返回更新后的消息列表。

    参数:
        runtime: 运行时配置字典，可为 None
        tools: 工具注册表，包含所有可用工具的定义
        model: 模型适配器，用于与大语言模型通信
        messages: 初始聊天消息列表
        cwd: 当前工作目录路径
        permissions: 权限管理器，处理用户对工具调用的审批
        resume_session: 要恢复的会话 ID，或 "latest" 表示最近一次会话
        list_sessions_only: 如果为 True，则仅打印会话列表后退出
        memory_manager: 内存管理器实例，用于管理长期记忆（可选）
        context_manager: 上下文管理器实例（可选）
        prompt_bundle: 提示词包，包含系统提示等（可选）
        product_snapshot: 产品快照字典（可选）

    返回:
        更新后的聊天消息列表，包含会话过程中产生的所有新消息
    """

    if handle_session_listing(cwd, list_sessions_only):
        return messages

    session = load_or_create_session(cwd, resume_session)
    args, state = build_tty_runtime_state(
        runtime,
        tools,
        model,
        messages,
        cwd,
        permissions,
        session,
        memory_manager,
        context_manager,
        prompt_bundle,
        product_snapshot,
    )

    # Throttled renderer: coalesces rapid rerender() calls to reduce flickering
    throttled = _ThrottledRenderer(lambda: _render_screen(args, state), min_interval=0.016)

    def rerender() -> None:
        throttled.request()

    approval_event, approval_result, _ = install_permission_prompt(args, state, rerender)

    input_remainder = ""
    should_exit = False
    # Autosave throttle: check at most every ~2 seconds, not every 20ms
    _autosave_counter = 0
    _AUTOSAVE_CHECK_INTERVAL = 100  # iterations (~2s at 20ms polling)

    enter_tty_runtime()

    # On Unix, listen for SIGWINCH so terminal resizes are picked up
    # immediately rather than waiting for the 0.5s cache TTL.
    # signal.signal() can only be called from the main thread.
    _prev_sigwinch = install_sigwinch_rerender(throttled)

    try:
        _render_screen(args, state)

        with _RawModeContext():
            while not should_exit:
                # Autosave check (throttled)
                _autosave_counter += 1
                if state.autosave and _autosave_counter >= _AUTOSAVE_CHECK_INTERVAL:
                    _autosave_counter = 0
                    state.autosave.save_if_needed()

                # Check if background agent thread completed
                agent_result_data = state.agent_result
                lock = getattr(state, "agent_lock", None)
                if agent_result_data is not None and lock is not None and agent_result_data.get("done"):
                    with lock:
                        if agent_result_data.get("messages"):
                            args.messages = agent_result_data["messages"]
                        agent_result_data["done"] = False  # Reset flag

                # Read raw input
                if sys.platform == "win32":
                    import msvcrt

                    if not msvcrt.kbhit():
                        # Flush any deferred renders during idle
                        throttled.flush()
                        time.sleep(0.05)  # 从 0.02 增加到 0.05 降低 CPU 使用率
                        continue
                    # Use _win_read_one_key to translate special keys
                    chunk = ""
                    while True:
                        ch = _win_read_one_key()
                        if not ch:
                            break
                        chunk += ch
                else:
                    import select

                    _fd = sys.stdin.fileno()
                    ready, _, _ = select.select([_fd], [], [], 0.05)
                    if not ready:
                        # Flush any deferred renders during idle
                        throttled.flush()
                        continue
                    # Use os.read() to bypass Python's TextIOWrapper/
                    # BufferedReader which can block on partial UTF-8
                    # sequences in raw mode.
                    _raw = os.read(_fd, 4096)
                    if not _raw:
                        should_exit = True
                        continue
                    # Drain any remaining bytes without blocking
                    while True:
                        ready2, _, _ = select.select([_fd], [], [], 0)
                        if not ready2:
                            break
                        _more = os.read(_fd, 4096)
                        if not _more:
                            break
                        _raw += _more
                    chunk = _raw.decode("utf-8", errors="replace")

                if not chunk:
                    continue

                parsed = parse_input_chunk(input_remainder + chunk, incoming_chunk=chunk)
                input_remainder = parsed.rest

                for event in parsed.events:
                    try:
                        _handle_tty_event(args, state, event, rerender, approval_event, approval_result, _handle_input)
                        if state.input == "/exit" or (
                            isinstance(event, KeyEvent)
                            and event.name == "c"
                            and event.ctrl
                        ):
                            raise SystemExit(0)
                    except SystemExit:
                        should_exit = True
                        break
                    except Exception as e:
                        # 记录事件处理错误，但不中断主循环
                        from minicode.logging_config import get_logger
                        get_logger("tty_app").debug("Event handling error: %s", e, exc_info=True)

                # Ensure the final state after processing all events is visible
                throttled.flush()

    finally:
        # Restore previous SIGWINCH handler on Unix
        exit_tty_runtime(_prev_sigwinch)

        finalize_tty_session(args, state)

    return args.messages


# ---------------------------------------------------------------------------
# Public API / backward-compatible exports for tests
# ---------------------------------------------------------------------------


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """生成工具输入的人类可读摘要。

    对 _summarize_tool_input 的公开包装，供外部调用者使用。
    将工具调用的输入参数字典转换为简洁的字符串描述，
    以便在转录中展示。

    参数:
        tool_name: 被调用工具的名称
        tool_input: 传递给工具的输入字典

    返回:
        用于在转录中显示的人类可读摘要字符串
    """
    return _summarize_tool_input(tool_name, tool_input)


def summarize_tool_output(tool_name: str, output: str) -> str:
    """为折叠显示生成工具输出摘要。

    选取输出的第一行有意义的文本，并将其截断至 140 个字符。
    用于在界面中折叠显示工具调用结果，以节省屏幕空间。

    参数:
        tool_name: 工具名称（为保持 API 一致性而保留，实际未使用）
        output: 完整的工具输出字符串

    返回:
        适用于折叠工具显示的截断摘要
    """
    return _summarize_collapsed_tool_body(output)


def _format_history(entries: list[str], limit: int = 20) -> str:
    """使用从 1 开始的编号格式化最近的历史记录条目。

    将历史记录列表转换为带行号的文本块，每行开头为序号，
    便于用户在界面中查看和引用。

    参数:
        entries: 历史记录条目字符串列表
        limit: 最多显示的条目数，默认为 20

    返回:
        格式化后的多行字符串，每行格式为 "序号. 条目内容"
    """
    start = max(0, len(entries) - limit)
    return "\n".join(
        f"{start + i + 1}. {entry}" for i, entry in enumerate(entries[start:])
    )


def _save_transcript(state_obj: Any, cwd: str, permissions: PermissionManager, output_path: str) -> str:
    """将转录条目保存到文件。

    将当前会话的转录内容（包括消息和工具调用记录）写入指定路径的文件。
    保存时应用权限管理器的过滤规则。

    参数:
        state_obj: 包含转录条目的状态对象
        cwd: 当前工作目录路径
        permissions: 权限管理器，用于过滤敏感内容
        output_path: 输出文件的路径

    返回:
        保存后实际的文件路径字符串
    """
    return _shared_save_transcript(state_obj, cwd, permissions, output_path)


def _apply_tool_result_visual_state(
    entry: TranscriptEntry,
    tool_name: str,
    output: str,
    is_error: bool,
) -> None:
    """对转录条目应用工具结果的视觉状态。

    根据工具执行结果（正常输出或错误）更新转录条目的显示样式，
    如设置错误标记、折叠状态等视觉属性。

    参数:
        entry: 要更新视觉状态的转录条目对象
        tool_name: 工具名称
        output: 工具输出内容
        is_error: 是否为错误输出
    """
    _shared_apply_tool_result_visual_state(entry, tool_name, output, is_error)


def _mark_unfinished_tools(state_obj: Any) -> int:
    """将正在运行的工具条目标记为错误并清理状态。

    当会话异常结束或中断时，调用此函数将所有未完成的工具调用
    标记为错误状态，并清理相关的运行状态数据。

    参数:
        state_obj: 包含转录和工具状态的状态对象

    返回:
        受影响的条目数量
    """
    return _shared_mark_unfinished_tools(state_obj)


def _handle_feedback_mode_event(
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """处理反馈模式（拒绝理由输入）下的事件。

    当用户选择拒绝工具调用并需要提供反馈理由时，
    此函数处理用户的文本输入和按键事件，
    包括字符输入、退格删除、回车确认和 Esc 取消。

    参数:
        state: 当前屏幕状态对象，包含待审批信息
        event: 已解析的输入事件（按键或文本事件）
        rerender: 重新渲染屏幕的回调函数
        approval_event: 用于通知审批完成的事件对象
        approval_result: 用于存储审批结果的字典
    """
    pending = state.pending_approval
    if not pending:
        return

    if isinstance(event, KeyEvent):
        if event.name == "escape":
            pending.feedback_mode = False
            pending.feedback_input = ""
            rerender()
            return
        if event.name == "return":
            approval_result.clear()
            approval_result["decision"] = "deny_with_feedback"
            approval_result["feedback"] = pending.feedback_input
            approval_event.set()
            rerender()
            return
        if event.name == "backspace":
            if pending.feedback_input:
                pending.feedback_input = pending.feedback_input[:-1]
                rerender()
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        pending.feedback_input += event.text
        rerender()
