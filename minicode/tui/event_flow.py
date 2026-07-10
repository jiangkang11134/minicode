"""终端事件分发流程：处理键盘、文本、滚轮事件，协调输入编辑与权限审批。

将终端原始输入事件（ParsedInputEvent）分发到对应的处理路径：
- 权限审批模式下的键盘/文本/滚轮/反馈事件
- 正常模式下的输入编辑、历史导航、斜杠命令选择、会话滚动
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from minicode.tui.input_parser import KeyEvent, ParsedInputEvent, TextEvent, WheelEvent
from minicode.tui.navigation import (
    _get_visible_commands,
    _history_down,
    _history_up,
    _jump_transcript_to_edge,
    _move_pending_approval_selection,
    _scroll_pending_approval_by,
    _scroll_transcript_by,
    _toggle_pending_approval_expand,
)
from minicode.tui.state import ScreenState, TtyAppArgs


def _handle_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
    handle_input_fn: Callable[[TtyAppArgs, ScreenState, Callable[[], None], str | None], bool],
) -> None:
    """主事件入口：将事件分发到权限审批模式或正常模式。

    首先检查是否为 Ctrl+C 退出命令；然后判断是否有待审批请求，
    如果有则进入审批事件处理路径，否则进入正常输入编辑模式。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        event: 已解析的输入事件
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典
        handle_input_fn: 提交输入后的处理回调
    """
    if isinstance(event, TextEvent) and event.ctrl and event.text == "c":
        raise SystemExit(0)

    pending = state.pending_approval
    if pending is not None:
        _handle_pending_approval_event(state, pending, event, rerender, approval_event, approval_result)
        return

    _handle_normal_mode_event(args, state, event, rerender, handle_input_fn)


def _handle_pending_approval_event(
    state: ScreenState,
    pending: Any,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """处理权限审批模式下的事件分发。

    根据事件类型（KeyEvent、TextEvent、WheelEvent）和审批状态（是否处于反馈模式），
    将事件路由到对应的处理函数。

    参数:
        state: 当前屏幕状态
        pending: 待审批请求对象
        event: 已解析的输入事件
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典
    """
    if pending.feedback_mode:
        _handle_feedback_mode_event(state, event, rerender, approval_event, approval_result)
        return

    if isinstance(event, KeyEvent):
        if _handle_pending_approval_key(state, event, rerender, approval_event, approval_result):
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        if _handle_pending_approval_text(state, event, rerender, approval_event, approval_result):
            return

    if isinstance(event, WheelEvent):
        if _handle_pending_approval_wheel(state, event, rerender):
            return


def _handle_pending_approval_key(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    """处理权限审批模式下的按键事件。

    支持的按键：
      - Escape: 拒绝本次请求 (deny_once)
      - Return: 确认当前选中的选项
      - Up/Down: 切换选项选择
      - PageUp/PageDown: 滚动详情区域
      - 单个字母键: 匹配选项的快捷键

    参数:
        state: 当前屏幕状态
        event: 按键事件
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典

    返回:
        事件是否已处理
    """
    pending = state.pending_approval

    if event.name == "escape":
        approval_result.clear()
        approval_result["decision"] = "deny_once"
        approval_event.set()
        rerender()
        return True

    if event.name == "return":
        _confirm_pending_choice(state, rerender, approval_event, approval_result)
        return True

    if event.name == "up" and _move_pending_approval_selection(state, -1):
        rerender()
        return True

    if event.name == "down" and _move_pending_approval_selection(state, 1):
        rerender()
        return True

    if event.name == "pageup" and _scroll_pending_approval_by(state, -5):
        rerender()
        return True

    if event.name == "pagedown" and _scroll_pending_approval_by(state, 5):
        rerender()
        return True

    choices = pending.request.get("choices", [])
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True

    return False


def _handle_pending_approval_text(
    state: ScreenState,
    event: TextEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    """处理权限审批模式下的文本输入事件。

    支持：
      - ``v`` 键: 展开/折叠审批详情
      - 选项快捷键: 直接选择对应选项

    参数:
        state: 当前屏幕状态
        event: 文本事件
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典

    返回:
        事件是否已处理
    """
    pending = state.pending_approval

    if event.text == "v" and _toggle_pending_approval_expand(state):
        rerender()
        return True

    choices = pending.request.get("choices", [])
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True

    return False


def _handle_pending_approval_wheel(
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    """处理权限审批模式下的滚轮事件。

    按方向滚动审批详情区域，每次 3 行。

    参数:
        state: 当前屏幕状态
        event: 滚轮事件
        rerender: 触发重新渲染的回调

    返回:
        事件是否已处理
    """
    delta = 3 if event.direction == "up" else -3
    if _scroll_pending_approval_by(state, delta):
        rerender()
        return True
    return False


def _confirm_pending_choice(
    state: ScreenState,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """确认当前选中的审批选项。

    如果有选项列表且选中索引有效，则调用 ``_select_pending_choice``
    执行对应的决策；否则默认允许本次 (allow_once)。

    参数:
        state: 当前屏幕状态
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典
    """
    pending = state.pending_approval
    choices = pending.request.get("choices", [])

    if choices and 0 <= pending.selected_choice_index < len(choices):
        choice = choices[pending.selected_choice_index]
        _select_pending_choice(state, choice, rerender, approval_event, approval_result)
    else:
        approval_result.clear()
        approval_result["decision"] = "allow_once"
        approval_event.set()
        rerender()


def _select_pending_choice(
    state: ScreenState,
    choice: dict,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """执行选中的审批选项决策。

    根据选项的 ``decision`` 字段决定行为：
      - ``deny_with_feedback``: 切换到反馈输入模式
      - 其他决策: 直接设置审批结果并通知

    参数:
        state: 当前屏幕状态
        choice: 选中的选项字典，含 decision 等字段
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典
    """
    pending = state.pending_approval
    decision = choice.get("decision", "allow_once")

    if decision == "deny_with_feedback":
        pending.feedback_mode = True
        pending.feedback_input = ""
        rerender()
        return

    approval_result.clear()
    approval_result["decision"] = decision
    approval_event.set()
    rerender()


def _handle_normal_mode_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    handle_input_fn: Callable[[TtyAppArgs, ScreenState, Callable[[], None], str | None], bool],
) -> None:
    """处理正常模式（非审批模式）下的事件分发。

    根据事件类型（KeyEvent、TextEvent、WheelEvent）将事件路由到对应的处理函数。
    路由前先获取当前可见的斜杠命令列表。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        event: 已解析的输入事件
        rerender: 触发重新渲染的回调
        handle_input_fn: 提交输入后的处理回调
    """
    visible_commands = _get_visible_commands(state.input)

    if isinstance(event, KeyEvent):
        if _handle_normal_mode_key(args, state, event, visible_commands, rerender, handle_input_fn):
            return
    elif isinstance(event, TextEvent):
        if _handle_normal_mode_text(args, state, event, visible_commands, rerender):
            return
    elif isinstance(event, WheelEvent):
        if _handle_normal_mode_wheel(args, state, event, rerender):
            return


def _handle_normal_mode_key(
    args: TtyAppArgs,
    state: ScreenState,
    event: KeyEvent,
    visible_commands: list,
    rerender: Callable[[], None],
    handle_input_fn: Callable[[TtyAppArgs, ScreenState, Callable[[], None], str | None], bool],
) -> bool:
    """处理正常模式下的按键事件。

    支持的按键：
      - Return: 提交输入或选择斜杠命令
      - Tab: 自动补全当前选中的斜杠命令
      - Backspace/Delete/Home/End/Left/Right/Ctrl+Left/Ctrl+Right: 输入编辑
      - Escape: 清除输入
      - Ctrl+W: 删除前一个词
      - Ctrl+K: 删除到行尾
      - PageUp/PageDown: 滚动会话记录
      - Up/Down: 斜杠命令导航或历史导航
      - Ctrl+Home/Ctrl+End: 跳转到会话顶部/底部
      - focus_in: 重绘界面

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        event: 按键事件
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调
        handle_input_fn: 提交输入后的处理回调

    返回:
        事件是否已处理
    """
    if event.name == "return":
        _handle_normal_mode_return(args, state, visible_commands, rerender, handle_input_fn)
        return True

    if event.name == "tab" and visible_commands:
        _handle_normal_mode_tab(state, visible_commands, rerender)
        return True

    if _handle_normal_mode_navigation(state, event, rerender):
        return True

    if event.name == "pageup" and _scroll_transcript_by(args, state, 8):
        rerender()
        return True

    if event.name == "pagedown" and _scroll_transcript_by(args, state, -8):
        rerender()
        return True

    # Focus events: re-render on focus regain
    if event.name == "focus_in":
        rerender()
        return True

    # Ctrl+Home/Ctrl+End: jump to transcript top/bottom
    if event.name == "home" and event.ctrl:
        from minicode.tui.navigation import _get_max_transcript_scroll_offset
        state.transcript_scroll_offset = _get_max_transcript_scroll_offset(args, state)
        rerender()
        return True
    if event.name == "end" and event.ctrl:
        state.transcript_scroll_offset = 0
        rerender()
        return True

    if event.name == "up":
        _handle_up_arrow(args, state, visible_commands, rerender)
        return True

    if event.name == "down":
        _handle_down_arrow(args, state, visible_commands, rerender)
        return True

    return False


def _handle_normal_mode_return(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
    handle_input_fn: Callable[[TtyAppArgs, ScreenState, Callable[[], None], str | None], bool],
) -> None:
    """处理正常模式下的 Return 键。

    如果有斜杠命令选中且可见，则将选中命令插入输入框。
    否则提交当前输入内容：清空输入框、重置光标和命令索引，
    调用 ``handle_input_fn`` 处理提交的文本。
    若 ``handle_input_fn`` 返回 ``True`` 则触发退出。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调
        handle_input_fn: 提交输入后的处理回调
    """
    if visible_commands and 0 <= state.selected_slash_index < len(visible_commands):
        selected = visible_commands[state.selected_slash_index]
        usage = getattr(selected, "usage", str(selected))
        state.input = usage
        state.cursor_offset = len(state.input)
        state.selected_slash_index = 0
        rerender()
        return

    submitted = state.input
    state.input = ""
    state.cursor_offset = 0
    state.selected_slash_index = 0
    rerender()
    if not submitted.strip():
        return
    if handle_input_fn(args, state, rerender, submitted):
        raise SystemExit(0)
    rerender()


def _handle_normal_mode_tab(
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """处理正常模式下的 Tab 键：自动补全选中的斜杠命令。

    将当前选中的命令用法插入输入框（追加空格），重置光标和命令索引。

    参数:
        state: 当前屏幕状态
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调
    """
    selected = visible_commands[min(state.selected_slash_index, len(visible_commands) - 1)]
    usage = getattr(selected, "usage", str(selected))
    state.input = usage + " "
    state.cursor_offset = len(state.input)
    state.selected_slash_index = 0
    rerender()


def _handle_normal_mode_navigation(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
) -> bool:
    """处理正常模式下的输入编辑按键（光标移动、字符删除、清空）。

    支持：
      - Backspace/Delete: 删除前一个/当前字符
      - Home/End: 跳转到行首/行尾
      - Left/Right: 左/右移一个字符
      - Ctrl+Left/Right: 左/右移一个词
      - Escape: 清空输入
      - Ctrl+W: 删除前一个词
      - Ctrl+K: 删除到行尾

    参数:
        state: 当前屏幕状态
        event: 按键事件
        rerender: 触发重新渲染的回调

    返回:
        事件是否已处理
    """
    if event.name == "backspace" and state.cursor_offset > 0:
        state.input = state.input[: state.cursor_offset - 1] + state.input[state.cursor_offset :]
        state.cursor_offset -= 1
        state.selected_slash_index = 0
        rerender()
        return True

    if event.name == "delete" and state.cursor_offset < len(state.input):
        state.input = state.input[: state.cursor_offset] + state.input[state.cursor_offset + 1 :]
        state.selected_slash_index = 0
        rerender()
        return True

    if event.name == "home":
        state.cursor_offset = 0
        rerender()
        return True

    if event.name == "end":
        state.cursor_offset = len(state.input)
        rerender()
        return True

    if event.name == "left":
        state.cursor_offset = max(0, state.cursor_offset - 1)
        rerender()
        return True

    if event.name == "right":
        state.cursor_offset = min(len(state.input), state.cursor_offset + 1)
        rerender()
        return True

    if event.name == "left" and event.ctrl:
        state.cursor_offset = _word_left(state.input, state.cursor_offset)
        rerender()
        return True

    if event.name == "right" and event.ctrl:
        state.cursor_offset = _word_right(state.input, state.cursor_offset)
        rerender()
        return True

    if event.name == "escape":
        state.input = ""
        state.cursor_offset = 0
        state.selected_slash_index = 0
        rerender()
        return True

    # Ctrl+W: delete word backward
    if event.name == "w" and event.ctrl:
        target = _word_left(state.input, state.cursor_offset)
        state.input = state.input[:target] + state.input[state.cursor_offset:]
        state.cursor_offset = target
        state.selected_slash_index = 0
        rerender()
        return True

    # Ctrl+K: delete to end of line
    if event.name == "k" and event.ctrl:
        state.input = state.input[:state.cursor_offset]
        state.selected_slash_index = 0
        rerender()
        return True

    return False


def _handle_up_arrow(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """处理上箭头键：在斜杠命令列表中向上循环选择，或进入输入历史。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调
    """
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index - 1 + len(visible_commands)) % len(visible_commands)
        rerender()
    elif _history_up(state):
        rerender()


def _handle_down_arrow(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """处理下箭头键：在斜杠命令列表中向下循环选择，或进入输入历史。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调
    """
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index + 1) % len(visible_commands)
        rerender()
    elif _history_down(state):
        rerender()


def _handle_normal_mode_text(
    args: TtyAppArgs,
    state: ScreenState,
    event: TextEvent,
    visible_commands: list,
    rerender: Callable[[], None],
) -> bool:
    """处理正常模式下的文本输入和 Ctrl 快捷键。

    Ctrl 快捷键支持：
      - Ctrl+U: 清除全部输入
      - Ctrl+A: 输入为空时跳转到会话顶部，否则跳到行首
      - Ctrl+E: 输入为空时跳转到会话底部，否则跳到行尾
      - Ctrl+P: 上一条历史
      - Ctrl+N: 下一条历史

    普通文本：在光标位置插入字符。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        event: 文本事件
        visible_commands: 当前可见的斜杠命令列表
        rerender: 触发重新渲染的回调

    返回:
        事件是否已处理
    """
    if event.ctrl:
        if event.text == "u":
            state.input = ""
            state.cursor_offset = 0
            state.selected_slash_index = 0
            rerender()
            return True

        if event.text == "a":
            if not state.input:
                if _jump_transcript_to_edge(args, state, "top"):
                    rerender()
                return True
            state.cursor_offset = 0
            rerender()
            return True

        if event.text == "e":
            if not state.input:
                if _jump_transcript_to_edge(args, state, "bottom"):
                    rerender()
                return True
            state.cursor_offset = len(state.input)
            rerender()
            return True

        if event.text == "p":
            if _history_up(state):
                rerender()
            return True

        if event.text == "n":
            if _history_down(state):
                rerender()
            return True

        return False

    if not event.ctrl and event.text:
        state.input = state.input[: state.cursor_offset] + event.text + state.input[state.cursor_offset :]
        state.cursor_offset += len(event.text)
        state.selected_slash_index = 0
        state.history_index = len(state.history)
        rerender()
        return True

    return False


def _handle_normal_mode_wheel(
    args: TtyAppArgs,
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    """处理正常模式下的滚轮事件：滚动会话记录。

    每次滚动 3 行（向上为正，向下为负）。

    参数:
        args: TUI 应用参数
        state: 当前屏幕状态
        event: 滚轮事件
        rerender: 触发重新渲染的回调

    返回:
        事件是否已处理
    """
    delta = 3 if event.direction == "up" else -3
    if _scroll_transcript_by(args, state, delta):
        rerender()
        return True
    return False


def _handle_feedback_mode_event(
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """处理审批反馈模式下的键盘和文本事件。

    Escape: 退出反馈模式，不发送反馈
    Return: 提交反馈并执行拒绝决策 (deny_with_feedback)
    Backspace: 删除反馈文本的最后一个字符
    普通文本: 追加到反馈输入

    参数:
        state: 当前屏幕状态
        event: 已解析的输入事件
        rerender: 触发重新渲染的回调
        approval_event: 审批完成通知事件
        approval_result: 审批结果字典
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


# ── Word navigation helpers ────────────────────────────────────────

def _word_left(text: str, cursor: int) -> int:
    """将光标向左移动到前一个词的边界。

    从当前光标位置开始，先跳过空白字符，再跳过非空白字符找到词首。

    参数:
        text: 输入文本
        cursor: 当前光标位置

    返回:
        新的光标位置
    """
    if cursor <= 1:
        return 0
    i = cursor - 1
    while i > 0 and text[i].isspace():
        i -= 1
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    return i


def _word_right(text: str, cursor: int) -> int:
    """将光标向右移动到下一个词的边界。

    从当前光标位置开始，先跳过非空白字符，再跳过空白字符找到词尾。

    参数:
        text: 输入文本
        cursor: 当前光标位置

    返回:
        新的光标位置
    """
    n = len(text)
    if cursor >= n:
        return n
    i = cursor
    while i < n and not text[i].isspace():
        i += 1
    while i < n and text[i].isspace():
        i += 1
    return i
