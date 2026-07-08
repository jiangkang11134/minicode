"""SmartCode TUI 输入处理模块。

提供跨平台的原始模式键盘输入读取功能，包括 Windows 和 Unix 系统的适配。
包含工具快捷方式执行、输入分发以及 Agent 回合的后台运行逻辑。
"""

from __future__ import annotations
from collections import defaultdict
import logging
import os
import sys
import threading
from typing import Any, Callable
from minicode.tui.state import AggregatedEditProgress, ScreenState, TtyAppArgs
from minicode.cli_commands import try_handle_local_command, find_matching_slash_commands
from minicode.agent_loop import run_agent_turn
from minicode.context_manager import save_context_state
from minicode.history import save_history_entries
from minicode.local_tool_shortcuts import parse_local_tool_shortcut
from minicode.prompt import build_system_prompt_bundle
from minicode.tooling import ToolContext
from minicode.types import RuntimeEvent
from minicode.tui.session_flow import refresh_tty_session_snapshot
from minicode.tui.tool_helpers import _summarize_tool_input, _is_file_edit_tool, _extract_path_from_tool_input, _summarize_collapsed_tool_body
from minicode.tui.tool_lifecycle import _push_transcript_entry, _update_tool_entry, _update_transcript_entry, _append_to_transcript_entry, _collapse_tool_entry, _finalize_dangling_running_tools, _get_running_tool_entries, _schedule_tool_auto_collapse

logger = logging.getLogger("minicode.input_handler")

# Cross-platform raw mode stdin
# ---------------------------------------------------------------------------


# Windows msvcrt scan-code → ANSI escape sequence mapping.
# msvcrt.getwch() returns a two-char sequence for special keys:
#   prefix ('\x00' or '\xe0') + scan-code byte.
# We translate these to the ANSI sequences that input_parser.py already
# understands.
_WIN_SCANCODE_TO_ANSI: dict[int, str] = {
    72: "\x1b[A",    # Up
    80: "\x1b[B",    # Down
    77: "\x1b[C",    # Right
    75: "\x1b[D",    # Left
    71: "\x1b[H",    # Home
    79: "\x1b[F",    # End
    73: "\x1b[5~",   # Page Up
    81: "\x1b[6~",   # Page Down
    83: "\x1b[3~",   # Delete
    82: "\x1b[2~",   # Insert
    # Alt+Arrow (returned with \x00 prefix on some terminals)
    152: "\x1b[1;3A",  # Alt+Up
    160: "\x1b[1;3B",  # Alt+Down
    157: "\x1b[1;3C",  # Alt+Right
    155: "\x1b[1;3D",  # Alt+Left
    # Ctrl+Arrow
    141: "\x1b[1;5A",  # Ctrl+Up
    145: "\x1b[1;5B",  # Ctrl+Down
    116: "\x1b[1;5C",  # Ctrl+Right
    115: "\x1b[1;5D",  # Ctrl+Left
}


def _win_read_one_key() -> str:
    """从 Windows msvcrt 读取一个逻辑键，将特殊键转换为 ANSI 转义序列。

    返回:
        表示按键的字符串；若无可用按键则返回空字符串。
    """  # import msvcrt

    if not msvcrt.kbhit():
        return ""

    ch = msvcrt.getwch()

    # Special-key prefix: next char is a scan code
    if ch in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            scan = ord(msvcrt.getwch())
        else:
            # Prefix arrived alone (rare) — treat as Escape
            return "\x1b"
        return _WIN_SCANCODE_TO_ANSI.get(scan, "")

    # Ctrl+C → keep as '\x03' so parse_input_chunk handles it
    return ch


def _read_raw_char() -> str:
    """以跨平台方式从标准输入读取单个原始字符。

    在 Windows 上委托给 _win_read_one_key，在 Unix 上使用 select 和 os.read
    实现非阻塞读取。

    返回:
        读取到的字符；若无可用输入则返回空字符串。
    """
    if sys.platform == "win32":
        return _win_read_one_key()
    else:
        import select

        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            # Use os.read() to bypass Python's TextIOWrapper buffering.
            # In raw/cbreak mode the kernel returns whatever bytes are
            # available, so os.read() won't block.
            data = os.read(fd, 4096)
            return data.decode("utf-8", errors="replace") if data else ""
        return ""


def _read_raw_chunk() -> str:
    """读取所有可用的原始字符，作为一个完整块返回。

    在 Windows 上循环调用 _win_read_one_key 收集所有按键；
    在 Unix 上通过 select 和 os.read 一次性读取全部可用字节。

    返回:
        包含所有可用输入的字符串；若无可用输入则返回空字符串。
    """
    if sys.platform == "win32":
        result = ""
        while True:
            ch = _win_read_one_key()
            if not ch:
                break
            result += ch
        return result
    else:
        import select

        fd = sys.stdin.fileno()
        # First wait with a timeout for initial data
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return ""
        # Read all available bytes in one go.  In raw mode the kernel
        # delivers whatever has arrived so far; os.read() returns
        # immediately with 1..N bytes.
        data = os.read(fd, 4096)
        if not data:
            return ""
        # Drain any remaining bytes without blocking
        while True:
            ready2, _, _ = select.select([fd], [], [], 0)
            if not ready2:
                break
            more = os.read(fd, 4096)
            if not more:
                break
            data += more
        return data.decode("utf-8", errors="replace")


class _RawModeContext:
    """终端原始模式的上下文管理器。

    在 Unix 上通过 termios/tty 将标准输入切换为原始模式，退出时恢复。
    在 Windows 上 msvcrt 原生支持逐个字符输入，但需要确保控制台代码页
    设置为 UTF-8 并启用 VT 处理。
    """

    def __init__(self) -> None:
        """初始化原始模式上下文管理器。

        记录旧的终端设置、控制台代码页和 SIGWINCH 信号处理函数。
        """  # self._old_settings: Any = None
        self._old_cp: int | None = None
        self._old_sigwinch: Any = None

    def __enter__(self) -> _RawModeContext:
        """进入原始模式。

        在 Windows 上启用 VT 处理并设置控制台为 UTF-8 代码页。
        在 Unix 上通过 termios 配置终端为原始模式（禁用回显、规范模式、
        信号生成等），并注册 SIGWINCH 信号处理以在窗口大小变化时更新
        终端尺寸缓存。

        返回:
            自身实例。
        """
        if sys.platform == "win32":
            # Ensure VT processing is active (idempotent)
            from minicode.tui.screen import _enable_windows_vt_processing
            _enable_windows_vt_processing()
            # Switch console to UTF-8 code page for proper Unicode handling
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                self._old_cp = kernel32.GetConsoleOutputCP()
                kernel32.SetConsoleOutputCP(65001)  # UTF-8
            except Exception:
                pass
        else:
            import termios
            import signal

            fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)

            # Wire SIGWINCH to invalidate terminal size cache on resize
            try:
                import signal

                def _on_resize(signum, frame):
                    from minicode.tui.chrome import invalidate_terminal_size_cache
                    invalidate_terminal_size_cache()
                self._old_sigwinch = signal.signal(signal.SIGWINCH, _on_resize)
            except (ImportError, AttributeError):
                pass  # Windows or no SIGWINCH support
            # Input flags: disable CR→NL translation and XON/XOFF flow control,
            # strip high bit, and break signal generation.
            new[0] &= ~(
                termios.BRKINT | termios.ICRNL | termios.INPCK
                | termios.ISTRIP | termios.IXON
            )
            # Output flags: KEEP OPOST so that \n → \r\n translation still
            # works.  tty.setraw() clears OPOST which causes "staircase"
            # output on Linux/macOS — every newline only moves down without
            # returning the cursor to column 0.
            # new[1] is intentionally left untouched.
            # Control flags: set 8-bit chars
            new[2] &= ~(termios.CSIZE | termios.PARENB)
            new[2] |= termios.CS8
            # Local flags: disable echo, canonical mode, extended processing,
            # and signal generation from keys (Ctrl-C, Ctrl-Z).
            new[3] &= ~(
                termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG
            )
            # Special characters: read returns after 1 byte, no timeout.
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        return self

    def __exit__(self, *_: Any) -> None:
        """退出原始模式，恢复终端到原始状态。

        在 Windows 上恢复控制台代码页。
        在 Unix 上通过 termios 恢复标准输入的原始终端设置，
        并恢复 SIGWINCH 信号处理函数。
        """
        if sys.platform == "win32":
            if self._old_cp is not None:
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetConsoleOutputCP(self._old_cp)  # type: ignore[attr-defined]
                except Exception:
                    pass
        elif self._old_settings is not None:
            import termios
            import signal

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            if getattr(self, '_old_sigwinch', None) is not None:
                try:
                    import signal
                    signal.signal(signal.SIGWINCH, self._old_sigwinch)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Tool shortcut execution
# ---------------------------------------------------------------------------


def _execute_tool_shortcut(
    args: TtyAppArgs,
    state: ScreenState,
    tool_name: str,
    tool_input: Any,
    rerender: Callable[[], None],
) -> None:
    """执行一个工具快捷方式。

    设置忙碌状态，调用工具执行，记录执行结果到转录中，
    并在完成后折叠工具条目并清理状态。

    参数:
        args: TTY 应用参数
        state: 屏幕状态对象
        tool_name: 要执行的工具名称
        tool_input: 工具的输入参数
        rerender: 触发界面重绘的回调函数
    """  # state.is_busy = True
    state.status = f"Running {tool_name}..."
    state.active_tool = tool_name
    entry_id = _push_transcript_entry(
        state,
        kind="tool",
        toolName=tool_name,
        status="running",
        body=_summarize_tool_input(tool_name, tool_input),
    )
    rerender()

    try:
        result = args.tools.execute(
            tool_name,
            tool_input,
            context=ToolContext(
                cwd=args.cwd,
                permissions=args.permissions,
                session=state.session,
            ),
        )
        state.recent_tools.append({
            "name": tool_name,
            "status": "success" if result.ok else "error",
        })
        output = result.output if result.ok else f"ERROR: {result.output}"
        _update_tool_entry(state, entry_id, "success" if result.ok else "error", output)
        _collapse_tool_entry(state, entry_id, _summarize_collapsed_tool_body(output))
        state.transcript_scroll_offset = 0
    finally:
        state.is_busy = False
        state.active_tool = None
        _finalize_dangling_running_tools(state)
        if not _get_running_tool_entries(state):
            state.status = None


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _handle_input(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    submitted_raw_input: str | None = None,
) -> bool:
    """处理用户输入的主分发函数。

    根据输入的文本执行不同操作：/exit 退出程序，/tools 列出工具，
    /collapse 折叠展开的工具输出，本地命令、工具快捷方式、未知斜杠命令提示，
    以及最终的 Agent 回合调用。如果是非 /exit 的输入则返回 False。

    参数:
        args: TTY 应用参数
        state: 屏幕状态对象
        rerender: 触发界面重绘的回调函数
        submitted_raw_input: 可选的预提交输入文本，若提供则替换 state.input

    返回:
        如果输入为 /exit 则返回 True，否则返回 False
    """
    if state.is_busy:
        # Animated spinner during tool execution
        import time
        spinners = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        tick = int(time.monotonic() * 8) % len(spinners)
        spin = spinners[tick]
        state.status = (
            f"{spin} {state.active_tool}..."
            if state.active_tool
            else f"{spin} Running..."
        )
        return False

    input_text = (submitted_raw_input if submitted_raw_input is not None else state.input).strip()
    if not input_text:
        return False
    if input_text == "/exit":
        return True

    memory_mgr = getattr(args, "memory_manager", None)
    if memory_mgr is not None:
        memory_result = memory_mgr.handle_user_memory_input(input_text)
        if memory_result is not None:
            _push_transcript_entry(state, kind="user", body=input_text)
            _push_transcript_entry(state, kind="assistant", body=memory_result)
            return False

    # History
    if not state.history or state.history[-1] != input_text:
        state.history.append(input_text)
        save_history_entries(state.history)
    state.history_index = len(state.history)
    state.history_draft = ""

    # Autosave trigger
    if state.autosave:
        state.autosave.mark_dirty()

    # /tools
    if input_text == "/tools":
        _push_transcript_entry(
            state,
            kind="assistant",
            body="\n".join(
                f"{t.name}: {t.description}" for t in args.tools.list()
            ),
        )
        return False

    # /collapse — collapse every expanded tool-output block in the transcript
    if input_text == "/collapse":
        collapsed = 0
        for entry in state.transcript:
            if getattr(entry, "kind", None) == "tool" and not getattr(entry, "collapsed", False):
                entry.collapsed = True
                if not getattr(entry, "collapsedSummary", None):
                    entry.collapsedSummary = "output collapsed"
                collapsed += 1
        _push_transcript_entry(
            state,
            kind="assistant",
            body=(
                f"Collapsed {collapsed} tool-output block(s)."
                if collapsed
                else "No expanded tool-output blocks to collapse."
            ),
        )
        return False

    # Local commands
    if state.session is not None:
        refresh_tty_session_snapshot(args, state)
    local_result = try_handle_local_command(
        input_text,
        tools=args.tools,
        cwd=args.cwd,
        session=state.session,
    )
    if local_result is not None:
        _push_transcript_entry(state, kind="assistant", body=local_result)
        return False

    # Tool shortcuts
    shortcut = parse_local_tool_shortcut(input_text)
    if shortcut:
        _execute_tool_shortcut(
            args, state, shortcut["toolName"], shortcut["input"], rerender
        )
        return False

    # Unknown slash commands
    if input_text.startswith("/"):
        matches = find_matching_slash_commands(input_text)
        _push_transcript_entry(
            state,
            kind="assistant",
            body=(
                f"Unknown command. Did you mean:\n{chr(10).join(matches)}"
                if matches
                else "Unknown command. Type /help to see available commands."
            ),
        )
        return False

    # Agent turn
    _push_transcript_entry(state, kind="user", body=input_text)
    state.transcript_scroll_offset = 0
    state.status = "Thinking..."
    state.is_busy = True

    # Hook: user input
    from minicode.hooks import HookEvent, fire_hook_sync
    fire_hook_sync(HookEvent.USER_INPUT, user_input=input_text)

    # Prompt injection detection (input layer)
    from minicode.auto_mode import AutoModeChecker
    is_injection, injection_reason = AutoModeChecker.detect_prompt_injection(input_text)
    if is_injection:
        logger.warning("Potential prompt injection detected: %s", injection_reason)
        # Don't block, but add a system message warning
        args.messages.append({
            "role": "system",
            "content": f"[SECURITY WARNING] Potential prompt injection pattern detected: {injection_reason}. Proceed with caution and verify all outputs."
        })

    # Update app state
    if state.app_state:
        from minicode.state import set_busy
        state.app_state.set_state(set_busy())

    rerender()

    pending_tool_entries: dict[str, list[int]] = defaultdict(list)
    aggregated_edit_by_key: dict[str, AggregatedEditProgress] = {}
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress] = {}

    # Refresh system prompt
    bundle = build_system_prompt_bundle(
        args.cwd,
        args.permissions.get_summary(),
        {
            "skills": args.tools.get_skills(),
            "mcpServers": args.tools.get_mcp_servers(),
            "memory_context": memory_mgr.get_relevant_context(query=input_text) if memory_mgr is not None else "",
            "runtime": args.runtime,
        },
    )
    args.prompt_bundle = bundle
    args.product_snapshot = bundle.product_snapshot
    args.messages[0] = {
        "role": "system",
        "content": bundle.prompt,
    }
    args.messages.append({"role": "user", "content": input_text})

    active_stream_entry_id = None
    pending_runtime_progress: str | None = None

    def on_assistant_stream_chunk(content: str) -> None:
        """处理助手流式输出的文本块。

        将收到的文本块追加到当前助手转录条目中，若尚无条目则新建一个。

        参数:
            content: 助手输出的文本块
        """
        nonlocal active_stream_entry_id
        if active_stream_entry_id is None:
            active_stream_entry_id = _push_transcript_entry(state, kind="assistant", body=content)
        else:
            _append_to_transcript_entry(state, active_stream_entry_id, content)
        state.transcript_scroll_offset = 0
        rerender()

    def on_assistant_message(content: str) -> None:
        """处理助手完整的输出消息。

        触发助手输出钩子，执行输出安全检查，更新或新建转录条目。

        参数:
            content: 助手的完整输出消息
        """
        nonlocal active_stream_entry_id
        # Hook: assistant output
        fire_hook_sync(HookEvent.ASSISTANT_OUTPUT, assistant_output=content[:500])
        # Output safety check (output layer)
        from minicode.auto_mode import AutoModeChecker
        is_unsafe, unsafe_reason = AutoModeChecker.classify_output_safety(content)
        if is_unsafe:
            logger.warning("Potentially unsafe output detected: %s", unsafe_reason)
        if active_stream_entry_id is not None:
            _update_transcript_entry(state, active_stream_entry_id, body=content)
            active_stream_entry_id = None
        else:
            _push_transcript_entry(state, kind="assistant", body=content)
        state.transcript_scroll_offset = 0
        rerender()

    def on_progress_message(content: str) -> None:
        """处理进度消息。

        在有活跃流条目时将其转换为进度类型条目，否则新建进度条目。

        参数:
            content: 进度消息文本
        """
        nonlocal active_stream_entry_id, pending_runtime_progress
        if pending_runtime_progress == content:
            pending_runtime_progress = None
            return
        if active_stream_entry_id is not None:
            _update_transcript_entry(
                state,
                active_stream_entry_id,
                kind="progress",
                body=content,
                category=None,
                runtimeKind=None,
                runtimeStep=None,
                runtimePhase=None,
                runtimeStopReason=None,
                runtimeVerificationFocus=None,
            )
            active_stream_entry_id = None
        else:
            _push_transcript_entry(
                state,
                kind="progress",
                body=content,
                category=None,
                runtimeKind=None,
                runtimeStep=None,
                runtimePhase=None,
                runtimeStopReason=None,
                runtimeVerificationFocus=None,
            )
        state.transcript_scroll_offset = 0
        rerender()

    def on_runtime_event(event: RuntimeEvent) -> None:
        """处理运行时事件。

        将运行时事件转换为进度类型转录条目，携带运行时元数据。

        参数:
            event: 运行时事件对象
        """
        nonlocal active_stream_entry_id, pending_runtime_progress
        pending_runtime_progress = event.message
        if active_stream_entry_id is not None:
            _update_transcript_entry(
                state,
                active_stream_entry_id,
                kind="progress",
                body=event.message,
                category="runtime",
                runtimeKind=event.category,
                runtimeStep=event.step,
                runtimePhase=event.phase or None,
                runtimeStopReason=event.stop_reason or None,
                runtimeVerificationFocus=event.verification_focus or None,
            )
            active_stream_entry_id = None
        else:
            _push_transcript_entry(
                state,
                kind="progress",
                body=event.message,
                category="runtime",
                runtimeKind=event.category,
                runtimeStep=event.step,
                runtimePhase=event.phase or None,
                runtimeStopReason=event.stop_reason or None,
                runtimeVerificationFocus=event.verification_focus or None,
            )
        state.transcript_scroll_offset = 0
        rerender()

    def on_tool_start(tool_name: str, tool_input: Any) -> None:
        """处理工具开始执行事件。

        设置工具运行状态，创建转录条目，支持对同一文件编辑工具的
        聚合展示（累计已完成和总操作数）。

        参数:
            tool_name: 工具名称
            tool_input: 工具输入参数
        """
        state.status = f"Running {tool_name}..."
        state.active_tool = tool_name
        state.tool_start_time = time.monotonic()  # 记录工具启动时间

        target_path = _extract_path_from_tool_input(tool_input)
        can_aggregate = _is_file_edit_tool(tool_name) and target_path is not None

        if can_aggregate:
            key = f"{tool_name}:{target_path}"
            existing = aggregated_edit_by_key.get(key)
            if existing:
                existing.total += 1
                existing.last_output = _summarize_tool_input(tool_name, tool_input)
                entry_id = existing.entry_id
                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if existing.errors > 0 else "running",
                    f"Aggregated {tool_name} for {target_path}\nCompleted: {existing.completed}/{existing.total}",
                )
            else:
                entry_id = _push_transcript_entry(
                    state,
                    kind="tool",
                    toolName=tool_name,
                    status="running",
                    body=_summarize_tool_input(tool_name, tool_input),
                )
                progress = AggregatedEditProgress(
                    entry_id=entry_id,
                    tool_name=tool_name,
                    path=target_path,
                    total=1,
                    completed=0,
                    errors=0,
                    last_output=_summarize_tool_input(tool_name, tool_input),
                )
                aggregated_edit_by_key[key] = progress
                aggregated_edit_by_entry_id[entry_id] = progress
        else:
            entry_id = _push_transcript_entry(
                state,
                kind="tool",
                toolName=tool_name,
                status="running",
                body=_summarize_tool_input(tool_name, tool_input),
            )

        pending_tool_entries[tool_name].append(entry_id)
        state.transcript_scroll_offset = 0
        rerender()

    def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
        """处理工具执行结果。

        更新工具执行时间记录，更新聚合编辑进度，处理错误恢复引导建议，
        整理输出并更新转录条目。

        参数:
            tool_name: 工具名称
            output: 工具输出文本
            is_error: 是否执行出错
        """
        # Track tool execution time
        elapsed_note = ""
        if state.tool_start_time is not None:
            elapsed_secs = time.monotonic() - state.tool_start_time
            if elapsed_secs > 0.5:
                if elapsed_secs < 60:
                    elapsed_note = f"[{elapsed_secs:.1f}s] "
                else:
                    elapsed_note = f"[{elapsed_secs/60:.1f}m] "

        pending = pending_tool_entries.get(tool_name, [])
        entry_id = pending.pop(0) if pending else None
        if entry_id is not None:
            aggregated = aggregated_edit_by_entry_id.get(entry_id)
            if aggregated and aggregated.tool_name == tool_name:
                aggregated.completed += 1
                if is_error:
                    aggregated.errors += 1
                aggregated.last_output = output
                done = aggregated.completed >= aggregated.total
                if done:
                    state.recent_tools.append({
                        "name": f"{tool_name} x{aggregated.total}",
                        "status": "error" if aggregated.errors > 0 else "success",
                    })
                body = (
                    "\n".join([
                        f"Aggregated {tool_name} for {aggregated.path}",
                        f"Operations: {aggregated.total}, errors: {aggregated.errors}",
                        f"Last result: {aggregated.last_output}",
                    ])
                    if done
                    else f"Aggregated {tool_name} for {aggregated.path}\nCompleted: {aggregated.completed}/{aggregated.total}"
                )
                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if aggregated.errors > 0 else ("success" if done else "running"),
                    body,
                )
                if done:
                    _collapse_tool_entry(state, entry_id, _summarize_collapsed_tool_body(body))
                    aggregated_edit_by_entry_id.pop(entry_id, None)
                    aggregated_edit_by_key.pop(f"{tool_name}:{aggregated.path}", None)
            else:
                state.recent_tools.append({
                    "name": tool_name,
                    "status": "error" if is_error else "success",
                })

                # 错误恢复引导
                display_output = elapsed_note + output
                if is_error:
                    suggestions = []
                    output_lower = output.lower()
                    if "not found" in output_lower or "no such file" in output_lower:
                        suggestions.append("💡 File not found. Try /ls to see available files")
                    elif "permission" in output_lower or "denied" in output_lower:
                        suggestions.append("💡 Permission denied. Check file access rights")
                    elif "syntax" in output_lower or "error" in output_lower:
                        suggestions.append("💡 Error occurred. Review the output and fix issues")

                    if suggestions:
                        display_output = f"ERROR: {output}\n\n" + "\n".join(suggestions)
                    else:
                        display_output = f"ERROR: {output}"

                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if is_error else "success",
                    display_output,
                )
                _schedule_tool_auto_collapse(
                    state,
                    entry_id,
                    display_output,
                    rerender,
                )

        state.active_tool = None
        remaining = sum(len(v) for v in pending_tool_entries.values())
        if remaining > 0:
            state.status = f"{remaining} tool(s) still running..."
        else:
            state.status = None
        state.transcript_scroll_offset = 0
        rerender()

    args.permissions.begin_turn()

    active_thinking_entry_id = None

    def on_thinking_chunk(content: str) -> None:
        """处理模型思考过程的流式文本块。

        将思考文本追加到进度类型转录条目中，若尚无条目则新建一个。

        参数:
            content: 思考过程的文本块
        """
        nonlocal active_thinking_entry_id
        if active_thinking_entry_id is None:
            active_thinking_entry_id = _push_transcript_entry(
                state, kind="progress", body=f"∴ Thinking…\n{content}"
            )
        else:
            _append_to_transcript_entry(state, active_thinking_entry_id, content)
        state.transcript_scroll_offset = 0
        rerender()

    # Run agent turn in background thread to keep UI responsive
    agent_error = None
    agent_result: dict = {"messages": None}
    agent_thread_lock = threading.Lock()

    def _run_agent_background():
        """在后台线程中运行 Agent 回合。

        调用 run_agent_turn 执行完整的 Agent 逻辑，更新消息和上下文，
        处理异常，并在结束后清理忙碌状态和工具状态。
        """
        nonlocal agent_error, agent_result
        try:
            next_messages = run_agent_turn(
                model=args.model,
                tools=args.tools,
                messages=list(args.messages),  # Copy to avoid race condition
                cwd=args.cwd,
                permissions=args.permissions,
                session=state.session,
                on_tool_start=on_tool_start,
                on_tool_result=on_tool_result,
                on_assistant_message=on_assistant_message,
                on_progress_message=on_progress_message,
                on_runtime_event=on_runtime_event,
                on_assistant_stream_chunk=on_assistant_stream_chunk,
                on_thinking_chunk=on_thinking_chunk,
                store=state.app_state,
                context_manager=args.context_manager,
                runtime=args.runtime,
            )
            if args.context_manager is not None:
                args.context_manager.messages = next_messages
                save_context_state(args.context_manager)
            with agent_thread_lock:
                agent_result["messages"] = next_messages
        except Exception as e:
            agent_error = e
        finally:
            args.permissions.end_turn()
            with agent_thread_lock:
                agent_result["done"] = True
            state.is_busy = False
            state.active_tool = None
            state.status = None
            rerender()

    agent_thread = threading.Thread(target=_run_agent_background, daemon=True)
    agent_thread.start()
    state.agent_thread = agent_thread
    # Assign lock BEFORE result — the main loop checks agent_result first,
    # so the lock must already be available to avoid AttributeError.
    state.agent_lock = agent_thread_lock
    state.agent_result = agent_result

    # Return immediately - agent runs in background
    return False


# ---------------------------------------------------------------------------
