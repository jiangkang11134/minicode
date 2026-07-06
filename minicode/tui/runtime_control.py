from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from minicode.tui.chrome import invalidate_terminal_size_cache
from minicode.tui.screen import enter_alternate_screen, exit_alternate_screen, hide_cursor, show_cursor


class _ThrottledRenderer:
    """带节流控制的渲染调度器。

    确保渲染函数不会在设定的最小时间间隔内被频繁调用。
    支持三种触发方式：request（标记待渲染）、flush（按节流规则执行）、
    force（忽略节流立即执行）。
    """

    __slots__ = ("_render_fn", "_min_interval", "_pending", "_last_render_time", "_lock")

    def __init__(self, render_fn: Callable[[], None], min_interval: float = 0.033) -> None:
        """初始化节流渲染器。

        参数:
            render_fn: 实际的渲染回调函数。
            min_interval: 两次渲染之间的最短时间间隔（秒），默认约 30 FPS。
        """  # self._render_fn = render_fn
        self._min_interval = min_interval
        self._pending = False
        self._last_render_time: float = 0.0
        self._lock = threading.Lock()

    def request(self) -> None:
        """请求一次渲染（标记待渲染状态，不立即执行）。"""  # with self._lock:
            self._pending = True

    def flush(self) -> None:
        """尝试执行渲染。

        仅在有待渲染标记且距离上次渲染已超过最小间隔时执行，
        否则跳过。线程安全。
        """  # now = time.monotonic()
        with self._lock:
            if not self._pending:
                return
            if now - self._last_render_time < self._min_interval:
                return
            self._pending = False
            self._last_render_time = now
        self._render_fn()

    def force(self) -> None:
        """强制立即渲染，忽略节流间隔限制。"""  # with self._lock:
            self._pending = False
            self._last_render_time = time.monotonic()
        self._render_fn()


def enter_tty_runtime() -> None:
    """进入 TTY 运行时环境。

    切换到终端备用屏幕（alternate screen buffer）并隐藏光标。
    """  # enter_alternate_screen()
    hide_cursor()


def exit_tty_runtime(prev_sigwinch: object | None) -> None:
    """退出 TTY 运行时环境并恢复终端状态。

    恢复之前保存的 SIGWINCH 信号处理器（非 Windows 平台），
    显示光标并退出备用屏幕。

    参数:
        prev_sigwinch: 之前安装的 SIGWINCH 处理器对象，若为 None 则不恢复。
    """  # if prev_sigwinch is not None and sys.platform != "win32":
        import signal as _signal

        _signal.signal(_signal.SIGWINCH, prev_sigwinch)  # type: ignore[arg-type]
    show_cursor()
    exit_alternate_screen()


def install_sigwinch_rerender(throttled: _ThrottledRenderer) -> object | None:
    """安装 SIGWINCH 信号处理器以在终端尺寸变化时触发重渲染。

    当终端窗口大小改变时，使尺寸缓存失效并请求一次节流渲染。
    仅可在非 Windows 的主线程中安装。

    参数:
        throttled: 节流渲染器实例，用于请求重渲染。

    返回:
        之前的 SIGWINCH 处理器对象，若安装失败则返回 None。
    """  # if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        return None

    import signal as _signal

    def _on_sigwinch(_signum: int, _frame: object) -> None:
        invalidate_terminal_size_cache()
        throttled.request()

    try:
        return _signal.signal(_signal.SIGWINCH, _on_sigwinch)
    except (OSError, ValueError):
        return None
