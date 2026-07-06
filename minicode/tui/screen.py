"""终端屏幕控制模块。

提供终端备用屏幕（alternate screen buffer）切换、光标显隐、
鼠标追踪、括号粘贴模式、聚焦追踪、同步输出等 ANSI/VT 转义序列的封装。
包含 Windows VT 处理功能的自动启用逻辑。
"""

from __future__ import annotations

import os
import sys

ENTER_ALT_SCREEN = "[?1049h"
EXIT_ALT_SCREEN = "[?1049l"
ERASE_SCREEN_AND_HOME = "[2J[H"
# Mouse tracking sequence breakdown:
#   ?1000h  — basic X10 mouse reporting (button press/release)
#   ?1002h  — button-event tracking (only reports while button pressed, can interfere)
#   ?1003h  — any-event tracking (reports all mouse events including scroll without button)
#   ?1006h  — SGR extended encoding (supports coordinates > 223, required for modern terminals)
# Strategy: use ?1000h (basic) + ?1003h (any-event for reliable scroll) + ?1006h (SGR format)
# This matches the TypeScript mini-code version behavior (?1000h + ?1006h) but adds
# ?1003h for better SSH/remote terminal scroll wheel support.
ENABLE_MOUSE_TRACKING = "[?1000h[?1003h[?1006h"
DISABLE_MOUSE_TRACKING = "[?1006l[?1003l[?1000l"

ENABLE_BRACKETED_PASTE  = "[?2004h"
DISABLE_BRACKETED_PASTE = "[?2004l"
ENABLE_FOCUS_TRACKING  = "[?1004h"
ENABLE_SYNC_OUTPUT  = "[?2026h"
DISABLE_SYNC_OUTPUT = "[?2026l"
DISABLE_FOCUS_TRACKING = "[?1004l"
# Terminal types that do not support alternate screen or mouse tracking.
# NOTE: the empty string is intentionally NOT included. On Windows the TERM
# environment variable is unset by default, so treating "" as dumb would skip
# the alternate screen buffer and cause every redraw frame to accumulate in the
# terminal scrollback (garbled "stacked frame" output when scrolling up —
# GitHub issue #7). Pipes / non-interactive output are handled separately via
# the isatty() guard in _is_dumb_terminal().
_DUMB_TERMS = frozenset({"dumb", "linux"})


# ---------------------------------------------------------------------------
# Windows VT processing
# ---------------------------------------------------------------------------

_vt_enabled = False


def _enable_windows_vt_processing() -> None:
    """启用 Windows 10+ 控制台的 ANSI/VT 转义序列处理。

    不调用此函数时，Windows 控制台会忽略颜色、备用屏幕、光标显隐、
    鼠标追踪等转义码。在非 Windows 平台或 API 调用不可用时静默跳过。

    内部会同时启用标准输出、标准错误输出的 VT 处理标志，
    以及标准输入的 VT 输入处理（使 ConPTY/Windows Terminal
    发送 ANSI 转义序列而非原生按键事件）。
    """  # global _vt_enabled
    if _vt_enabled:
        return

    if sys.platform != "win32":
        _vt_enabled = True
        return

    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        STD_OUTPUT_HANDLE = -11
        STD_ERROR_HANDLE = -12
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001

        for handle_id in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            handle = kernel32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
                kernel32.SetConsoleMode(handle, new_mode)

        # Also enable VT input processing so the console sends ANSI
        # escape sequences for special keys instead of Windows-native
        # key events (useful for ConPTY / Windows Terminal).
        STD_INPUT_HANDLE = -10
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode_in = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_in, ctypes.byref(mode_in)):
            kernel32.SetConsoleMode(h_in, mode_in.value | ENABLE_VIRTUAL_TERMINAL_INPUT)

        _vt_enabled = True
    except Exception:
        # If ctypes is unavailable or the call fails (e.g. old Windows),
        # fall through silently — ANSI codes will simply not render.
        _vt_enabled = True


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def hide_cursor() -> None:
    """隐藏终端光标。"""  # _enable_windows_vt_processing()
    sys.stdout.write("[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    """显示终端光标。"""  # sys.stdout.write("[?25h")
    sys.stdout.flush()


def _is_dumb_terminal() -> bool:
    """判断当前终端是否不支持转义序列。

    Windows 默认没有 TERM 环境变量，但现代 Windows 控制台在启用 VT 处理后
    支持备用屏幕和鼠标追踪。因此通过 isatty() 检查非交互式（管道）输出，
    而非将空 TERM 视为 dumb——否则 Windows 会跳过备用屏幕，
    导致重绘帧堆积在滚动缓冲区中（issue #7）。

    返回:
        若终端为 dumb 类型或输出被重定向到管道，返回 True；否则返回 False。
    """  # if not sys.stdout.isatty():
        return True
    return os.environ.get("TERM", "") in _DUMB_TERMS


def enter_alternate_screen() -> None:
    """切换到终端备用屏幕（alternate screen buffer）。

    在支持 VT 的终端中，切换到独立于主滚动缓冲区的备用屏幕，
    并启用鼠标追踪、括号粘贴模式、聚焦追踪和同步输出。
    在 dumb 终端或管道输出中静默跳过。
    """  # _enable_windows_vt_processing()
    if _is_dumb_terminal():
        # Dumb terminals (e.g. 'linux' console, 'dumb', piped output)
        # don't support alternate screen or mouse tracking.
        return
    sys.stdout.write(DISABLE_MOUSE_TRACKING + ENTER_ALT_SCREEN + ERASE_SCREEN_AND_HOME + ENABLE_MOUSE_TRACKING + ENABLE_BRACKETED_PASTE + ENABLE_FOCUS_TRACKING + ENABLE_SYNC_OUTPUT)
    sys.stdout.flush()


def exit_alternate_screen() -> None:
    """退出备用屏幕并恢复主缓冲区。

    禁用鼠标追踪、括号粘贴模式、聚焦追踪和同步输出。
    在 dumb 终端中静默跳过。
    """  # if _is_dumb_terminal():
        return
    sys.stdout.write(DISABLE_MOUSE_TRACKING + EXIT_ALT_SCREEN + DISABLE_BRACKETED_PASTE + ENABLE_SYNC_OUTPUT + DISABLE_FOCUS_TRACKING)
    sys.stdout.flush()


def clear_screen() -> None:
    """清除整个终端屏幕并将光标移至左上角。"""  # sys.stdout.write("[H[J")
    sys.stdout.flush()
