"""会话流程控制模块。

处理 TUI 模式下会话的加载、创建、状态构建、权限审批安装、
会话快照同步和最终保存等生命周期流程。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from minicode.cost_tracker import CostTracker
from minicode.history import load_history_entries
from minicode.permissions import PermissionManager
from minicode.session import (
    AutosaveManager,
    SessionData,
    create_new_session,
    format_session_list,
    format_session_resume,
    get_latest_session,
    list_sessions,
    load_session,
    save_session,
)
from minicode.state import create_app_store
from minicode.tui.state import PendingApproval, ScreenState, TtyAppArgs
from minicode.tui.tool_lifecycle import _bump_transcript_revision
from minicode.tui.types import TranscriptEntry


def handle_session_listing(cwd: str, list_sessions_only: bool) -> bool:
    """处理 --list-sessions 命令行参数。

    如果参数为真，列出所有历史会话并返回 True；否则返回 False。

    参数:
        cwd: 当前工作目录（保留参数，暂未使用）。
        list_sessions_only: 是否仅列出会话。

    返回:
        是否执行了会话列表打印操作。
    """  # if not list_sessions_only:
        return False
    sessions = list_sessions()
    print(format_session_list(sessions))
    return True


def load_or_create_session(cwd: str, resume_session: str | None) -> SessionData:
    """加载已有会话或创建新会话。

    根据 resume_session 参数决定行为：
      - "latest"：加载当前工作空间的最新会话，若不存在则创建新会话。
      - 具体会话 ID：加载指定会话，不存在则抛出 FileNotFoundError。
      - None：检查当前工作空间是否有可恢复的会话并提示，然后创建新会话。

    参数:
        cwd: 当前工作目录，用于解析工作空间路径。
        resume_session: 要恢复的会话标识（"latest"、会话 ID 或 None）。

    返回:
        加载或创建成功的 SessionData 实例。
    """  # workspace = str(Path(cwd).resolve())
    if resume_session:
        if resume_session == "latest":
            session = get_latest_session(workspace=workspace)
            if session:
                print(format_session_resume(session))
                return session
            print("No previous session found for this workspace.")
            return create_new_session(workspace=workspace)

        session = load_session(resume_session)
        if not session:
            raise FileNotFoundError(f"Session '{resume_session}' not found.")
        print(format_session_resume(session))
        return session

    session = get_latest_session(workspace=workspace)
    if session:
        print(f"Previous session found: {session.session_id[:8]}")
        print("Use --resume to continue, or starting fresh session.")
        return create_new_session(workspace=workspace)

    return create_new_session(workspace=workspace)


def build_tty_runtime_state(
    runtime: dict | None,
    tools: Any,
    model: Any,
    messages: list[Any],
    cwd: str,
    permissions: PermissionManager,
    session: SessionData,
    memory_manager: Any | None = None,
    context_manager: Any | None = None,
    prompt_bundle: Any | None = None,
    product_snapshot: dict[str, Any] | None = None,
) -> tuple[TtyAppArgs, ScreenState]:
    """构建 TUI 运行时所需的初始状态。

    创建 TtyAppArgs 和 ScreenState 实例，装载命令历史、会话信息、
    自动保存管理器、应用状态 Store 和成本追踪器。
    如果会话中包含历史消息和转录条目，则将其恢复到运行时中。

    参数:
        runtime: 运行时配置字典。
        tools: 工具注册表。
        model: 模型适配器。
        messages: 聊天消息列表。
        cwd: 当前工作目录。
        permissions: 权限管理器。
        session: 会话数据。
        memory_manager: 可选的 memory 管理器。
        context_manager: 可选的 context 管理器。
        prompt_bundle: 可选的 prompt 捆绑包。
        product_snapshot: 可选的产品快照字典。

    返回:
        (TtyAppArgs, ScreenState) 二元组。
    """  # args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=model,
        messages=messages,
        cwd=cwd,
        permissions=permissions,
        memory_manager=memory_manager,
        context_manager=context_manager,
        prompt_bundle=prompt_bundle,
        product_snapshot=product_snapshot,
    )

    state = ScreenState(
        history=load_history_entries(),
        session=session,
        autosave=AutosaveManager(session),
        app_state=create_app_store({
            "session_id": session.session_id,
            "workspace": cwd,
            "model": runtime.get("model", "unknown") if runtime else "unknown",
        }),
        cost_tracker=CostTracker(),
    )
    state.history_index = len(state.history)

    if session.messages:
        args.messages.clear()
        args.messages.extend(session.messages)
        for entry_data in session.transcript_entries:
            state.transcript.append(TranscriptEntry(**entry_data))
        _bump_transcript_revision(state)
        print(f"Restored {len(session.messages)} messages, {len(state.transcript)} transcript entries.")

    return args, state


def install_permission_prompt(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Any,
) -> tuple[threading.Event, dict[str, Any], Any]:
    """安装权限审批提示处理器。

    用阻塞式审批处理器替换 PermissionManager 的 prompt 回调，
    在 TUI 中显示审批请求并等待用户操作。返回同步原语供外部
    在用户做出选择后解锁。

    参数:
        args: TTY 应用参数，其 permissions.prompt 将被替换。
        state: 屏幕状态，用于存储待审批请求。
        rerender: 触发界面重渲染的可调用对象。

    返回:
        (approval_event, approval_result, handler) 三元组，
        分别对应：通知事件、结果字典和处理器函数引用。
    """  # approval_event = threading.Event()
    approval_result: dict[str, Any] = {}

    def _permission_prompt_handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal approval_result
        state.pending_approval = PendingApproval(
            request=request,
            resolve=lambda r: None,
        )
        rerender()
        approval_event.clear()
        approval_event.wait()
        result = approval_result.copy()
        state.pending_approval = None
        return result

    args.permissions.prompt = _permission_prompt_handler
    return approval_event, approval_result, _permission_prompt_handler


def refresh_tty_session_snapshot(args: TtyAppArgs, state: ScreenState) -> None:
    """将实时的 TTY 会话状态同步到 SessionData 中，不触发磁盘写入。

    将当前消息列表、转录条目、命令历史、权限摘要、技能信息、
    MCP 服务器列表以及产品快照中的 instruction 层、hook 状态、
    委托任务等数据写回 state.session 对象。

    参数:
        args: TTY 应用参数，从中读取 messages、permissions、tools 等。
        state: 当前屏幕状态，其 session 将被就地更新。
    """  # if not state.session:
        return

    state.session.messages = list(args.messages)
    state.session.transcript_entries = [
        {
            "id": e.id,
            "kind": e.kind,
            "category": e.category,
            "runtimeKind": e.runtimeKind,
            "runtimeStep": e.runtimeStep,
            "runtimePhase": e.runtimePhase,
            "runtimeStopReason": e.runtimeStopReason,
            "runtimeVerificationFocus": e.runtimeVerificationFocus,
            "toolName": e.toolName,
            "status": e.status,
            "body": e.body,
            "collapsed": e.collapsed,
            "collapsedSummary": e.collapsedSummary,
            "collapsePhase": e.collapsePhase,
        }
        for e in state.transcript
    ]
    state.session.history = state.history
    state.session.permissions_summary = args.permissions.get_summary()
    state.session.skills = args.tools.get_skills()
    state.session.mcp_servers = args.tools.get_mcp_servers()
    product_snapshot = getattr(args, "product_snapshot", None)
    if product_snapshot:
        state.session.instruction_layers = list(
            product_snapshot.get("instruction_layers", [])
        )
        state.session.hook_status = dict(product_snapshot.get("hook_status", {}))
        state.session.delegated_tasks = list(
            product_snapshot.get("delegated_tasks", [])
        )
        state.session.delegation_status = dict(
            product_snapshot.get("delegation_status", {})
        )
        state.session.extension_manifests = list(
            product_snapshot.get("extension_manifests", [])
        )
        state.session.readiness_report = dict(
            product_snapshot.get("readiness_report", {})
        )
    if hasattr(state.session, "update_metadata"):
        state.session.update_metadata()


def finalize_tty_session(args: TtyAppArgs, state: ScreenState) -> None:
    """完成并持久化 TTY 会话。

    先同步最新的运行时状态到会话对象，然后通过自动保存管理器
    或直接调用 save_session 保存到磁盘，并打印保存成功的摘要信息。

    参数:
        args: TTY 应用参数。
        state: 当前屏幕状态，包含待保存的 session 和 autosave 对象。
    """  # if not state.session:
        return

    refresh_tty_session_snapshot(args, state)

    if state.autosave:
        state.autosave.force_save()
    else:
        save_session(state.session)

    print(f"\nSession saved: {state.session.session_id[:8]}")
