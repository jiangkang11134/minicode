"""终端 UI 样式组件：ANSI 颜色常量、Unicode 装饰字符、面板渲染与权限提示。

提供终端 UI 所需的底层样式原语，包括：
- ANSI 转义序列常量（颜色、样式、256 色扩展）
- Unicode 装饰图标常量
- 终端尺寸缓存查询
- CJK/Emoji 宽度计算与文本截断/填充
- 面板、横幅、状态栏、工具面板、页脚栏渲染
- / 命令菜单渲染
- Diff 着色与权限审批提示渲染
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from typing import Any

from .theme import theme

# ---------------------------------------------------------------------------
# Re-export legacy ANSI constants (kept for backward compatibility)
# ---------------------------------------------------------------------------
RESET = "\x1b[0m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
BOLD = "\x1b[1m"
REVERSE = "\x1b[7m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
BRIGHT_GREEN = "\x1b[92m"
BRIGHT_RED = "\x1b[91m"
BRIGHT_CYAN = "\x1b[96m"
BRIGHT_YELLOW = "\x1b[93m"
BRIGHT_BLUE = "\x1b[94m"
BRIGHT_MAGENTA = "\x1b[95m"
BRIGHT_WHITE = "\x1b[97m"
# Extended 256-color palette
BORDER = "\x1b[38;5;39m"
BORDER_DIM = "\x1b[38;5;24m"
ACCENT = "\x1b[38;5;214m"
ACCENT2 = "\x1b[38;5;141m"
SUBTLE = "\x1b[38;5;243m"
HIGHLIGHT_BG = "\x1b[48;5;236m"

# ---------------------------------------------------------------------------
# Unicode decorative characters
# ---------------------------------------------------------------------------
ICON_MINICODE = "\u2726"   # ✦
ICON_USER = "\u25B6"       # ▶
ICON_ASSISTANT = "\u2734"  # ✴
ICON_TOOL = "\u2699"       # ⚙
ICON_PROGRESS = "\u25CF"   # ●
ICON_SUCCESS = "\u2714"    # ✔
ICON_ERROR = "\u2718"      # ✘
ICON_RUNNING = "\u25CB"    # ○
ICON_FOLDER = "\u25A0"     # ■
ICON_MODEL = "\u25C6"      # ◆
ICON_PROVIDER = "\u25C8"   # ◈
ICON_PROMPT = "\u276F"     # ❯
ICON_SKILL = "\u2605"      # ★
ICON_MSG = "\u25AC"        # ▬
ICON_EVENT = "\u25AA"      # ▪
ICON_MCP = "\u25C9"        # ◉
ICON_BG = "\u25D0"         # ◐
ICON_LOCK = "\u25A3"       # ▣
ICON_DIVIDER = "\u2500"    # ─
ICON_DOT = "\u00B7"        # ·
ICON_ARROW = "\u25B8"      # ▸

# Pre-compiled regex for ANSI stripping
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """去除字符串中的所有 ANSI 转义码。

    参数:
        text: 可能包含 ANSI 转义序列的原始文本

    返回:
        纯文本字符串，不含任何 ANSI 控制码
    """  # return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Cached terminal size
# ---------------------------------------------------------------------------
_ts_cache: tuple[int, int] | None = None
_ts_cache_time: float = 0.0
_TS_TTL: float = 0.5


def _cached_terminal_size() -> tuple[int, int]:
    """返回缓存的终端尺寸 ``(columns, rows)``。

    每 ``_TS_TTL`` (0.5 秒) 内复用缓存值，避免高频 I/O 调用。
    当 ``os.get_terminal_size`` 失败时返回回退值 (100, 40)。

    返回:
        (列数, 行数) 的二元组
    """  # global _ts_cache, _ts_cache_time
    now = time.monotonic()
    if _ts_cache is None or (now - _ts_cache_time) > _TS_TTL:
        try:
            ts = os.get_terminal_size()
            cols, rows = ts.columns, ts.lines
            if cols <= 0 or rows <= 0:
                _ts_cache = (100, 40)
            else:
                _ts_cache = (cols, rows)
        except (AttributeError, ValueError, OSError):
            _ts_cache = (100, 40)
        _ts_cache_time = now
    return _ts_cache


def invalidate_terminal_size_cache() -> None:
    """使终端尺寸缓存失效，下一次调用 ``_cached_terminal_size`` 时会重新查询 OS。"""  # global _ts_cache
    _ts_cache = None


# ---------------------------------------------------------------------------
# Width computation
# ---------------------------------------------------------------------------

_WIDE_CHAR_PATTERN = re.compile(
    r'[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF'
    r'\uF900-\uFAFF\uFE10-\uFE19\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6'
    r'\U0001F300-\U0001FAF6\U00020000-\U0003FFFD]'
)


def char_display_width(char: str) -> int:
    """检测 CJK/Emoji 字符宽度。

    根据 Unicode 范围判断字符是否为宽字符（中日韩、表情符号等），
    宽字符返回 2，普通字符返回 1，空字符返回 0。

    参数:
        char: 单个字符

    返回:
        显示宽度 (0、1 或 2)
    """  # if not char:
        return 0
    code = ord(char)
    if (
        0x1100 <= code <= 0x115F
        or code == 0x2329
        or code == 0x232A
        or (0x2E80 <= code <= 0xA4CF and code != 0x303F)
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE19
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAF6
        or 0x20000 <= code <= 0x3FFFD
    ):
        return 2
    return 1


@lru_cache(maxsize=2048)
def _stripped_display_width(stripped: str) -> int:
    """计算已去除 ANSI 码的字符串的显示宽度。结果会缓存以加速热点路径。

    宽字符（CJK/Emoji）每个算 2 个显示宽度单位。

    参数:
        stripped: 已去除 ANSI 转义码的纯文本

    返回:
        显示宽度总和
    """  # wide_chars = len(_WIDE_CHAR_PATTERN.findall(stripped))
    return len(stripped) + wide_chars


def string_display_width(text: str) -> int:
    """计算字符串的显示宽度，自动去除 ANSI 码后统计。

    先去除 ANSI 转义序列，再计算剩余文本中宽字符的宽度，
    普通字符每个 1 单位，CJK/Emoji 每个 2 单位。

    参数:
        text: 可能包含 ANSI 码的文本

    返回:
        显示宽度总和
    """  # stripped = _ANSI_RE.sub("", text)
    return _stripped_display_width(stripped)


def truncate_plain(text: str, width: int) -> str:
    """CJK 感知的文本截断，超出宽度时追加 ``...`` 后缀。

    保留文本中的 ANSI 转义码，确保截断后颜色样式不丢失。
    先计算显示宽度判断是否需要截断，仅在必要时才进行逐字符截断。

    参数:
        text: 原始文本（可含 ANSI 码）
        width: 目标显示宽度

    返回:
        截断或原样返回的文本
    """  # if string_display_width(text) <= width:
        return text

    limit = max(0, width - 3)
    res = ""
    w = 0
    i = 0
    while i < len(text):
        match = _ANSI_RE.match(text, i)
        if match:
            res += match.group()
            i = match.end()
            continue

        char = text[i]
        cw = char_display_width(char)
        if w + cw > limit:
            res += "..."
            i += 1
            while i < len(text):
                m = _ANSI_RE.match(text, i)
                if m:
                    res += m.group()
                    i = m.end()
                else:
                    i += 1
            return res

        res += char
        w += cw
        i += 1
    return res


def pad_plain(text: str, width: int) -> str:
    """CJK 感知的右填充，不足宽度时在尾部补空格。

    参数:
        text: 待填充文本
        width: 目标显示宽度

    返回:
        右填充空格后的文本
    """  # display_w = string_display_width(text)
    return text + (" " * max(0, width - display_w))


def truncate_path_middle(path: str, width: int) -> str:
    """中间截断路径，保留头尾，用 ``...`` 连接。

    适用于长路径显示，在有限宽度内展示路径的开头和结尾部分。
    当宽度 <= 5 时退化到 ``truncate_plain`` 行为。

    参数:
        path: 文件路径字符串
        width: 目标显示宽度

    返回:
        中间截断后的路径文本
    """  # if string_display_width(path) <= width:
        return path
    if width <= 5:
        return truncate_plain(path, width)

    half = (width - 3) // 2
    start_chars = ""
    start_w = 0
    for c in path:
        cw = char_display_width(c)
        if start_w + cw > half:
            break
        start_chars += c
        start_w += cw

    end_chars = ""
    end_w = 0
    for c in reversed(path):
        cw = char_display_width(c)
        if end_w + cw > (width - 3 - start_w):
            break
        end_chars = c + end_chars
        end_w += cw

    return start_chars + "..." + end_chars


def color_badge(label: str, value: str, color: str, icon: str = "") -> str:
    """渲染样式化的标记徽章：``icon`` [``label``] ``value``。

    参数:
        label: 标签文本（显示在方括号中）
        value: 值文本（加粗显示）
        color: 颜色 ANSI 码
        icon: 可选的前置图标

    返回:
        格式化后的徽章字符串
    """  # t = theme()
    icon_part = f"{color}{icon} " if icon else ""
    return f"{icon_part}{color}{t.dim}[{label}]{t.reset} {t.bold}{value}{t.reset}"


def border_line(kind: str, width: int, color: str = "") -> str:
    """用 Unicode 制表符绘制边框线：顶部 ``╭─╮`` 或底部 ``╰─╯``，中间 ``├─┤``。

    参数:
        kind: 类型，'top' 为顶部边框，'bottom' 为底部边框，其余为中间分隔线
        width: 终端列宽
        color: 边框颜色 ANSI 码，为空时使用默认 ``BORDER``

    返回:
        带 ANSI 颜色的单行边框字符串
    """  # c = color or BORDER
    if kind == "top":
        return f"{c}╭{'─' * (width - 2)}╮{RESET}"
    elif kind == "bottom":
        return f"{c}╰{'─' * (width - 2)}╯{RESET}"
    else:
        return f"{c}├{'─' * (width - 2)}┤{RESET}"


def panel_row(left: str, width: int, right: str | None = None, border_color: str = "") -> str:
    """渲染面板行内容，格式为 ``│ left ... right │``。

    左右两侧文本自动对齐，右侧文本可选。当左右内容超出内部宽度时，
    左侧会被截断以保留右侧内容。

    参数:
        left: 左侧文本
        width: 终端总宽度
        right: 可选的右侧对齐文本
        border_color: 边框颜色 ANSI 码

    返回:
        带边框的一行字符串
    """  # bc = border_color or BORDER
    inner_width = width - 4
    if right:
        l_w = string_display_width(left)
        r_w = string_display_width(right)
        gap = inner_width - l_w - r_w
        if gap < 1:
            left = truncate_plain(left, inner_width - r_w - 1)
            gap = 1
        return f"{bc}│{RESET} {left}{' ' * gap}{right} {bc}│{RESET}"
    else:
        return f"{bc}│{RESET} {pad_plain(left, inner_width)} {bc}│{RESET}"


def empty_panel_row(width: int) -> str:
    """渲染空的面板行（仅含边框和空白）。"""  # return panel_row("", width)


def wrap_panel_body_line(line: str, width: int) -> list[str]:
    """CJK 感知的长行换行，用于面板正文。

    根据面板内部宽度 (总宽度 - 4) 对长行进行折行处理，
    保留文本中的 ANSI 转义码，空格处可自然换行。

    参数:
        line: 单行文本（可含 ANSI 码）
        width: 终端总宽度

    返回:
        折行后的字符串列表
    """  # inner_width = width - 4
    if string_display_width(line) <= inner_width:
        return [line]

    ansi_spans: list[tuple[int, int]] = []
    for m in _ANSI_RE.finditer(line):
        ansi_spans.append((m.start(), m.end()))

    lines: list[str] = []
    current_line = ""
    current_w = 0
    i = 0
    span_idx = 0

    while i < len(line):
        if span_idx < len(ansi_spans) and i == ansi_spans[span_idx][0]:
            end = ansi_spans[span_idx][1]
            current_line += line[i:end]
            i = end
            span_idx += 1
            continue

        char = line[i]
        cw = char_display_width(char)
        if current_w + cw > inner_width:
            lines.append(current_line)
            current_line = ""
            current_w = 0
            if char == " ":
                i += 1
                continue
        current_line += char
        current_w += cw
        i += 1
    if current_line:
        lines.append(current_line)
    return lines


_PANEL_ICONS: dict[str, str] = {
    "minicode": ICON_MINICODE,
    "session feed": ICON_MSG,
    "prompt": ICON_PROMPT,
    "activity": ICON_TOOL,
    "action required": ICON_LOCK,
}


def render_panel(
    title: str,
    body: str,
    right_title: str | None = None,
    min_body_lines: int = 0,
    border_color: str = "",
) -> str:
    """渲染带 Unicode 边框的完整面板。

    根据面板标题自动选择主题边框颜色（workspace/header、session、prompt/input、action/approval 等）。
    面板包含：顶部边框、标题行、分隔线、正文行（自动折行）、底部边框。
    正文行数不足 ``min_body_lines`` 时会自动填充空白行。

    参数:
        title: 面板标题
        body: 面板正文（多行用 ``\\n`` 分隔）
        right_title: 可选的右侧标题文本
        min_body_lines: 最小正文行数
        border_color: 边框颜色 ANSI 码，为空时根据标题自动匹配

    返回:
        完整面板字符串（含换行）
    """  # t = theme()
    width, _ = _cached_terminal_size()
    if width < 40:
        width = 40

    # Pick border color from theme based on title
    if not border_color:
        title_lower = title.lower()
        if "workspace" in title_lower or "minicode" in title_lower:
            border_color = t.header
        elif "session" in title_lower:
            border_color = t.session
        elif "prompt" in title_lower or "input" in title_lower:
            border_color = t.input
        elif "action" in title_lower or "approval" in title_lower:
            border_color = t.approval
        else:
            border_color = BORDER

    icon = _PANEL_ICONS.get(title.lower(), "")
    icon_str = f"{ACCENT}{icon} {RESET}" if icon else ""

    res = [border_line("top", width, border_color)]
    title_display = f"{icon_str}{t.bold}{title}{t.reset}"
    right_display = f"{t.subtle}{right_title}{t.reset}" if right_title else None
    res.append(panel_row(title_display, width, right_display, border_color))

    inner = width - 4
    divider_line = f"{BORDER_DIM}{'╌' * inner}{RESET}"
    res.append(panel_row(divider_line, width, border_color=border_color))

    body_lines = body.splitlines() if body else []
    wrapped_lines: list[str] = []
    for bl in body_lines:
        wrapped_lines.extend(wrap_panel_body_line(bl, width))

    while len(wrapped_lines) < min_body_lines:
        wrapped_lines.append("")

    for wl in wrapped_lines:
        res.append(panel_row(wl, width, border_color=border_color))
    res.append(border_line("bottom", width, border_color))
    return "\n".join(res)


# ---------------------------------------------------------------------------
# Banner / header — aligned with Rust's build_header_lines
# ---------------------------------------------------------------------------

def render_banner(
    runtime: dict | None,
    cwd: str,
    permission_summary: list[str],
    session: dict[str, int],
    compact: bool = False,
) -> str:
    """渲染工作区标题面板（Banner）。

    布局与 Rust 版本的 ``build_header_lines`` 对齐：
      - 普通模式（3 行）：第 1 行 project/provider/model/auth，第 2 行 session 统计，第 3 行权限摘要
      - 紧凑模式（1 行）：所有信息压缩到单行，适用于小终端

    参数:
        runtime: 运行时配置字典，含 model、baseUrl、authToken、apiKey 等字段
        cwd: 当前工作目录路径
        permission_summary: 权限摘要字符串列表
        session: 会话统计字典，含 messageCount、transcriptCount、skillCount、mcpCount
        compact: 是否使用紧凑模式（单行显示）

    返回:
        渲染后的工作区面板字符串
    """  # t = theme()

    model = runtime.get("model", "(unconfigured)") if runtime else "(unconfigured)"

    # Provider hostname (strip scheme)
    provider = "offline"
    if runtime and runtime.get("baseUrl"):
        provider = (
            runtime["baseUrl"]
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
        )

    # Auth kind
    auth = "none"
    if runtime:
        if runtime.get("authToken"):
            auth = "auth_token"
        elif runtime.get("apiKey"):
            auth = "api_key"

    msg_count = session.get("messageCount", 0)
    evt_count = session.get("transcriptCount", 0)
    skill_count = session.get("skillCount", 0)
    mcp_count = session.get("mcpCount", 0)

    if compact:
        # Single-line compact header for small terminals
        import os as _os
        cwd_short = _os.path.basename(cwd) or cwd
        body = (
            f"{t.header_label_info}{t.bold}project{t.reset} {cwd_short}"
            f"  {t.header_label_info}{t.bold}model{t.reset} {model}"
            f"  {t.header_label_session}{t.bold}msgs{t.reset} {msg_count}"
        )
        return render_panel("Workspace", body)

    # Line 1 — project / provider / model / auth
    line1 = (
        f"{t.header_label_info}{t.bold}project{t.reset} {cwd}"
        f"   {t.header_label_info}{t.bold}provider{t.reset} {provider}"
        f"   {t.header_label_info}{t.bold}model{t.reset} {model}"
        f"   {t.header_label_info}{t.bold}auth{t.reset} {auth}"
    )

    # Line 2 — session stats
    line2 = (
        f"{t.header_label_session}{t.bold}session{t.reset}"
        f" messages={msg_count}"
        f" events={evt_count}"
        f" skills={skill_count}"
        f" mcp={mcp_count}"
    )

    body = "\n".join([line1, line2])
    return render_panel("Workspace", body)


def render_status_line(status: str | None) -> str:
    """渲染状态行，思考/等待状态下显示闪烁动画（shimmer）。

    当状态文本包含 "Thinking" 时，启动 3Hz 的高亮扫描动画，
    模拟光标在文本上移动的效果。其它状态显示静态图标 + 文本。

    参数:
        status: 状态文本，为 ``None`` 时显示 "Ready"

    返回:
        带 ANSI 颜色的单行状态字符串
    """  # t = theme()
    if status:
        import time as _time
        # Shimmer: animate a traveling highlight across the status text when thinking
        is_thinking = "Thinking" in status
        if is_thinking:
            tick = int(_time.monotonic() * 3)  # 3 Hz shimmer sweep
            plain = f"  {status}"
            width = len(plain)
            if width > 3:
                pos = tick % (width + 6) - 3  # Sweep from -3 to width
                shimmered = ""
                for i, ch in enumerate(plain):
                    dist = abs(i - pos)
                    if dist == 0:
                        shimmered += f"{t.bold}{t.assistant}{ch}{t.reset}"
                    elif dist == 1:
                        shimmered += f"{t.assistant}{ch}{t.reset}"
                    else:
                        shimmered += f"{t.tool}{ch}{t.reset}"
                return f"{t.bold}{ICON_RUNNING}{t.reset}{shimmered}"
        return f"{t.tool}{t.bold}{ICON_RUNNING} {status}{t.reset}"
    return f"{t.assistant}{ICON_SUCCESS} Ready{t.reset}"


def render_tool_panel(
    active_tool: str | None,
    recent_tools: list[dict[str, str]],
    background_tasks: list[dict[str, Any]] | None = None,
) -> str:
    """渲染当前工具活动摘要面板。

    展示正在运行的工具、后台任务和最近工具的执行状态（成功/失败）。
    无活动时显示 "idle"。

    参数:
        active_tool: 当前正在运行的工具名称
        recent_tools: 最近执行的工具列表，每项含 name、status 等字段
        background_tasks: 后台任务列表，每项含 status、label 等字段

    返回:
        工具活动摘要字符串
    """  # t = theme()
    if background_tasks is None:
        background_tasks = []
    parts: list[str] = []
    if active_tool:
        parts.append(f"{ICON_RUNNING} {t.tool}{t.bold}running{t.reset} {active_tool}")
    for task in background_tasks:
        if task.get("status") == "running":
            parts.append(f"{ICON_BG} {t.progress}bg{t.reset} {task.get('label', 'task')}")
    if not parts and not recent_tools:
        parts.append(f"{t.subtle}{ICON_DOT} idle{t.reset}")
    else:
        for tool in recent_tools[-3:]:
            if tool.get("status") == "success":
                parts.append(f"{t.assistant}{ICON_SUCCESS} {tool.get('name', 'tool')}{t.reset}")
            else:
                parts.append(f"{t.tool_error}{ICON_ERROR} {tool.get('name', 'tool')}{t.reset}")
    return f"{ICON_TOOL} {t.dim}tools{t.reset}  " + f"  {t.subtle}{ICON_DOT}{t.reset}  ".join(parts)


def render_footer_bar(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]] | None = None,
) -> str:
    """渲染单行页脚栏。

    左侧显示状态行（含闪烁动画），右侧显示工具和技能状态指示器（绿色勾/红色叉），
    以及后台任务数量。左右两侧自动对齐。

    参数:
        status: 状态文本
        tools_enabled: 工具是否启用
        skills_enabled: 技能是否启用
        background_tasks: 后台任务列表

    返回:
        带对齐的单行页脚字符串
    """  # t = theme()
    if background_tasks is None:
        background_tasks = []
    width, _ = _cached_terminal_size()
    left = render_status_line(status)

    bg_info = ""
    if background_tasks:
        bg_info = f" {ICON_BG} {t.progress}{len(background_tasks)} bg{t.reset} {t.subtle}│{t.reset}"

    tools_indicator = f"{t.assistant}{ICON_SUCCESS}{t.reset}" if tools_enabled else f"{t.tool_error}{ICON_ERROR}{t.reset}"
    skills_indicator = f"{t.assistant}{ICON_SUCCESS}{t.reset}" if skills_enabled else f"{t.tool_error}{ICON_ERROR}{t.reset}"

    right = (
        f"{bg_info} {ICON_TOOL} {t.subtle}tools{t.reset} {tools_indicator}"
        f" {t.subtle}│{t.reset} {ICON_SKILL} {t.subtle}skills{t.reset} {skills_indicator}"
    )
    gap = max(1, width - string_display_width(left) - string_display_width(right))
    return f"{left}{' ' * gap}{right}"


def render_slash_menu(commands: list[Any], selected_index: int) -> str:
    """渲染斜杠命令菜单，高亮当前选中的命令。

    显示所有可用命令及其用法和描述，当前选中的命令使用高亮背景色。

    参数:
        commands: 命令对象列表，每项应有 usage 和 description 属性
        selected_index: 当前选中的命令索引

    返回:
        多行菜单字符串
    """  # t = theme()
    if not commands:
        return f"{t.subtle}no commands{t.reset}"
    width, _ = _cached_terminal_size()
    rows = [f"{ACCENT}{ICON_ARROW}{RESET} {t.dim}commands{t.reset}"]
    for i, cmd in enumerate(commands):
        usage = pad_plain(getattr(cmd, "usage", str(cmd)), 14)
        desc = getattr(cmd, "description", "")
        if i == selected_index:
            line = (
                f"  {t.command_highlight_bg}{BRIGHT_CYAN}{ICON_ARROW}{RESET}"
                f"{t.command_highlight_bg} {BRIGHT_WHITE}{t.bold}{usage}{RESET}"
                f"{t.command_highlight_bg} {desc} {RESET}"
            )
        else:
            line = f"   {t.subtle}{ICON_DOT}{t.reset} {usage} {t.subtle}{desc}{t.reset}"
        rows.append(truncate_plain(line, width))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Diff colorization
# ---------------------------------------------------------------------------

def classify_diff_line(line: str) -> str:
    """分类 diff 行的类型。

    参数:
        line: diff 文本行

    返回:
        行类型：'meta'（元数据行，---/+++/@@）、'add'（新增行）、'remove'（删除行）、'context'（上下文行）
    """  # if line.startswith(("+++", "---", "@@")):
        return "meta"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "remove"
    return "context"


def compute_changed_range(removed: str, added: str) -> tuple[int, int] | None:
    """计算删除行与新增行之间的差异区间。

    通过前缀后缀公共子串确定实际变化范围，用于词级高亮。
    当两行完全相同时返回 ``None``。

    参数:
        removed: 被删除的行内容（不含前导 ``-``）
        added: 新增的行内容（不含前导 ``+``）

    返回:
        ``(start, end)`` 区间（在 ``added`` 中的位置），或 ``None``
    """  # if not removed or not added:
        return None
    p = 0
    while p < len(removed) and p < len(added) and removed[p] == added[p]:
        p += 1
    s = 0
    while s < (len(removed) - p) and s < (len(added) - p) and removed[-(s + 1)] == added[-(s + 1)]:
        s += 1
    return (p, len(added) - s) if p < (len(added) - s) else None


def apply_word_emphasis(content: str, color: str, emphasis_range: tuple[int, int] | None = None) -> str:
    """对文本中的指定区间应用词级高亮（加粗 + 反色）。

    用于 diff 显示中突出显示变化的词。当 ``emphasis_range`` 为 ``None`` 时
    仅应用基础颜色。

    参数:
        content: 原始文本内容
        color: 基础 ANSI 颜色码
        emphasis_range: 需要高亮的区间 ``(start, end)``，为 ``None`` 时不进行词级高亮

    返回:
        带 ANSI 着色和高亮的字符串
    """  # if not emphasis_range:
        return f"{color}{content}{RESET}"
    s, e = emphasis_range
    return f"{color}{content[:s]}{BOLD}{REVERSE}{content[s:e]}{RESET}{color}{content[e:]}{RESET}"


def colorize_unified_diff_block(block: str) -> str:
    """对 unified diff 文本块进行完整着色，并添加词级高亮。

    对 diff 的各个部分分别着色：
      - 元数据行（---、+++、@@）使用青色
      - 新增行（+）使用绿色，并高亮词级变化
      - 删除行（-）使用红色，并高亮词级变化
      - 上下文行使用暗色

    参数:
        block: unified diff 格式的文本块

    返回:
        着色后的 diff 文本字符串
    """  # lines = block.splitlines()
    res: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("--- ", "+++ ", "@@ ")):
            res.append(f"{CYAN}{line}{RESET}")
            i += 1
            continue
        if line.startswith("-"):
            removals: list[str] = []
            while i < len(lines) and lines[i].startswith("-"):
                removals.append(lines[i][1:])
                i += 1
            additions: list[str] = []
            while i < len(lines) and lines[i].startswith("+"):
                additions.append(lines[i][1:])
                i += 1
            paired = min(len(removals), len(additions))
            for j in range(paired):
                emphasis = compute_changed_range(removals[j], additions[j])
                res.append("-" + apply_word_emphasis(removals[j], RED, emphasis))
                res.append("+" + apply_word_emphasis(additions[j], GREEN, emphasis))
            for j in range(paired, len(removals)):
                res.append(f"{RED}-{removals[j]}{RESET}")
            for j in range(paired, len(additions)):
                res.append(f"{GREEN}+{additions[j]}{RESET}")
            continue
        if line.startswith("+"):
            res.append(f"{GREEN}{line}{RESET}")
            i += 1
        else:
            res.append(f"{DIM}{line}{RESET}")
            i += 1
    return "\n".join(res)


def _looks_like_diff_block(detail: str) -> bool:
    """判断字符串是否看起来像 unified diff 文本块。

    检查是否包含多行文本和 diff 的特征标记（``--- a/``、``+++ b/``、``@@ ``）。

    参数:
        detail: 待检测的字符串

    返回:
        是否为 diff 文本块
    """  # return "\n" in detail and (
        "--- a/" in detail or "+++ b/" in detail or "@@ " in detail
    )


def colorize_edit_permission_details(details: list[str]) -> list[str]:
    """对权限详情列表中的 diff 文本块进行着色处理。

    遍历详情列表，对每个看起来像 diff 块的条目调用 ``colorize_unified_diff_block``，
    其它条目保持原样。

    参数:
        details: 权限详情字符串列表

    返回:
        着色处理后的字符串列表
    """  # return [
        colorize_unified_diff_block(d) if _looks_like_diff_block(d) else d
        for d in details
    ]


# ---------------------------------------------------------------------------
# Permission prompt
# ---------------------------------------------------------------------------

def get_permission_prompt_max_scroll_offset(
    request: dict[str, Any], expanded: bool = False
) -> int:
    """计算权限提示中详情区域的最大可滚动偏移量。

    仅在展开模式下，根据详情行数和终端行数计算可滚动范围。

    参数:
        request: 权限请求字典（含 details 字段）
        expanded: 是否已展开详情

    返回:
        最大滚动偏移量（行数）
    """  # if not expanded:
        return 0
    flat = flatten_detail_lines(request.get("details", []))
    _, rows = _cached_terminal_size()
    max_visible = max(4, rows - 20)
    return max(0, len(flat) - max_visible)


def flatten_detail_lines(details: list[str]) -> list[str]:
    """将权限详情列表展开为纯文本行列表（每项按换行符拆分）。

    参数:
        details: 多行详情字符串列表

    返回:
        展开后的单行文本列表
    """  # result: list[str] = []
    for detail in details:
        result.extend(detail.split("\n"))
    return result


def slice_visible_details(
    flat_lines: list[str], scroll_offset: int, max_visible: int | None = None
) -> tuple[list[str], int]:
    """从展开的详情行列表中截取当前可见区域。

    根据滚动偏移和最大可见行数返回可见部分以及总行数。

    参数:
        flat_lines: 展开后的单行文本列表
        scroll_offset: 当前滚动偏移行数
        max_visible: 最大可见行数，为 ``None`` 时根据终端尺寸自动计算

    返回:
        ``(visible_lines, total_lines)`` 二元组
    """  # if max_visible is None:
        _, rows = _cached_terminal_size()
        max_visible = max(4, rows - 20)
    total = len(flat_lines)
    offset = max(0, min(scroll_offset, max(0, total - max_visible)))
    return flat_lines[offset:offset + max_visible], total


def render_permission_prompt(
    request: dict[str, Any],
    expanded: bool = False,
    scroll_offset: int = 0,
    selected_choice_index: int = 0,
    feedback_mode: bool = False,
    feedback_input: str = "",
) -> str:
    """渲染交互式权限审批提示面板。

    支持两种模式：
      - 普通模式：显示请求摘要、可展开的详情（支持 diff 着色）、选项列表
      - 反馈模式：显示拒绝理由输入框

    详情区域支持滚动和折叠，选项支持键盘选择和快捷键触发。

    参数:
        request: 权限请求字典，含 summary、details、choices 等字段
        expanded: 是否展开详情区域
        scroll_offset: 详情区域滚动偏移
        selected_choice_index: 当前选中的选项索引
        feedback_mode: 是否处于拒绝反馈输入模式
        feedback_input: 当前反馈输入文本

    返回:
        完整的面板字符串
    """  # t = theme()
    lines: list[str] = []
    if feedback_mode:
        lines.extend([
            f"{t.progress}{t.bold}{ICON_PROMPT} Provide reason for rejection:{t.reset}",
            f"  {t.assistant}{ICON_PROMPT}{t.reset} {feedback_input}_",
            "",
            f"{t.subtle}  Press Enter to send, Esc to cancel.{t.reset}",
        ])
    else:
        lines.extend([request.get("summary", "Permission Request"), ""])
        details = request.get("details", [])
        if details:
            flat = flatten_detail_lines(details)
            if not expanded:
                lines.append(
                    f"{t.subtle}  {ICON_ARROW} {len(flat)} lines hidden "
                    f"{t.subtle}│{t.reset} {t.dim}press 'v' to expand │ Ctrl+O toggle{t.reset}"
                )
            else:
                colorized = colorize_edit_permission_details(flat)
                visible, total = slice_visible_details(colorized, scroll_offset)
                lines.extend(visible)
                if total > len(visible):
                    lines.append(
                        f"{t.subtle}  {ICON_DIVIDER * 3} scroll "
                        f"{scroll_offset + 1}/{total} (Wheel/PgUp/PgDn) "
                        f"{ICON_DIVIDER * 3}{t.reset}"
                    )
            lines.append("")
        for i, choice in enumerate(request.get("choices", [])):
            label = choice.get("label", "")
            key = choice.get("key", "")
            if i == selected_choice_index:
                lines.append(
                    f"  {t.command_highlight_bg}{BRIGHT_CYAN}{ICON_ARROW}{RESET}"
                    f"{t.command_highlight_bg} {BRIGHT_WHITE}{t.bold}{label}{RESET}"
                    f"{t.command_highlight_bg} {t.subtle}({key}){RESET}"
                )
            else:
                lines.append(f"    {t.subtle}{ICON_DOT}{t.reset} {label} {t.subtle}({key}){t.reset}")
    return render_panel("Action Required", "\n".join(lines), right_title="Permission")
