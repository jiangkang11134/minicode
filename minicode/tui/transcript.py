"""MiniCode 转录面板模块。

提供将对话转录条目渲染为终端可视内容的能力，包括：
- 根据终端宽度自动换行（支持 CJK/全宽/emoji 字符宽度计算）
- 运行时（runtime）条目的标签、追踪和摘要生成
- 工具调用输出的预览和截断
- 基于可视行数的窗口滚动与缓存布局
- 8 段 Unicode 字符的精细滚动条渲染
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from .chrome import (
    _cached_terminal_size,
    RESET,
    DIM,
    ICON_DIVIDER,
    ICON_DOT,
    _looks_like_diff_block,
    colorize_unified_diff_block,
)
from .markdown import render_markdownish
from .theme import theme
from .types import TranscriptEntry

# Pre-build the separator string once (immutable)
_SEPARATOR = f"  {DIM}{ICON_DOT} {ICON_DIVIDER * 3} {ICON_DOT}{RESET}"
_SEPARATOR_LINES = ["", _SEPARATOR, ""]
_SEPARATOR_LINE_COUNT = 3

# Tool names that produce diff output
_DIFF_TOOLS = frozenset({"edit_file", "patch_file", "diff_viewer"})

# Tool output preview limits (match Rust TOOL_PREVIEW_LINES / TOOL_PREVIEW_CHARS)
_TOOL_PREVIEW_LINES = 6
_TOOL_PREVIEW_CHARS = 180


# ---------------------------------------------------------------------------
# Visual-line wrapping (port of TS charDisplayWidth / stringDisplayWidth /
# wrapPanelBodyLine). The transcript scroll offset must count width-wrapped
# VISUAL rows, not just newline-split logical lines, otherwise long lines make
# the scrollbar/scroll offset under-count (TS parity).
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """移除字符串中的 ANSI 转义序列。

    参数:
        text: 可能包含 ANSI 转义码的文本

    返回:
        移除了所有 ANSI 转义码后的纯文本
    """  # return _ANSI_RE.sub("", text)


def _char_display_width(ch: str) -> int:
    """返回单个字符的显示列宽（CJK/全宽/emoji 占 2 列）。

    参数:
        ch: 单个字符

    返回:
        显示宽度，1 或 2
    """  # code = ord(ch)
    if 0x1100 <= code <= 0x115F or code in (0x2329, 0x232A):
        return 2
    if 0x2E80 <= code <= 0xA4CF and code != 0x303F:
        return 2
    if 0xAC00 <= code <= 0xD7A3:
        return 2
    if 0xF900 <= code <= 0xFAFF:
        return 2
    if 0xFE10 <= code <= 0xFE19 or 0xFE30 <= code <= 0xFE6F:
        return 2
    if 0xFF00 <= code <= 0xFF60 or 0xFFE0 <= code <= 0xFFE6:
        return 2
    if 0x1F300 <= code <= 0x1FAF6:
        return 2
    if 0x20000 <= code <= 0x3FFFD:
        return 2
    return 1


def _string_display_width(text: str) -> int:
    """计算移除 ANSI 转义后文本的显示总宽度。

    参数:
        text: 要计算宽度的文本（可含 ANSI 转义码）

    返回:
        显示列宽总和
    """  # return sum(_char_display_width(ch) for ch in _strip_ansi(text))


def _transcript_panel_width() -> int:
    """获取转录面板的可用宽度（终端列数和 60 的最小值）。

    返回:
        面板宽度列数，最小为 60
    """  # cols, _ = _cached_terminal_size()
    return max(60, cols)


def _wrap_panel_body_line(line: str, width: int) -> list[str]:
    """将已渲染的行按指定显示宽度换行（移植自 TS 的 ``wrapPanelBodyLine``）。

    以 ``inner = width - 4`` 为实际内宽，为面板内边距留出空间；
    按字符显示宽度贪心打包。能容纳的行原样返回（保留 ANSI 样式），
    超长的行按纯文本分段换行返回。

    参数:
        line:  需要换行的文本行（可含 ANSI 转义码）
        width: 面板总宽度（列数）

    返回:
        换行后的文本行列表
    """  # inner = max(0, width - 4)
    if inner <= 0:
        return [""]
    if _string_display_width(line) <= inner:
        return [line]
    parts: list[str] = []
    current = ""
    current_width = 0
    for ch in _strip_ansi(line):
        ch_width = _char_display_width(ch)
        if current_width + ch_width > inner:
            parts.append(current)
            current = ch
            current_width = ch_width
            continue
        current += ch
        current_width += ch_width
    if current:
        parts.append(current)
    return parts


def _is_runtime_progress_message(text: str) -> bool:
    """判断文本是否为运行时进度消息。

    检查标准化后的文本是否以特定的运行时前缀开头，
    或包含与运行时拓宽/升级相关的关键词。

    参数:
        text: 要检查的文本

    返回:
        若是运行时进度消息返回 True，否则 False
    """  # normalized = " ".join((text or "").split()).lower()
    runtime_prefixes = (
        "runtime phase:",
        "verification guard:",
        "compacted context for the current runtime phase.",
        "depth stalled;",
    )
    if normalized.startswith(runtime_prefixes):
        return True
    return (
        "widened mode is active" in normalized
        or "widening is now available" in normalized
        or "escalation trigger:" in normalized
    )


def _is_runtime_entry(entry: TranscriptEntry) -> bool:
    """判断转录条目是否属于运行时类别。

    参数:
        entry: 转录条目

    返回:
        若条目类别为 runtime 或内容为运行时进度消息则返回 True
    """  # return entry.category == "runtime" or _is_runtime_progress_message(entry.body)


def _runtime_label_text(entry: TranscriptEntry) -> str:
    """获取运行时条目的标签文本。

    参数:
        entry: 转录条目

    返回:
        格式如 "runtime:<kind>" 或默认 "runtime" 的标签
    """  # runtime_kind = (entry.runtimeKind or "").strip()
    return f"runtime:{runtime_kind}" if runtime_kind else "runtime"


def _runtime_meta_suffix(entry: TranscriptEntry) -> str:
    """获取运行时条目的元信息后缀字符串。

    参数:
        entry: 转录条目

    返回:
        包含 step/phase/reason/verify 等信息的后缀字符串，非运行时条目返回空串
    """  # if not _is_runtime_entry(entry):
        return ""

    meta_parts: list[str] = []
    if entry.runtimeStep is not None:
        meta_parts.append(f"step={entry.runtimeStep}")
    if entry.runtimePhase:
        meta_parts.append(f"phase={entry.runtimePhase}")
    if entry.runtimeStopReason:
        meta_parts.append(f"reason={entry.runtimeStopReason}")
    if entry.runtimeVerificationFocus:
        meta_parts.append(f"verify={entry.runtimeVerificationFocus}")
    return f" [{' '.join(meta_parts)}]" if meta_parts else ""


def _runtime_trace_token(entry: TranscriptEntry) -> str | None:
    """为运行时条目生成追踪令牌字符串。

    根据 runtimeKind 生成不同格式的令牌（如 phase:xxx@step、guard:xxx@step 等），
    用于构建运行时追踪摘要。

    参数:
        entry: 转录条目

    返回:
        追踪令牌字符串，非运行时条目返回 None
    """  # if not _is_runtime_entry(entry):
        return None

    step_suffix = f"@{entry.runtimeStep}" if entry.runtimeStep is not None else ""
    runtime_kind = (entry.runtimeKind or "").strip().lower()

    if runtime_kind == "phase":
        detail = (entry.runtimePhase or "unknown").strip() or "unknown"
        return f"phase:{detail}{step_suffix}"
    if runtime_kind == "guard":
        detail = (
            (entry.runtimeVerificationFocus or "").strip()
            or (entry.runtimeStopReason or "").strip()
            or "verification"
        )
        return f"guard:{detail}{step_suffix}"
    if runtime_kind == "widening":
        detail = (entry.runtimeStopReason or "").strip() or "escalation"
        return f"widen:{detail}{step_suffix}"
    if runtime_kind == "stop":
        detail = (entry.runtimeStopReason or "").strip() or "done"
        return f"stop:{detail}{step_suffix}"
    if runtime_kind == "compaction":
        detail = (entry.runtimePhase or "").strip() or "context"
        return f"compact:{detail}{step_suffix}"
    if runtime_kind == "recovery":
        detail = (entry.runtimeStopReason or "").strip() or "resume"
        return f"recover:{detail}{step_suffix}"

    return f"{runtime_kind or 'runtime'}{step_suffix}"


def _runtime_trace_summary(entries: list[TranscriptEntry]) -> str | None:
    """生成运行时追踪摘要字符串（以箭头连接各令牌）。

    参数:
        entries: 转录条目列表

    返回:
        箭头连接的追踪摘要（如 "phase:init@1 -> widen:escalation@3"），
        无追踪信息时返回 None
    """  # trace_tokens: list[str] = []
    for entry in entries:
        token = _runtime_trace_token(entry)
        if token and (not trace_tokens or trace_tokens[-1] != token):
            trace_tokens.append(token)
    if not trace_tokens:
        return None
    return " -> ".join(trace_tokens)


def format_runtime_summary_line(entries: list[TranscriptEntry]) -> str | None:
    """格式化运行时摘要行，以供外部显示。

    参数:
        entries: 转录条目列表

    返回:
        格式为 "runtime-summary: <摘要>" 的字符串，无可摘要内容时返回 None
    """  # runtime_summary = _runtime_trace_summary(entries)
    if not runtime_summary:
        return None
    return f"runtime-summary: {runtime_summary}"


def _indent_block(text: str, prefix: str = "  ") -> str:
    """为文本块中的每一行添加缩进前缀。

    参数:
        text:   要缩进的原始文本
        prefix: 缩进前缀，默认为两个空格

    返回:
        每行均已添加前缀后的文本
    """  # return "\n".join(prefix + line for line in text.split("\n"))


def preview_tool_body(tool_name: str, body: str) -> str:
    """根据工具名称和内容大小截断工具输出。

    参数:
        tool_name: 工具名称（如 "read_file" 有更严格的限制）
        body:      工具输出的原始文本

    返回:
        截断后的文本，若被截断则追加截断提示
    """  # max_chars = 1000 if tool_name == "read_file" else 1800
    max_lines = 20 if tool_name == "read_file" else 36

    lines = body.split("\n")
    limited_lines = lines[:max_lines] if len(lines) > max_lines else lines
    limited = "\n".join(limited_lines)

    if len(limited) > max_chars:
        limited = limited[:max_chars] + "..."

    if limited != body:
        return f"{limited}\n{DIM}... output truncated in transcript{RESET}"

    return limited


def _render_transcript_entry(entry: TranscriptEntry) -> str:
    """渲染单条转录条目，应用 Morandi 主题配色。

    参数:
        entry: 转录条目对象

    返回:
        渲染后的 ANSI 字符串，未知类型返回空串
    """  # t = theme()

    if entry.kind == "user":
        label = f"{t.user}{t.bold}▶ you{t.reset}"
        return f"{label}\n{_indent_block(entry.body)}"

    if entry.kind == "assistant":
        label = f"{t.assistant}{t.bold}▶ assistant{t.reset}"
        return f"{label}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "progress":
        label_text = (
            _runtime_label_text(entry)
            if _is_runtime_entry(entry)
            else "progress"
        )
        label = f"{t.progress}{t.bold}▶ {label_text}{t.reset}"
        meta_suffix = _runtime_meta_suffix(entry)
        return f"{label}{meta_suffix}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "tool":
        if entry.status == "running":
            status_label = f"{t.tool}{ICON_DOT} running{t.reset}"
        elif entry.status == "success":
            status_label = f"{t.assistant}ok{t.reset}"
        else:
            status_label = f"{t.tool_error}err{t.reset}"

        tool_name_display = f"{t.tool}{t.bold}{entry.toolName}{t.reset}"

        body_lines = entry.body.split("\n") if entry.body else []
        total_lines = len(body_lines)
        collapsible_by_lines = total_lines > _TOOL_PREVIEW_LINES
        collapsible_by_chars = any(
            len(ln) > _TOOL_PREVIEW_CHARS for ln in body_lines[:_TOOL_PREVIEW_LINES]
        )
        is_collapsed = entry.collapsed or entry.collapsePhase == 3
        is_collapsing = entry.collapsePhase in (1, 2)
        can_toggle = collapsible_by_lines or collapsible_by_chars or is_collapsing

        if can_toggle:
            if is_collapsing:
                toggle_text = f"  {t.expandable}{t.bold}[collapsing]{t.reset}"
            else:
                toggle_text = (
                    f"  {t.expandable}{t.bold}[收起]{t.reset}"
                    if not is_collapsed
                    else f"  {t.expandable}{t.bold}[展开]{t.reset}"
                )
        else:
            toggle_text = ""

        label = (
            f"{t.tool}{t.bold}▶ tool{t.reset} {tool_name_display}"
            f" {status_label}{toggle_text}"
        )

        if entry.status == "running":
            body = entry.body
        elif is_collapsing:
            body = _render_tool_body(entry, body_lines, total_lines, collapsible_by_lines, is_collapsed)
        elif is_collapsed:
            summary = entry.collapsedSummary or "output collapsed"
            body = f"{t.subtle}{t.italic}{summary}{t.reset}"
        else:
            body = _render_tool_body(entry, body_lines, total_lines, collapsible_by_lines, is_collapsed)

        return f"{label}\n{_indent_block(body)}"

    return ""


def _render_tool_body(entry, body_lines, total_lines, collapsible, is_collapsed):
    """渲染工具调用的输出内容，对编辑/补丁/差异工具应用差异着色。

    参数:
        entry:         转录条目对象
        body_lines:    内容按行分割后的列表
        total_lines:   内容总行数
        collapsible:   内容是否可折叠（超出预览限制）
        is_collapsed:  内容当前是否已折叠

    返回:
        渲染后的内容字符串（含 ANSI 转义码）
    """  # t = theme()
    body = entry.body

    if entry.toolName in _DIFF_TOOLS and _looks_like_diff_block(body):
        colored = colorize_unified_diff_block(body)
        if collapsible and not is_collapsed:
            preview = "\n".join(colored.split("\n")[:_TOOL_PREVIEW_LINES])
            hidden = max(0, total_lines - _TOOL_PREVIEW_LINES)
            return preview + (f"\n{t.subtle}  ... {hidden} more lines{t.reset}" if hidden > 0 else "")
        return colored

    if collapsible and not is_collapsed:
        preview = "\n".join(body_lines[:_TOOL_PREVIEW_LINES])
        hidden = max(0, total_lines - _TOOL_PREVIEW_LINES)
        return preview_tool_body(entry.toolName or "", render_markdownish(preview)) + (
            f"\n{t.subtle}  ... {hidden} more lines{t.reset}" if hidden > 0 else ""
        )
    return preview_tool_body(entry.toolName or "", render_markdownish(body))


def get_transcript_window_size(window_size: int | None = None) -> int:
    """计算转录面板的显示窗口大小。

    参数:
        window_size: 指定的窗口大小，若为 None 则根据终端行数自动计算

    返回:
        窗口行数，最小为 4（指定时）或 8（自动计算时）
    """  # if window_size is not None:
        return max(4, window_size)
    _, rows = _cached_terminal_size()
    return max(8, rows - 15)


@dataclass(slots=True)
class TranscriptLayout:
    """转录布局数据结构。

    存储一次布局计算的结果，包含修订号、总行数、
    以及每个条目的起始行和行数信息。
    """
    revision: int
    total_lines: int
    entry_line_starts: list[int]
    entry_line_counts: list[int]


_EntryCacheKey = tuple[
    str,
    str,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    bool,
    int | None,
    str | None,
    str | None,
]
_entry_cache: dict[_EntryCacheKey, list[str]] = {}
_line_count_cache: dict[_EntryCacheKey, int] = {}
_LayoutCacheKey = tuple[int, int, int, int]
_layout_cache: dict[_LayoutCacheKey, TranscriptLayout] = {}
_CACHE_MAX_SIZE = 500
_LAYOUT_CACHE_MAX_SIZE = 64


def _entry_cache_key(entry: TranscriptEntry) -> _EntryCacheKey:
    """从条目的渲染相关状态构造无冲突的缓存键。

    参数:
        entry: 转录条目对象

    返回:
        包含条目渲染状态所有相关字段的缓存键元组
    """  # return (
        entry.kind,
        entry.body,
        entry.category,
        entry.runtimeKind,
        entry.runtimeStep,
        entry.runtimePhase,
        entry.runtimeStopReason,
        entry.runtimeVerificationFocus,
        entry.status,
        entry.collapsed,
        entry.collapsePhase,
        entry.collapsedSummary,
        entry.toolName,
    )


def _get_entry_lines(entry: TranscriptEntry) -> list[str]:
    """获取条目渲染后的行列表（含缓存）。

    先渲染逻辑行，再按面板宽度换行，使行数反映屏幕上的可视行数。

    参数:
        entry: 转录条目对象

    返回:
        渲染后的文本行列表
    """  # content_key = _entry_cache_key(entry)
    width = _transcript_panel_width()
    cache_key = (content_key, width)

    cached = _entry_cache.get(cache_key)
    if cached is not None:
        return cached

    # Render logical lines, then wrap each to the panel width so the entry's
    # line count reflects on-screen visual rows (TS parity).
    logical = _render_transcript_entry(entry).split("\n")
    lines: list[str] = []
    for logical_line in logical:
        lines.extend(_wrap_panel_body_line(logical_line, width))

    if len(_entry_cache) > _CACHE_MAX_SIZE:
        keys = list(_entry_cache.keys())
        for k in keys[: len(keys) // 2]:
            del _entry_cache[k]
            _line_count_cache.pop(k, None)

    _entry_cache[cache_key] = lines
    return lines


def _get_entry_line_count(entry: TranscriptEntry) -> int:
    """获取条目渲染后的总行数（含缓存）。

    优先从行数缓存中读取，若未命中则从完整行缓存或重新渲染中获取。

    参数:
        entry: 转录条目对象

    返回:
        条目占用的可视行数
    """  # content_key = _entry_cache_key(entry)
    width = _transcript_panel_width()
    cache_key = (content_key, width)

    cached_lc = _line_count_cache.get(cache_key)
    if cached_lc is not None:
        return cached_lc

    cached_full = _entry_cache.get(cache_key)
    if cached_full is not None:
        count = len(cached_full)
        _line_count_cache[cache_key] = count
        return count

    lines = _get_entry_lines(entry)
    count = len(lines)
    _line_count_cache[cache_key] = count
    return count


def _layout_cache_key(
    entries: list[TranscriptEntry],
    revision: int | None,
) -> _LayoutCacheKey | None:
    """构造布局缓存的键。

    参数:
        entries:  转录条目列表
        revision: 修订号，若为 None 则不缓存

    返回:
        布局缓存键元组，revision 为 None 时返回 None
    """  # if revision is None:
        return None
    return (id(entries), revision, len(entries), _transcript_panel_width())


def _build_transcript_layout(
    entries: list[TranscriptEntry],
    revision: int | None,
) -> TranscriptLayout:
    """构建完整的转录布局。

    遍历每个条目，计算其行数和起始位置，并考虑条目之间的分隔符行。
    结果会被缓存以提高性能。

    参数:
        entries:  转录条目列表
        revision: 修订号，用于缓存一致性

    返回:
        包含所有条目布局信息的 TranscriptLayout 对象
    """  # cache_key = _layout_cache_key(entries, revision)
    if cache_key is not None:
        cached = _layout_cache.get(cache_key)
        if cached is not None:
            return cached

    entry_line_starts: list[int] = []
    entry_line_counts: list[int] = []
    current_line = 0

    for i, entry in enumerate(entries):
        if i > 0:
            current_line += _SEPARATOR_LINE_COUNT
        entry_line_starts.append(current_line)
        line_count = _get_entry_line_count(entry)
        entry_line_counts.append(line_count)
        current_line += line_count

    layout = TranscriptLayout(
        revision=revision or 0,
        total_lines=current_line,
        entry_line_starts=entry_line_starts,
        entry_line_counts=entry_line_counts,
    )

    if cache_key is not None:
        if len(_layout_cache) >= _LAYOUT_CACHE_MAX_SIZE:
            for key in list(_layout_cache.keys())[: len(_layout_cache) // 2]:
                del _layout_cache[key]
        _layout_cache[cache_key] = layout
    return layout


def _compute_total_lines(entries: list[TranscriptEntry], revision: int | None = None) -> int:
    """计算转录条目的总可视行数。

    参数:
        entries:  转录条目列表
        revision: 修订号，可选

    返回:
        总行数，条目为空时返回 0
    """  # if not entries:
        return 0
    return _build_transcript_layout(entries, revision).total_lines


def _render_visible_window(
    entries: list[TranscriptEntry],
    start_line: int,
    end_line: int,
    revision: int | None = None,
) -> list[str]:
    """渲染指定可见行范围内的条目内容，包含分隔符。

    使用二分查找定位起始条目，按需截取每个条目的可视部分。

    参数:
        entries:   转录条目列表
        start_line: 可视范围的起始行号（含）
        end_line:   可视范围的结束行号（不含）
        revision:   修订号，可选

    返回:
        可见范围内的渲染文本行列表
    """  # if not entries:
        return []

    layout = _build_transcript_layout(entries, revision)
    result: list[str] = []
    if not layout.entry_line_starts:
        return result

    start_index = bisect_left(layout.entry_line_starts, start_line)
    if start_index > 0:
        start_index -= 1

    for i in range(start_index, len(entries)):
        entry_start = layout.entry_line_starts[i]
        entry_line_count = layout.entry_line_counts[i]
        entry_end = entry_start + entry_line_count

        if i > 0:
            sep_start = entry_start - _SEPARATOR_LINE_COUNT
            sep_end = entry_start
            if sep_start < end_line and sep_end > start_line:
                vis_start = max(0, start_line - sep_start)
                vis_end = min(_SEPARATOR_LINE_COUNT, end_line - sep_start)
                result.extend(_SEPARATOR_LINES[vis_start:vis_end])

        if entry_start >= end_line:
            break

        if entry_start < end_line and entry_end > start_line:
            lines = _get_entry_lines(entries[i])
            vis_start = max(0, start_line - entry_start)
            vis_end = min(entry_line_count, end_line - entry_start)
            result.extend(lines[vis_start:vis_end])

    return result


def get_transcript_max_scroll_offset(
    entries: list[TranscriptEntry],
    window_size: int | None = None,
    revision: int | None = None,
) -> int:
    """获取转录面板的最大滚动偏移量。

    参数:
        entries:    转录条目列表
        window_size: 窗口大小，可选
        revision:    修订号，可选

    返回:
        最大滚动偏移行数，条目为空时返回 0
    """  # if not entries:
        return 0
    total = _compute_total_lines(entries, revision)
    ws = get_transcript_window_size(window_size)
    return max(0, total - ws)


def render_transcript(
    entries: list[TranscriptEntry],
    scroll_offset: int,
    window_size: int | None = None,
    revision: int | None = None,
) -> str:
    """渲染转录面板的窗口视图。复杂度与可见行数相关。

    支持滚动偏移，在非零偏移时追加滚动位置指示器。

    参数:
        entries:      转录条目列表
        scroll_offset: 当前滚动偏移行数
        window_size:   窗口大小，可选
        revision:      修订号，可选

    返回:
        完整的转录面板渲染字符串（含 ANSI 转义码和滚动条）
    """  # t = theme()
    if not entries:
        return ""

    layout = _build_transcript_layout(entries, revision)
    total_lines = layout.total_lines
    ws = get_transcript_window_size(window_size)
    max_offset = max(0, total_lines - ws)
    offset = max(0, min(scroll_offset, max_offset))

    if offset == 0:
        end = total_lines
        start = max(0, end - ws)
        visible_lines = _render_visible_window(entries, start, end, revision)
        body = "\n".join(visible_lines)
        scrollbar = _render_scrollbar(offset, max_offset, len(visible_lines))
        return _interleave_scrollbar(body, scrollbar)

    content_ws = max(1, ws - 1)
    end = total_lines - offset
    start = max(0, end - content_ws)
    visible_lines = _render_visible_window(entries, start, end, revision)
    body = "\n".join(visible_lines)
    scrollbar = _render_scrollbar(offset, max_offset, len(visible_lines))

    indicator = (
        f"{body}\n"
        f"{t.subtle}  {ICON_DIVIDER * 2} scroll {offset}/{max_offset} "
        f"(PgUp/PgDn or scroll){ICON_DIVIDER * 2}{t.reset}"
    )
    return _interleave_scrollbar(indicator, scrollbar)


# 8-segment Unicode blocks for sub-character scrollbar precision
# ' ' (0/8) → '▏' (1/8) → '▎' (2/8) → '▍' (3/8) → '▌' (4/8) → '▋' (5/8) → '▊' (6/8) → '▉' (7/8) → '█' (8/8)
_SCROLLBAR_BLOCKS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']


def _render_scrollbar(offset: int, max_offset: int, height: int) -> list[str]:
    """渲染垂直滚动条，使用 8 段 Unicode 区块实现亚字符级精度。

    参数:
        offset:     当前滚动偏移
        max_offset: 最大滚动偏移
        height:     滚动条的高度（行数）

    返回:
        每行对应的滚动条字符列表
    """  # if max_offset <= 0 or height < 3:
        return [" "] * max(1, height)
    # Thumb position with sub-character precision
    ratio = offset / max_offset
    precise_pos = ratio * (height - 1)
    whole = int(precise_pos)
    remainder = precise_pos - whole
    part_idx = int(remainder * 8)  # 0-8, map to _SCROLLBAR_BLOCKS

    bar = []
    for i in range(height):
        if i < whole:
            bar.append("░")  # above thumb
        elif i == whole:
            bar.append(_SCROLLBAR_BLOCKS[part_idx])  # partial block for smooth position
        elif i == whole + 1 and part_idx > 0:
            bar.append(_SCROLLBAR_BLOCKS[8 - part_idx])  # complementary block below thumb
        elif i == 0 and offset > 0:
            bar.append("▲")
        elif i == height - 1 and offset < max_offset:
            bar.append("▼")
        else:
            bar.append("░")
    return bar


def _interleave_scrollbar(body: str, scrollbar: list[str]) -> str:
    """将滚动条字符追加到正文的每一行末尾。

    参数:
        body:     正文文本（可能包含多行）
        scrollbar: 每行对应的滚动条字符列表

    返回:
        每行末尾追加了滚动条字符的完整文本
    """  # lines = body.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i < len(scrollbar):
            result.append(f"{line}{scrollbar[i]}")
        else:
            result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Legacy full-render API (backward compat)
# ---------------------------------------------------------------------------

def _render_transcript_lines(entries: list[TranscriptEntry]) -> list[str]:
    """将所有条目渲染为带分隔符的行列表（保留用于向后兼容）。

    参数:
        entries: 转录条目列表

    返回:
        所有条目渲染后的行列表，条目间包含分隔符
    """  # all_lines: list[str] = []
    for i, entry in enumerate(entries):
        if i > 0:
            all_lines.extend(_SEPARATOR_LINES)
        all_lines.extend(_get_entry_lines(entry))
    return all_lines


def format_transcript_text(entries: list[TranscriptEntry]) -> str:
    """将转录条目格式化为纯文本（不含 ANSI 转义码），用于保存文件。

    参数:
        entries: 转录条目列表

    返回:
        纯文本格式的转录内容，条目间以 ``---`` 分隔
    """  # parts = []
    runtime_summary_line = format_runtime_summary_line(entries)
    if runtime_summary_line:
        parts.append(f"runtime-summary\n  {runtime_summary_line.removeprefix('runtime-summary: ')}")
    for entry in entries:
        if entry.kind == "user":
            label = "you"
        elif entry.kind == "progress" and _is_runtime_entry(entry):
            label = _runtime_label_text(entry) + _runtime_meta_suffix(entry)
        else:
            label = entry.kind
        if entry.kind == "tool":
            status_text = f" ({entry.status})" if entry.status else ""
            label = f"{entry.toolName or 'tool'}{status_text}"
        indented = "\n".join("  " + line for line in entry.body.splitlines())
        parts.append(f"{label}\n{indented}")
    return "\n\n---\n\n".join(parts)
