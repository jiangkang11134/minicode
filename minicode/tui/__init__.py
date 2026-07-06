"""MiniCode TUI（终端用户界面）模块的公共 API 入口。

汇集了屏幕管理、界面元素渲染、输入解析、Markdown 渲染、主题和会话记录
等子模块的公开接口，通过 __all__ 统一导出。
"""

from minicode.tui.chrome import (
    get_permission_prompt_max_scroll_offset,
    render_banner,
    render_footer_bar,
    render_panel,
    render_permission_prompt,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
)
from minicode.tui.input import render_input_prompt
from minicode.tui.input_parser import (
    KeyEvent,
    ParsedInputEvent,
    ParseResult,
    TextEvent,
    WheelEvent,
    parse_input_chunk,
)
from minicode.tui.markdown import render_markdownish
from minicode.tui.screen import (
    clear_screen,
    enter_alternate_screen,
    exit_alternate_screen,
    hide_cursor,
    show_cursor,
)
from minicode.tui.theme import ColorTheme, theme
from minicode.tui.transcript import (
    format_transcript_text,
    get_transcript_max_scroll_offset,
    get_transcript_window_size,
    render_transcript,
)
from minicode.tui.types import TranscriptEntry

__all__ = [
    # screen
    "clear_screen",
    "enter_alternate_screen",
    "exit_alternate_screen",
    "hide_cursor",
    "show_cursor",
    # chrome
    "get_permission_prompt_max_scroll_offset",
    "render_banner",
    "render_footer_bar",
    "render_panel",
    "render_permission_prompt",
    "render_slash_menu",
    "render_status_line",
    "render_tool_panel",
    # input
    "render_input_prompt",
    # input_parser
    "KeyEvent",
    "ParsedInputEvent",
    "ParseResult",
    "TextEvent",
    "WheelEvent",
    "parse_input_chunk",
    # markdown
    "render_markdownish",
    # theme
    "ColorTheme",
    "theme",
    # transcript
    "format_transcript_text",
    "get_transcript_max_scroll_offset",
    "get_transcript_window_size",
    "render_transcript",
    # types
    "TranscriptEntry",
]
