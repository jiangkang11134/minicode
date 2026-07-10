"""终端原始输入解析器：将字节流/字符串解析为结构化事件。

将终端发送的原始转义序列和字符流解析为三类事件：
- KeyEvent: 按键事件（方向键、功能键、Ctrl 组合键等）
- TextEvent: 文本输入事件（普通字符、粘贴内容等）
- WheelEvent: 滚轮事件（鼠标滚动）

同时处理 SGR 鼠标、传统鼠标、CSI 光标、CSI 波浪号、SS3、Alt+ 组合、
焦点事件、带括号粘贴（Bracketed Paste）等转义序列。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Union

# Pre-compiled regexes for escape sequence parsing
_SGR_MOUSE_RE = re.compile(r'^\x1b\[<(\d+);(\d+);(\d+)([Mm])')
_CSI_CURSOR_RE = re.compile(r'^\x1b\[(?:1;(\d+))?([A-DF-H])')
_CSI_TILDE_RE = re.compile(r'^\x1b\[(\d+)(?:;(\d+))?~')
_SS3_RE = re.compile(r'^\x1bO([A-DF-H])')
_ESC_CHAR_RE = re.compile(r'^\x1b([^\x1b\[O])')

ParsedKeyName = Literal[
    'return', 'tab', 'backspace', 'delete',
    'up', 'down', 'left', 'right',
    'pageup', 'pagedown', 'home', 'end', 'escape'
]

@dataclass(frozen=True)
class KeyEvent:
    """按键事件，表示一个功能键或 Ctrl 组合键的按下。

    ``name`` 可以是标准键名（return、tab、up、down、escape 等）
    或 Ctrl 组合键对应的字母（如 ``'c'`` 代表 Ctrl+C）。
    """
    name: str  # ParsedKeyName 或控制键字母（如 'a', 'c' 等）
    ctrl: bool
    meta: bool
    kind: str = "key"

@dataclass(frozen=True)
class TextEvent:
    """文本输入事件，表示一个或多个可打印字符的输入。

    包括普通文本字符、粘贴内容、以及 Alt+ 组合（meta 为 True）。
    """
    text: str
    ctrl: bool
    meta: bool
    kind: str = "text"

@dataclass(frozen=True)
class WheelEvent:
    """滚轮事件，表示鼠标滚轮的滚动方向。"""
    direction: Literal['up', 'down']
    kind: str = "wheel"

ParsedInputEvent = Union[KeyEvent, TextEvent, WheelEvent]

@dataclass(frozen=True)
class ParseResult:
    """解析结果，包含已解析的事件列表和剩余未解析的文本片段。

    当输入包含不完整的转义序列时，``rest`` 会保留剩余字符以便
    与下一批输入拼接后继续解析。
    """
    events: list[ParsedInputEvent]
    rest: str

CTRL_CHAR_TO_NAME: dict[str, str] = {
    '\x01': 'a',
    '\x03': 'c',
    '\x05': 'e',
    '\x0e': 'n',
    '\x0f': 'o',
    '\x10': 'p',
    '\x15': 'u',
}

def _is_multiline_paste_chunk(chunk: str) -> bool:
    """判断文本块是否为多行粘贴内容。

    当文本块同时包含换行符和其他字符时，视为多行粘贴（如 ``"line1\\r\\nline2"``），
    此时换行符保留为文本而非触发提交。单独的 ``\\r`` / ``\\n``（真实 Enter 按键）
    不被视为粘贴，仍会触发提交。

    行为与 TS 参考实现的 ``isMultilinePasteChunk`` 保持一致。

    参数:
        chunk: 待检测的文本块

    返回:
        是否为多行粘贴内容
    """
    has_newline = False
    has_other = False
    for c in chunk:
        if c == '\r' or c == '\n':
            has_newline = True
        else:
            has_other = True
        if has_newline and has_other:
            return True
    return False


def maybe_need_more_for_escape_sequence(chunk: str) -> bool:
    """判断是否需要更多字节来完成当前的转义序列。

    检查 chunk 是否以 ``\\x1b`` (ESC) 开头且序列不完整。
    处理 CSI、SS3、SGR 鼠标、传统鼠标等多种转义序列类型。

    参数:
        chunk: 当前累积的文本块

    返回:
        是否需要更多输入才能完成转义序列解析
    """
    if not chunk:
        return False
    if chunk[0] != '\x1b':
        return False
    if len(chunk) == 1:
        return True

    # CSI
    if chunk[1] == '[':
        # SGR Mouse: ESC[<button;x;yM/m
        if len(chunk) >= 3 and chunk[2] == '<':
            return not any(c in 'Mm' for c in chunk[3:])
        # Legacy Mouse: ESC[M...
        if len(chunk) >= 3 and chunk[2] == 'M':
            return len(chunk) < 6
        # CSI cursor/tilde: look for terminator char (A-Z, a-z, ~)
        # Only digits, semicolons, and '?' are valid intermediate/parameter bytes
        for i in range(2, len(chunk)):
            c = chunk[i]
            if 'A' <= c <= 'Z' or 'a' <= c <= 'z' or c == '~':
                return False
            if c not in '0123456789;?':
                # Invalid character in CSI — not a valid sequence, stop waiting
                return False
        # All chars so far are parameter bytes, still waiting for terminator
        return True

    # SS3
    if chunk[1] == 'O':
        return len(chunk) < 3

    # ESC + char (Alt+char)
    # We already checked len(chunk) == 1. For Alt+char, 2 chars is complete.
    return False

def parse_escape_sequence(chunk: str) -> tuple[ParsedInputEvent | None, int]:
    """解析以 ESC (``\\x1b``) 开头的转义序列。

    支持的序列类型：
      - 单独的 ESC: 返回 Escape 按键
      - SGR 鼠标: 解析滚轮方向
      - 传统鼠标: 解析滚轮方向
      - CSI 光标 (ESC[N{A/B/C/D/H/F}): 带修饰符的方向键/Home/End
      - CSI 波浪号 (ESC[N~): Home/Delete/End/PageUp/PageDown
      - SS3 (ESC O A/B/C/D/H/F): 方向键/Home/End
      - ESC+Tab: Alt+Tab
      - ESC+char: Alt+字符

    参数:
        chunk: 以 ``\\x1b`` 开头的文本块

    返回:
        ``(event, consumed)`` 二元组：解析后的事件和已消耗的字符数
    """
    if not chunk or chunk[0] != '\x1b':
        return None, 0

    if len(chunk) == 1:
        return KeyEvent(name='escape', ctrl=False, meta=False), 1

    # SGR Mouse: ESC[<button;x;yM/m
    sgr_match = _SGR_MOUSE_RE.match(chunk)
    if sgr_match:
        button = int(sgr_match.group(1))
        # wheel events (button & 0x43 == 0x40 → up, 0x41 → down)
        if (button & 0x43) == 0x40:
            return WheelEvent(direction='up'), sgr_match.end()
        elif (button & 0x43) == 0x41:
            return WheelEvent(direction='down'), sgr_match.end()
        return None, sgr_match.end()

    # Legacy mouse: ESC[M...
    if chunk.startswith('\x1b[M') and len(chunk) >= 6:
        button = ord(chunk[3])
        if (button & 0x43) == 0x40:
            return WheelEvent(direction='up'), 6
        elif (button & 0x43) == 0x41:
            return WheelEvent(direction='down'), 6
        return None, 6

    # CSI cursor: ESC[{1;modifier}A/B/C/D/H/F
    csi_cursor_match = _CSI_CURSOR_RE.match(chunk)
    if csi_cursor_match:
        mod_str = csi_cursor_match.group(1)
        key_char = csi_cursor_match.group(2)
        mod = int(mod_str) if mod_str else 1

        # Modifier logic: 2=Shift, 3=Alt, 4=Shift+Alt, 5=Ctrl, 6=Shift+Ctrl, 7=Alt+Ctrl, 8=Shift+Alt+Ctrl
        # 1=None.
        # (mod - 1) & 4 -> Ctrl
        # (mod - 1) & 2 -> Alt/Meta
        ctrl = bool((mod - 1) & 4)
        meta = bool((mod - 1) & 2)

        name_map: dict[str, str] = {
            'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left', 'H': 'home', 'F': 'end'
        }
        return KeyEvent(name=name_map[key_char], ctrl=ctrl, meta=meta), csi_cursor_match.end()

    # CSI tilde: ESC[N~ or ESC[N;M~ (with modifier)
    csi_tilde_match = _CSI_TILDE_RE.match(chunk)
    if csi_tilde_match:
        n = int(csi_tilde_match.group(1))
        mod_str = csi_tilde_match.group(2)
        mod = int(mod_str) if mod_str else 1
        ctrl = bool((mod - 1) & 4)
        meta = bool((mod - 1) & 2)
        # 1=home,3=delete,4=end,5=pageup,6=pagedown,7=home,8=end
        tilde_map: dict[int, str] = {
            1: 'home', 3: 'delete', 4: 'end', 5: 'pageup', 6: 'pagedown', 7: 'home', 8: 'end'
        }
        if n in tilde_map:
            return KeyEvent(name=tilde_map[n], ctrl=ctrl, meta=meta), csi_tilde_match.end()
        return None, csi_tilde_match.end()

    # SS3: ESC O A/B/C/D/H/F
    ss3_match = _SS3_RE.match(chunk)
    if ss3_match:
        key_char = ss3_match.group(1)
        ss3_name_map: dict[str, str] = {
            'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left', 'H': 'home', 'F': 'end'
        }
        return KeyEvent(name=ss3_name_map[key_char], ctrl=False, meta=False), ss3_match.end()

    # ESC+Tab
    if chunk.startswith('\x1b\t'):
        return KeyEvent(name='tab', ctrl=False, meta=True), 2

    # ESC+char (Alt+char)
    esc_char_match = _ESC_CHAR_RE.match(chunk)
    if esc_char_match:
        char = esc_char_match.group(1)
        return TextEvent(text=char, ctrl=False, meta=True), 2

    # Default to bare escape if nothing else matches and we are not waiting for more
    return KeyEvent(name='escape', ctrl=False, meta=False), 1

def parse_input_chunk(chunk: str, incoming_chunk: str | None = None) -> ParseResult:
    """将原始终端输入文本块解析为结构化事件列表。

    逐字符遍历输入文本，将转义序列、控制字符、普通文本分类处理：
      - 焦点事件 (``\\x1b[I`` / ``\\x1b[O``)
      - 带括号粘贴 (``\\x1b[200~`` ... ``\\x1b[201~``)
      - 其他转义序列（方向键、功能键等）
      - 回车/换行：触发生成 ``KeyEvent(name='return')``，
        多行粘贴时保留换行为 ``TextEvent``
      - Tab、Backspace、Ctrl 组合键
      - 普通文本：生成 ``TextEvent``

    参数:
        chunk: 待解析的完整文本（通常是 ``remainder + incoming_chunk``）
        incoming_chunk: 新到达的原始文本块（未与 remainder 拼接）。
            当提供此参数时，多行粘贴检测仅针对此块（而非累积的 remainder），
            与 TS 参考实现行为一致。默认与 ``chunk`` 相同。

    返回:
        包含事件列表和未解析剩余文本的 ``ParseResult``
    """
    treat_newlines_as_text = _is_multiline_paste_chunk(
        incoming_chunk if incoming_chunk is not None else chunk
    )
    events: list[ParsedInputEvent] = []
    i = 0
    while i < len(chunk):
        if maybe_need_more_for_escape_sequence(chunk[i:]):
            break

        char = chunk[i]

        # Escape sequence
        if char == '\x1b':
            # Focus in/out: \x1b[I / \x1b[O
            if chunk[i:i+3] == '\x1b[I':
                events.append(KeyEvent(name='focus_in', ctrl=False, meta=False))
                i += 3
                continue
            if chunk[i:i+3] == '\x1b[O':
                events.append(KeyEvent(name='focus_out', ctrl=False, meta=False))
                i += 3
                continue

            # Bracketed paste start: \x1b[200~
            if chunk[i:i+6] == '\x1b[200~' and not maybe_need_more_for_escape_sequence(chunk[i+6:]):
                i += 6
                # Accumulate until paste end \x1b[201~
                paste_end = chunk.find('\x1b[201~', i)
                if paste_end >= 0:
                    paste_text = chunk[i:paste_end]
                    # Strip control characters except newline and tab
                    paste_text = ''.join(c for c in paste_text if c.isprintable() or c in '\n\t')
                    events.append(TextEvent(text=paste_text, ctrl=False))
                    i = paste_end + 6  # Skip past \x1b[201~
                    continue
                else:
                    break  # Need more input for paste end

            event, consumed = parse_escape_sequence(chunk[i:])
            if event:
                events.append(event)
            i += consumed
            continue

        # Carriage return / line feed: submit, unless this is a multi-line paste
        # (in which case the newline is preserved as text). \r\n and \n\r are
        # consumed as a single newline. Mirrors TS parseInputChunk.
        if char == '\r' or char == '\n':
            if treat_newlines_as_text:
                events.append(TextEvent(text='\n', ctrl=False, meta=False))
            else:
                events.append(KeyEvent(name='return', ctrl=False, meta=False))
            if (char == '\r' and i + 1 < len(chunk) and chunk[i + 1] == '\n') or (
                char == '\n' and i + 1 < len(chunk) and chunk[i + 1] == '\r'
            ):
                i += 2
            else:
                i += 1
            continue

        # Tab
        if char == '\t':
            events.append(KeyEvent(name='tab', ctrl=False, meta=False))
            i += 1
            continue

        # Backspace (0x7f, 0x08)
        if char in ('\x7f', '\x08'):
            events.append(KeyEvent(name='backspace', ctrl=False, meta=False))
            i += 1
            continue

        # Ctrl chars (0x01-0x1a)
        if '\x01' <= char <= '\x1a':
            if char in CTRL_CHAR_TO_NAME:
                events.append(KeyEvent(name=CTRL_CHAR_TO_NAME[char], ctrl=True, meta=False))
            # Swallow other control characters
            i += 1
            continue

        # Regular text
        events.append(TextEvent(text=char, ctrl=False, meta=False))
        i += 1

    return ParseResult(events=events, rest=chunk[i:])
