"""终端用户界面的核心渲染器。

提供屏幕渲染、缓存管理、增量更新等功能，负责将应用状态
转换为终端输出，包括头部面板、会话面板、输入面板和底部状态栏。
"""

from __future__ import annotations
import sys
import time
from typing import Any
from minicode.background_tasks import list_background_tasks
from minicode.session import format_checkpoint_summary_line
from minicode.tui.chrome import (
    _cached_terminal_size,
    render_banner,
    render_footer_bar,
    render_panel,
    render_permission_prompt,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
    SUBTLE,
    RESET,
)
from minicode.tui.input import render_input_prompt
from minicode.tui.transcript import format_runtime_summary_line, render_transcript
from minicode.tui.state import TtyAppArgs, ScreenState
from minicode.tui.navigation import _get_transcript_body_lines, _get_visible_commands
from minicode.tui.tool_helpers import _get_session_stats
from minicode.tui.types import TranscriptEntry
from minicode.tui.ui_hints import _get_contextual_help

# Rendering — cached header & footer
# ---------------------------------------------------------------------------

# Banner cache: the banner rarely changes (only when cwd, model, or stats change).
_banner_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}

# Incremental rendering: track last rendered state to avoid full redraw
_last_render_hash: int = 0
_last_render_time: float = 0.0
_transcript_snapshot_cache: dict[
    str,
    tuple[tuple[int, int, int], list[TranscriptEntry]],
] = {}


def _render_header_panel(args: TtyAppArgs, state: ScreenState) -> str:
    """渲染顶部横幅面板，包含模型信息、当前工作目录和会话统计。

    结果会被缓存，当统计信息未变化时避免重复渲染。

    参数:
        args: 终端应用参数，包含运行时、工作目录和权限信息。
        state: 当前屏幕状态，用于获取会话统计。

    返回:
        渲染后的顶部面板字符串。
    """  # stats = _get_session_stats(args, state)
    cache_key = (
        args.cwd,
        id(args.runtime),
        stats.get("transcriptCount"),
        stats.get("messageCount"),
        stats.get("skillCount"),
        stats.get("mcpCount"),
        _cached_terminal_size(),
    )
    cached = _banner_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_banner(
        args.runtime,
        args.cwd,
        args.permissions.get_summary(),
        stats,
    )
    _banner_cache["key"] = (cache_key, result)
    return result


# Footer cache: only changes with status, tool/skill state, background tasks
_footer_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


def _render_footer_cached(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]],
) -> str:
    """渲染底部状态栏，带缓存以减少闪烁。

    显示当前操作状态、工具/技能可用性以及后台任务信息。

    参数:
        status: 当前操作状态的描述文本，可为 None。
        tools_enabled: 工具是否已启用。
        skills_enabled: 技能是否已启用。
        background_tasks: 后台任务列表。

    返回:
        渲染后的底部状态栏字符串。
    """  # cache_key = (
        status,
        tools_enabled,
        skills_enabled,
        len(background_tasks),
        _cached_terminal_size(),
    )
    cached = _footer_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_footer_bar(status, tools_enabled, skills_enabled, background_tasks)
    _footer_cache["key"] = (cache_key, result)
    return result


def _render_prompt_panel(state: ScreenState) -> str:
    """渲染输入提示面板，包含输入框和斜杠命令菜单。

    当输入内容匹配到可用命令时，在输入框下方附加斜杠命令菜单。

    参数:
        state: 当前屏幕状态，包含输入文本、光标偏移和所选命令索引。

    返回:
        渲染后的输入提示面板字符串。
    """  # commands = _get_visible_commands(state.input)
    prompt_body = render_input_prompt(state.input, state.cursor_offset)
    if commands:
        prompt_body += "\n" + render_slash_menu(
            commands,
            min(state.selected_slash_index, len(commands) - 1),
        )
    return render_panel("prompt", prompt_body)


def _compute_render_hash(args: TtyAppArgs, state: ScreenState) -> int:
    """计算当前渲染状态的哈希值，用于检测是否需要重绘。"""  # transcript_rev = state.transcript_revision
    scroll = state.transcript_scroll_offset
    input_hash = hash(state.input)
    cursor = state.cursor_offset
    status = hash(state.status)
    approval = 0
    if state.pending_approval:
        approval = hash((
            state.pending_approval.details_expanded,
            state.pending_approval.details_scroll_offset,
            state.pending_approval.selected_choice_index,
            state.pending_approval.feedback_mode,
            state.pending_approval.feedback_input,
        ))
    recent_tool_state = tuple(
        (tool.get("name"), tool.get("status"))
        for tool in state.recent_tools[-3:]
    )
    term_size = _cached_terminal_size()
    return hash((
        transcript_rev,
        scroll,
        input_hash,
        cursor,
        status,
        state.active_tool,
        recent_tool_state,
        approval,
        term_size,
    ))


def _get_transcript_snapshot(state: ScreenState) -> list[TranscriptEntry]:
    """获取当前会话记录的快照，带缓存以避免并发修改问题。

    由于后台线程可能会追加记录，通过快照机制确保渲染时数据一致性。

    参数:
        state: 当前屏幕状态，包含会话记录列表及其修订版本号。

    返回:
        会话记录条目的快照列表。
    """  # cache_key = (id(state.transcript), state.transcript_revision, len(state.transcript))
    cached = _transcript_snapshot_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]

    snapshot = list(state.transcript)
    _transcript_snapshot_cache["key"] = (cache_key, snapshot)
    return snapshot


def _decorate_session_feed_body(
    transcript_body: str,
    transcript_entries: list[TranscriptEntry],
    session: Any | None = None,
) -> str:
    """在会话记录正文前附加会话元数据摘要信息。

    包括检查点摘要、运行时摘要以及就绪度、指令、钩子、委托和扩展等
    会话元数据，如果存在的话。

    参数:
        transcript_body: 原始的会话记录正文。
        transcript_entries: 会话记录条目列表。
        session: 可选的会话对象，包含元数据信息。

    返回:
        装饰后的会话记录正文字符串，若无需添加则返回原始正文。
    """  # checkpoint_summary_line = format_checkpoint_summary_line(session)
    runtime_summary_line = format_runtime_summary_line(transcript_entries)
    session_metadata = getattr(session, "metadata", None)
    summary_lines = [
        line
        for line in (
            checkpoint_summary_line,
            runtime_summary_line,
            f"readiness-summary: {session_metadata.readiness_summary}"
            if session_metadata and getattr(session_metadata, "readiness_summary", "")
            else "",
            f"instruction-summary: {session_metadata.instruction_summary}"
            if session_metadata and getattr(session_metadata, "instruction_summary", "")
            else "",
            f"hook-summary: {session_metadata.hook_summary}"
            if session_metadata and getattr(session_metadata, "hook_summary", "")
            else "",
            f"delegation-summary: {session_metadata.delegation_summary}"
            if session_metadata and getattr(session_metadata, "delegation_summary", "")
            else "",
            f"extension-summary: {session_metadata.extension_summary}"
            if session_metadata and getattr(session_metadata, "extension_summary", "")
            else "",
        )
        if line
    ]
    if not summary_lines:
        return transcript_body
    summary_block = f"{RESET}\n{SUBTLE}".join(summary_lines)
    return f"{SUBTLE}{summary_block}{RESET}\n\n{transcript_body}"


def _render_screen(args: TtyAppArgs, state: ScreenState) -> None:
    """渲染完整屏幕内容并输出到终端。

    构建完整的终端帧，包含头部面板、会话面板（或权限审批覆盖层）、
    输入提示面板和底部状态栏。采用增量渲染策略，通过哈希比较
    判断内容是否变化，并在短时间内跳过重复渲染以控制帧率。

    参数:
        args: 终端应用参数，包含运行时、工具、权限等信息。
        state: 当前屏幕状态，包含输入、会话记录、待审批等状态。
    """  # global _last_render_hash, _last_render_time
    
    # Quick check: skip render if nothing changed and within throttle
    current_hash = _compute_render_hash(args, state)
    now = time.monotonic()
    if (current_hash == _last_render_hash 
            and now - _last_render_time < 0.016):  # ~60fps cap
        return
    
    background_tasks = list_background_tasks()

    # 获取上下文帮助
    contextual_help = _get_contextual_help(state, args)

    # Build the entire frame into a buffer, then write once
    buf: list[str] = []
    # CSI H + CSI J  (cursor home + erase to end) – avoids full clear flicker
    buf.append("\u001b[H\u001b[J")

    # Header
    buf.append(_render_header_panel(args, state))
    buf.append("\n\n")

    has_skills = len(args.tools.get_skills()) > 0

    if state.pending_approval:
        # Permission approval overlay
        buf.append(
            render_permission_prompt(
                state.pending_approval.request,
                expanded=state.pending_approval.details_expanded,
                scroll_offset=state.pending_approval.details_scroll_offset,
                selected_choice_index=state.pending_approval.selected_choice_index,
                feedback_mode=state.pending_approval.feedback_mode,
                feedback_input=state.pending_approval.feedback_input,
            )
        )
        buf.append("\n\n")
        buf.append(
            render_panel(
                "activity",
                render_tool_panel(state.active_tool, state.recent_tools, background_tasks),
            )
        )
        buf.append("\n\n")
        buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks))
        output = "".join(buf)
        sys.stdout.write(output)
        sys.stdout.flush()
        _last_render_hash = current_hash
        _last_render_time = now
        return

    # Transcript — snapshot the list to avoid IndexError from concurrent
    # agent-thread appends (CPython GIL makes list.append atomic but
    # iteration + append can still race on length vs slot access).
    transcript_snapshot = _get_transcript_snapshot(state)
    body_lines = _get_transcript_body_lines(args, state)
    if transcript_snapshot:
        transcript_body = render_transcript(
            transcript_snapshot,
            state.transcript_scroll_offset,
            body_lines,
            state.transcript_revision,
        )
        transcript_body = _decorate_session_feed_body(
            transcript_body,
            transcript_snapshot,
            state.session,
        )
    else:
        transcript_body = f"{render_status_line(None)}\n\nType /help for commands."
    buf.append(
        render_panel(
            "session feed",
            transcript_body,
            right_title=f"{len(transcript_snapshot)} events",
            min_body_lines=body_lines,
        )
    )
    buf.append("\n\n")

    # Prompt
    buf.append(_render_prompt_panel(state))
    buf.append("\n\n")

    # Footer (cached)
    buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks))
    
    # 上下文帮助行
    if contextual_help:
        buf.append(f"\n{SUBTLE}{contextual_help}{RESET}")
    
    output = "".join(buf)
    sys.stdout.write(output)
    sys.stdout.flush()
    _last_render_hash = current_hash
    _last_render_time = now


# ---------------------------------------------------------------------------
