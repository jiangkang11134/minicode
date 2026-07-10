from __future__ import annotations

from typing import Any

from minicode.cli_commands import SLASH_COMMANDS, find_matching_slash_commands
from minicode.tui.chrome import _cached_terminal_size, get_permission_prompt_max_scroll_offset
from minicode.tui.state import ScreenState, TtyAppArgs
from minicode.tui.transcript import get_transcript_max_scroll_offset

_HEADER_LINES_ESTIMATE = 11
_PROMPT_LINES_ESTIMATE = 7
_FOOTER_LINES = 1
_GAPS = 3
_TRANSCRIPT_FRAME_LINES = 4


def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    """计算转录区域（transcript body）可用的行数。

    基于终端的当前行数减去头部、提示区、页脚、间距和转录框架的估算高度，
    确保至少保留 6 行给转录内容。

    参数:
        args: TTY 应用参数（未直接使用，保留接口一致性）。
        state: 当前屏幕状态（未直接使用，保留接口一致性）。

    返回:
        转录内容区域可用的行数。
    """  # _, rows = _cached_terminal_size()
    rows = max(24, rows)
    chrome_overhead = (
        _HEADER_LINES_ESTIMATE
        + _PROMPT_LINES_ESTIMATE
        + _FOOTER_LINES
        + _GAPS
        + _TRANSCRIPT_FRAME_LINES
    )
    return max(6, rows - chrome_overhead)


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    """获取转录区域允许的最大滚动偏移量。

    委托给 get_transcript_max_scroll_offset 基于当前转录内容、
    可见行数和修订号计算。

    参数:
        args: TTY 应用参数。
        state: 当前屏幕状态，包含 transcript 和 transcript_revision。

    返回:
        最大可滚动偏移行数。
    """
    return get_transcript_max_scroll_offset(
        state.transcript,
        _get_transcript_body_lines(args, state),
        state.transcript_revision,
    )


def _scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    """将转录区域滚动指定的行数偏移量。

    在 0 和最大偏移之间钳制，若位置未变化则返回 False。

    参数:
        args: TTY 应用参数。
        state: 屏幕状态（将被修改）。
        delta: 滚动行数（正数向下，负数向上）。

    返回:
        滚动位置是否实际发生改变。
    """
    max_offset = _get_max_transcript_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    """将转录区域跳到顶部或底部。

    参数:
        args: TTY 应用参数。
        state: 屏幕状态（将被修改）。
        target: 目标位置，"top" 跳到顶部（最新内容），其他值跳到底部。

    返回:
        滚动位置是否实际发生改变。
    """
    next_offset = _get_max_transcript_scroll_offset(args, state) if target == "top" else 0
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _scroll_pending_approval_by(state: ScreenState, delta: int) -> bool:
    """滚动待审批详情区域。

    仅在存在待审批请求且详情已展开时有效。

    参数:
        state: 屏幕状态（将被修改）。
        delta: 滚动行数（正数向下，负数向上）。

    返回:
        滚动位置是否实际发生改变。
    """
    pending = state.pending_approval
    if not pending or not pending.details_expanded:
        return False
    max_offset = get_permission_prompt_max_scroll_offset(pending.request, expanded=True)
    next_offset = max(0, min(max_offset, pending.details_scroll_offset + delta))
    if next_offset == pending.details_scroll_offset:
        return False
    pending.details_scroll_offset = next_offset
    return True


def _toggle_pending_approval_expand(state: ScreenState) -> bool:
    """切换待审批详情区域的展开/折叠状态。

    仅在审批请求的 kind 为 "edit" 时有效，切换后滚动位置归零。

    参数:
        state: 屏幕状态（将被修改）。

    返回:
        展开状态是否实际发生切换。
    """
    pending = state.pending_approval
    if not pending or pending.request.get("kind") != "edit":
        return False
    pending.details_expanded = not pending.details_expanded
    pending.details_scroll_offset = 0
    return True


def _move_pending_approval_selection(state: ScreenState, delta: int) -> bool:
    """移动待审批选择列表中的选中项。

    循环移动，仅在非反馈模式且有可用选项时生效。

    参数:
        state: 屏幕状态（将被修改）。
        delta: 移动步长（正数向下，负数向上）。

    返回:
        是否成功移动（若无可用选项则返回 False）。
    """
    pending = state.pending_approval
    if not pending or pending.feedback_mode:
        return False
    total = len(pending.request.get("choices", []))
    if total <= 0:
        return False
    pending.selected_choice_index = (pending.selected_choice_index + delta + total) % total
    return True


def _history_up(state: ScreenState) -> bool:
    """在命令历史中向上导航（较旧的命令）。

    如果当前在最新位置，先将当前输入保存为 draft。移动 history_index
    并用历史记录中的命令填充 input。

    参数:
        state: 屏幕状态（将被修改）。

    返回:
        是否成功向上导航（历史已到最旧记录则返回 False）。
    """
    if not state.history or state.history_index <= 0:
        return False
    if state.history_index == len(state.history):
        state.history_draft = state.input
    state.history_index -= 1
    state.input = state.history[state.history_index] if state.history_index < len(state.history) else ""
    state.cursor_offset = len(state.input)
    return True


def _history_down(state: ScreenState) -> bool:
    """在命令历史中向下导航（较新的命令）。

    当到达历史末尾时恢复之前保存的 draft。

    参数:
        state: 屏幕状态（将被修改）。

    返回:
        是否成功向下导航（已在最新位置则返回 False）。
    """
    if state.history_index >= len(state.history):
        return False
    state.history_index += 1
    state.input = (
        state.history_draft
        if state.history_index == len(state.history)
        else (state.history[state.history_index] if state.history_index < len(state.history) else "")
    )
    state.cursor_offset = len(state.input)
    return True


def _get_visible_commands(input_text: str) -> list[Any]:
    """获取与当前输入匹配的斜杠命令列表。

    仅在输入以 "/" 开头时生效：单 "/" 返回全部命令，
    否则基于模糊匹配结果过滤。

    参数:
        input_text: 当前输入文本。

    返回:
        匹配的斜杠命令对象列表，无匹配时返回空列表。
    """
    if not input_text.startswith("/"):
        return []
    if input_text == "/":
        return SLASH_COMMANDS
    matches = find_matching_slash_commands(input_text)
    return [cmd for cmd in SLASH_COMMANDS if getattr(cmd, "usage", str(cmd)) in matches]
