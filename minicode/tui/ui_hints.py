"""TUI 上下文提示与帮助信息。

根据当前屏幕状态生成上下文相关的提示文本，
包括操作技巧、忙碌状态提示和权限请求提示。
"""

from __future__ import annotations

import random

from minicode.tui.state import ScreenState, TtyAppArgs


def _get_contextual_help(state: ScreenState, args: TtyAppArgs) -> str | None:
    """返回用于底部区域显示的上下文相关提示信息。

    根据屏幕状态生成不同类别的提示：空闲时随机展示操作技巧，
    忙碌时显示当前正在运行的工具，等待权限审批时提示操作方式。

    参数:
        state: 屏幕状态对象
        args: TUI 应用参数

    返回:
        str | None: 提示文本，无合适提示时返回 None
    """  # if not state.is_busy and not state.pending_approval:
        tips = [
            "💡 Tip: Use /skills to see available workflows",
            "💡 Tip: Try '帮我分析这个项目' to get started",
            "💡 Tip: Use Tab to autocomplete commands",
            "💡 Tip: Type /help for all commands",
            "💡 Tip: Use Ctrl+R to search history",
        ]
        return random.choice(tips)

    if state.is_busy and state.active_tool:
        return f"⏳ Running {state.active_tool}... Press Ctrl+C to cancel"

    if state.pending_approval:
        return "⚠️ Permission required. Use arrow keys and Enter to choose"

    return None
