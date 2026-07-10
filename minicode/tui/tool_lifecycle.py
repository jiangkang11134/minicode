"""工具调用生命周期管理。

提供 Transcript 条目的增删改查、状态更新、折叠控制等功能，
管理工具调用的完整生命周期。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from minicode.tui.state import ScreenState
from minicode.tui.tool_helpers import _summarize_collapsed_tool_body
from minicode.tui.types import TranscriptEntry


def _bump_transcript_revision(state: ScreenState) -> None:
    """递增转录本的修订号，触发 UI 重渲染。

    参数:
        state: 屏幕状态对象
    """  # state.transcript_revision += 1


def _push_transcript_entry(state: ScreenState, **kwargs: Any) -> int:
    """向 Transcript 中添加一条新条目。

    分配自增 ID 后追加到 transcript 列表，并递增修订号。

    参数:
        state: 屏幕状态对象
        **kwargs: TranscriptEntry 的其他字段参数（kind、body 等）

    返回:
        int: 新条目的 ID
    """
    entry_id = state.next_entry_id
    state.next_entry_id += 1
    state.transcript.append(TranscriptEntry(id=entry_id, **kwargs))
    _bump_transcript_revision(state)
    return entry_id


def _update_transcript_entry(state: ScreenState, entry_id: int, **kwargs: Any) -> bool:
    """更新 Transcript 中指定条目的字段值。

    遍历查找匹配 ID 的条目，仅在实际值发生变更时更新并递增修订号。

    参数:
        state: 屏幕状态对象
        entry_id: 要更新的条目 ID
        **kwargs: 要更新的字段名和对应值

    返回:
        bool: 是否有字段发生了实际变更
    """
    for entry in state.transcript:
        if entry.id == entry_id:
            changed = False
            for key, value in kwargs.items():
                if hasattr(entry, key) and getattr(entry, key) != value:
                    setattr(entry, key, value)
                    changed = True
            if changed:
                _bump_transcript_revision(state)
            return changed
    return False


def _find_transcript_entry(
    state: ScreenState,
    entry_id: int,
    *,
    prefer_tail: bool = False,
) -> TranscriptEntry | None:
    """在 Transcript 中按 ID 查找条目。

    支持从前向后或从后向前（最近优先）两种遍历方向。

    参数:
        state: 屏幕状态对象
        entry_id: 要查找的条目 ID
        prefer_tail: 是否从尾部开始搜索（查找最近的匹配项）

    返回:
        TranscriptEntry | None: 找到的条目，未找到返回 None
    """
    entries = reversed(state.transcript) if prefer_tail else state.transcript
    for entry in entries:
        if entry.id == entry_id:
            return entry
    return None


def _append_to_transcript_entry(state: ScreenState, entry_id: int, extra_body: str) -> bool:
    """向指定条目追加正文内容。

    在末尾查找最近的匹配条目，将 extra_body 追加到其 body 字段后。

    参数:
        state: 屏幕状态对象
        entry_id: 目标条目 ID
        extra_body: 要追加的文本内容

    返回:
        bool: 是否成功追加
    """
    if not extra_body:
        return False
    entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
    if entry is None:
        return False
    entry.body += extra_body
    _bump_transcript_revision(state)
    return True


def _mark_running_tools_as_error(state: ScreenState, message: str) -> None:
    """将所有运行中的工具条目标记为错误状态。

    遍历 transcript，将所有 kind 为 "tool" 且 status 为 "running" 的条目
    设置为错误状态，并将错误信息写入 body。

    参数:
        state: 屏幕状态对象
        message: 错误信息文本
    """
    changed = False
    for entry in state.transcript:
        if entry.kind == "tool" and entry.status == "running":
            entry.status = "error"
            entry.body = message
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            state.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
            changed = True
    if any(e.kind == "tool" and e.status == "error" for e in state.transcript):
        state.active_tool = None
    if changed:
        _bump_transcript_revision(state)


def _update_tool_entry(state: ScreenState, entry_id: int, status: str, body: str) -> bool:
    """更新指定工具条目的状态和正文。

    查找最近匹配的工具类型条目，更新其 status、body 及折叠相关字段。

    参数:
        state: 屏幕状态对象
        entry_id: 工具条目 ID
        status: 新的状态值（如 "success"、"error"）
        body: 新的正文内容

    返回:
        bool: 是否有字段发生了实际变更
    """
    entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
    if entry is None or entry.kind != "tool":
        return False

    changed = False
    updates = {
        "status": status,
        "body": body,
        "collapsed": False,
        "collapsedSummary": None,
        "collapsePhase": None,
    }
    for key, value in updates.items():
        if getattr(entry, key) != value:
            setattr(entry, key, value)
            changed = True
    if changed:
        _bump_transcript_revision(state)
    return changed


def _set_tool_entry_collapse_phase(state: ScreenState, entry_id: int, phase: int) -> bool:
    """设置工具条目的折叠阶段。

    仅对非运行中的工具条目生效，如果指定阶段与当前值相同则不执行。

    参数:
        state: 屏幕状态对象
        entry_id: 工具条目 ID
        phase: 折叠阶段值

    返回:
        bool: 是否发生了变更
    """
    entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
    if entry is None or entry.kind != "tool" or entry.status == "running":
        return False
    if entry.collapsePhase == phase:
        return False
    entry.collapsePhase = phase
    _bump_transcript_revision(state)
    return True


def _collapse_tool_entry(state: ScreenState, entry_id: int, summary: str) -> bool:
    """将工具条目标记为折叠状态并设置摘要。

    仅对非运行中的工具条目生效，设置 collapsed=True 并保存摘要。

    参数:
        state: 屏幕状态对象
        entry_id: 工具条目 ID
        summary: 折叠后显示的摘要文本

    返回:
        bool: 是否发生了变更
    """
    entry = _find_transcript_entry(state, entry_id, prefer_tail=True)
    if entry is None or entry.kind != "tool" or entry.status == "running":
        return False

    changed = False
    updates = {
        "collapsePhase": None,
        "collapsed": True,
        "collapsedSummary": summary,
    }
    for key, value in updates.items():
        if getattr(entry, key) != value:
            setattr(entry, key, value)
            changed = True
    if changed:
        _bump_transcript_revision(state)
    return changed


def _get_running_tool_entries(state: ScreenState) -> list[TranscriptEntry]:
    """获取所有正在运行中的工具条目列表。

    参数:
        state: 屏幕状态对象

    返回:
        list[TranscriptEntry]: 状态为 "running" 的工具条目列表
    """
    return [e for e in state.transcript if e.kind == "tool" and e.status == "running"]


def _finalize_dangling_running_tools(state: ScreenState) -> None:
    """终结上一轮未完成的工具调用。

    将所有仍处于运行状态的工具条目标记为错误，并设置状态提示信息。

    参数:
        state: 屏幕状态对象
    """
    running = _get_running_tool_entries(state)
    if running:
        error_message = (
            f"{running[0].body}\n\n"
            "ERROR: Tool did not report a final result before the turn ended. "
            "This usually means the command kept running in the background "
            "or the tool lifecycle got out of sync."
        )
        _mark_running_tools_as_error(state, error_message)
        state.status = f"Previous turn ended with {len(running)} unfinished tool call(s)."


def _schedule_tool_auto_collapse(
    state: ScreenState,
    entry_id: int,
    output: str,
    rerender: Callable[[], None],
) -> None:
    """延迟自动折叠工具条目。

    在 0.25 秒后对工具条目执行折叠操作，并触发重渲染。
    使用守护线程执行，不影响主线程。

    参数:
        state: 屏幕状态对象
        entry_id: 工具条目 ID
        output: 工具输出的原始文本，用于生成摘要
        rerender: 重渲染回调函数
    """
    summary = _summarize_collapsed_tool_body(output)

    def _do_collapse() -> None:
        time.sleep(0.25)
        _collapse_tool_entry(state, entry_id, summary)
        rerender()

    t = threading.Thread(target=_do_collapse, daemon=True)
    t.start()
