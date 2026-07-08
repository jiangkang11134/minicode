"""SmartCode TUI 的 Morandi 色彩主题。

低饱和度调色板，灵感来自 Rust 版本的 ColorTheme。
所有颜色以 ANSI 256 色或 24 位（RGB）转义码形式表示。
"""

from __future__ import annotations

from dataclasses import dataclass


def _rgb(r: int, g: int, b: int) -> str:
    """生成 24 位前景色 ANSI 转义码。

    参数:
        r: 红色分量（0-255）
        g: 绿色分量（0-255）
        b: 蓝色分量（0-255）

    返回:
        str: 24 位前景色 ANSI 转义序列
    """
    return f"\x1b[38;2;{r};{g};{b}m"


def _rgb_bg(r: int, g: int, b: int) -> str:
    """生成 24 位背景色 ANSI 转义码。

    参数:
        r: 红色分量（0-255）
        g: 绿色分量（0-255）
        b: 蓝色分量（0-255）

    返回:
        str: 24 位背景色 ANSI 转义序列
    """
    return f"\x1b[48;2;{r};{g};{b}m"


@dataclass(frozen=True)
class ColorTheme:
    """Morandi 风格的低饱和度色彩主题。

    包含面板边框、消息类型、UI 装饰和文字工具等多个颜色分类。
    所有字段为固定值（frozen dataclass），颜色值以 ANSI 转义字符串表示。
    """

    # Section borders / frames
    header: str        # Workspace header border
    session: str       # Session feed border
    input: str         # Input box border
    approval: str      # Approval dialog border

    # Message kinds
    user: str          # User messages
    assistant: str     # Assistant messages
    progress: str      # Progress messages
    tool: str          # Tool messages
    tool_error: str    # Tool error messages

    # UI chrome
    command_highlight_bg: str   # Slash command highlight background
    expandable: str             # [展开]/[收起] toggle text

    # Header label colors
    header_label_info: str       # project / provider / model / auth labels
    header_label_session: str    # session label
    header_label_permissions: str  # permissions / cwd labels
    header_label_recent: str     # recent tools label

    # Text utilities
    reset: str = "\x1b[0m"
    bold: str = "\x1b[1m"
    dim: str = "\x1b[2m"
    italic: str = "\x1b[3m"
    underline: str = "\x1b[4m"
    reverse: str = "\x1b[7m"

    # Semantic aliases
    subtle: str = "\x1b[38;5;243m"    # gray for subtle/secondary text
    border: str = "\x1b[38;5;39m"     # bright blue (legacy panel borders)
    border_dim: str = "\x1b[38;5;24m" # secondary border
    accent: str = "\x1b[38;5;214m"    # warm orange accent
    accent2: str = "\x1b[38;5;141m"   # soft purple accent
    highlight_bg: str = "\x1b[48;5;236m"  # dark selection background


def _default_theme() -> ColorTheme:
    """构建默认的 Morandi 色彩主题实例。

    返回:
        ColorTheme: 包含 Morandi 色系各颜色分量的主题实例
    """
    return ColorTheme(
        # Section borders — Morandi tones
        header=_rgb(120, 150, 140),      # muted teal
        session=_rgb(140, 120, 160),     # muted purple
        input=_rgb(130, 160, 100),       # muted sage green
        approval=_rgb(170, 110, 110),    # muted mauve

        # Message kinds
        user=_rgb(160, 130, 100),        # muted warm brown
        assistant=_rgb(100, 150, 150),   # muted teal-cyan
        progress=_rgb(170, 150, 90),     # muted mustard
        tool=_rgb(140, 100, 160),        # muted purple-plum
        tool_error=_rgb(180, 100, 100),  # muted rose

        # UI chrome
        command_highlight_bg=_rgb_bg(100, 110, 140),  # muted slate-blue bg
        expandable=_rgb(110, 150, 150),  # muted cyan-gray

        # Header labels
        header_label_info=_rgb(170, 150, 100),        # muted ochre
        header_label_session=_rgb(160, 120, 100),     # muted terracotta
        header_label_permissions=_rgb(130, 100, 160), # muted plum
        header_label_recent=_rgb(130, 100, 160),      # same as permissions
    )


# Module-level singleton
_THEME: ColorTheme | None = None


def theme() -> ColorTheme:
    """返回全局 ColorTheme 单例。

    第一次调用时创建实例并缓存，后续直接返回缓存实例。

    返回:
        ColorTheme: 全局唯一的色彩主题实例
    """  # global _THEME
    if _THEME is None:
        _THEME = _default_theme()
    return _THEME
