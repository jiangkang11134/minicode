from __future__ import annotations

"""文件树可视化工具集。

提供以树形结构展示目录和文件的功能，支持显示文件大小、
修改时间、文件类型图标，以及按深度限制和模式过滤。
"""

import time
from datetime import datetime
from pathlib import Path
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


# ---------------------------------------------------------------------------
# File Tree Helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的文件大小表示。

    自动选择合适的单位（B、KB、MB、GB、TB），保留一位小数。
    例如：1024 -> "1.0KB"，1536 -> "1.5KB"。

    参数:
        size_bytes: 文件大小，单位为字节。

    返回:
        带单位的可读大小字符串。
    """  # for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def _format_time(timestamp: float) -> str:
    """将时间戳格式化为人类可读的相对时间或日期。

    根据距离当前时间的远近返回不同格式：
    - 不到 1 小时：Xm ago（分钟前）
    - 不到 24 小时：Xh ago（小时前）
    - 不到 7 天：Xd ago（天前）
    - 超过 7 天：YYYY-MM-DD 格式日期

    参数:
        timestamp: Unix 时间戳（秒）。

    返回:
        可读的相对时间或日期字符串。
    """  # dt = datetime.fromtimestamp(timestamp)
    now = time.time()
    diff = now - timestamp

    if diff < 3600:
        mins = int(diff / 60)
        return f"{mins}m ago"
    elif diff < 86400:
        hours = int(diff / 3600)
        return f"{hours}h ago"
    elif diff < 604800:
        days = int(diff / 86400)
        return f"{days}d ago"
    else:
        return dt.strftime("%Y-%m-%d")


def _get_file_icon(file_path: Path) -> str:
    """根据文件扩展名返回对应的 Emoji 图标。

    支持常见的编程语言文件、配置文件和媒体文件类型。
    对于 README 文件返回单独图标，未知扩展名返回通用文件图标。

    参数:
        file_path: 文件的 Path 对象。

    返回:
        表示文件类型的 Emoji 字符串。
    """  # ext = file_path.suffix.lower()
    icons = {
        '.py': '🐍',
        '.js': '📜',
        '.ts': '🔷',
        '.jsx': '⚛️',
        '.tsx': '⚛️',
        '.html': '🌐',
        '.css': '🎨',
        '.md': '📝',
        '.json': '📋',
        '.yaml': '⚙️',
        '.yml': '⚙️',
        '.toml': '⚙️',
        '.txt': '📄',
        '.log': '📃',
        '.sh': '🖥️',
        '.bat': '🖥️',
        '.gitignore': '🚫',
        '.env': '🔒',
        '.lock': '🔒',
        '.png': '🖼️',
        '.jpg': '🖼️',
        '.jpeg': '🖼️',
        '.svg': '🎭',
        '.ipynb': '📓',
    }
    if file_path.name == 'README':
        return '📖'
    return icons.get(ext, '📄')


def _get_file_status_color(file_path: Path) -> str:
    """根据文件修改时间返回表示新鲜度的状态 Emoji 指示符。

    返回的标记：
    - 🟢（绿色）：最近 1 小时内修改
    - 🟡（黄色）：最近 24 小时内修改
    - ⚪（白色）：超过 24 小时未修改

    参数:
        file_path: 文件的 Path 对象。

    返回:
        表示修改时间新鲜度的 Emoji 字符串。
    """  # now = time.time()
    age = now - file_path.stat().st_mtime

    if age < 3600:  # Modified within 1 hour
        return "🟢"
    elif age < 86400:  # Modified within 24 hours
        return "🟡"
    else:
        return "⚪"


def _build_tree(
    path: Path,
    prefix: str = "",
    is_last: bool = True,
    max_depth: int = 3,
    current_depth: int = 0,
    show_hidden: bool = False,
    ignore_dirs: set[str] | None = None,
) -> list[str]:
    """递归构建可视化的文件树形结构。

    使用 ├──、└──、│ 等制表符绘制树形连接线。
    对目录显示 📁 图标并递归进入子目录（受 max_depth 限制）；
    对文件显示图标、状态颜色、大小和修改时间。
    自动跳过不可读目录和配置的忽略目录列表。

    参数:
        path: 要遍历的目录 Path。
        prefix: 当前层级的前缀字符串（用于子目录递归传递连接线）。
        is_last: 当前节点是否为同级最后一个条目。
        max_depth: 递归遍历的最大深度。
        current_depth: 当前递归深度。
        show_hidden: 是否显示以点开头的隐藏文件/目录。
        ignore_dirs: 要忽略跳过的目录名称集合。

    返回:
        树的每一行字符串列表。
    """  # if ignore_dirs is None:
        ignore_dirs = {'.git', '__pycache__', 'venv', 'env', '.tox', 'node_modules', '.mypy_cache', '.pytest_cache'}

    lines = []

    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return [f"{prefix}{'└── ' if is_last else '├── '}🔒 Permission denied"]

    # Filter hidden files
    if not show_hidden:
        entries = [e for e in entries if not e.name.startswith('.')]

    # Filter ignored directories
    if path.is_dir():
        entries = [e for e in entries if not (e.is_dir() and e.name in ignore_dirs)]

    for i, entry in enumerate(entries):
        is_last_entry = (i == len(entries) - 1)

        # Choose connector
        connector = "└── " if is_last_entry else "├── "
        extension = "    " if is_last_entry else "│   "

        if entry.is_dir():
            icon = "📁"
            lines.append(f"{prefix}{connector}{icon} {entry.name}")

            if current_depth < max_depth:
                lines.extend(_build_tree(
                    entry,
                    prefix + extension,
                    is_last_entry,
                    max_depth,
                    current_depth + 1,
                    show_hidden,
                    ignore_dirs,
                ))
            else:
                lines.append(f"{prefix}{extension}    ...")
        else:
            icon = _get_file_icon(entry)
            status = _get_file_status_color(entry)
            size = _format_size(entry.stat().st_size)
            mod_time = _format_time(entry.stat().st_mtime)

            lines.append(f"{prefix}{connector}{status} {icon} {entry.name} ({size}, {mod_time})")

    return lines


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

def _validate(input_data: dict) -> dict:
    """验证 file_tree 工具的输入参数。

    检查 path、max_depth、show_hidden 和 pattern 参数的合法性。

    参数:
        input_data: 原始输入字典，包含 path、max_depth、show_hidden 和可选的 pattern。

    返回:
        验证后的字典。

    抛出:
        ValueError: 当 max_depth 超出 1-10 范围或 show_hidden 不是布尔值时。
    """  # path = input_data.get("path", ".")
    max_depth = int(input_data.get("max_depth", 3))
    if max_depth < 1 or max_depth > 10:
        raise ValueError("max_depth must be between 1 and 10")
    show_hidden = input_data.get("show_hidden", False)
    if not isinstance(show_hidden, bool):
        raise ValueError("show_hidden must be a boolean")

    pattern = input_data.get("pattern")

    return {
        "path": path,
        "max_depth": max_depth,
        "show_hidden": show_hidden,
        "pattern": pattern,
    }


def _run(input_data: dict, context) -> ToolResult:
    """执行文件树展示，输出可视化的目录结构。

    通过 resolve_tool_path 解析目标路径，使用 _build_tree 递归构建树形结构。
    支持通过 pattern 参数使用 glob 模式过滤显示的文件行，
    并在输出底部附上文件和目录统计信息以及图例说明。

    参数:
        input_data: 包含 path（目标路径）、max_depth（最大深度）、
                    show_hidden（是否显示隐藏文件）和 pattern（过滤模式）的字典。
        context: 工具运行时上下文，用于路径解析。

    返回:
        包含格式化文件树文本的 ToolResult；路径不存在或权限不足时返回错误信息。
    """  # try:
        target = resolve_tool_path(context, input_data["path"], "list")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    max_depth = input_data["max_depth"]
    show_hidden = input_data["show_hidden"]
    pattern = input_data.get("pattern")

    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {target}")

    # Build tree
    tree_lines = _build_tree(
        target,
        max_depth=max_depth,
        show_hidden=show_hidden,
    )

    # Apply pattern filter if provided
    if pattern:
        import fnmatch
        tree_lines = [
            line for line in tree_lines
            if fnmatch.fnmatch(line, f"*{pattern}*")
        ]
        if not tree_lines:
            return ToolResult(
                ok=True,
                output=f"No files match pattern '{pattern}'",
            )

    # Count stats
    try:
        total_files = sum(1 for _ in target.rglob("*") if _.is_file() and not _.name.startswith('.'))
        total_dirs = sum(1 for _ in target.rglob("*") if _.is_dir() and not _.name.startswith('.'))
    except Exception:
        total_files = 0
        total_dirs = 0

    # Format output
    lines = [
        f"📂 File Tree: {input_data['path']}",
        "=" * 60,
        "",
    ]

    # Add target name at root
    if target.is_dir():
        lines.append(f"📁 {target.name}")
        for line in tree_lines:
            lines.append(f"  {line}")
    else:
        for line in tree_lines:
            lines.append(line)

    lines.extend([
        "",
        "-" * 60,
        "📊 Stats:",
        f"  Files: {total_files}",
        f"  Directories: {total_dirs}",
        f"  Max depth shown: {max_depth}",
    ])

    # Legend
    lines.extend([
        "",
        "🎨 Legend:",
        "  🟢 Modified < 1h ago",
        "  🟡 Modified < 24h ago",
        "  ⚪ Modified > 24h ago",
    ])

    return ToolResult(ok=True, output="\n".join(lines))


file_tree_tool = ToolDefinition(
    name="file_tree",
    description="Display a visual file tree with file sizes, modification times, and type icons. Supports filtering by pattern and controlling depth.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory or file path to display (default: current directory)"},
            "max_depth": {"type": "number", "description": "Maximum depth to display (default: 3, max: 10)"},
            "show_hidden": {"type": "boolean", "description": "Show hidden files (starting with .) (default: false)"},
            "pattern": {"type": "string", "description": "Filter files by glob pattern (e.g., '*.py', 'test_*')"},
        },
    },
    validator=_validate,
    run=_run,
)  # 