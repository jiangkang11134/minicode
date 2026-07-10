from __future__ import annotations

"""文件差异对比工具集。

提供 unified diff、inline diff 和 stat 三种格式的文件差异对比功能，
支持直接传入文件内容或通过 __file__ 标记从磁盘加载文件内容进行比较。
"""

import difflib
from pathlib import Path
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult

# ---------------------------------------------------------------------------
# Diff Viewer Helpers
# ---------------------------------------------------------------------------

def _colorize_line(line: str) -> str:
    """为 diff 输出行添加 ANSI 颜色代码以便终端显示。

    规则：
    - 以 + 开头的行显示为绿色（新增）
    - 以 - 开头的行显示为红色（删除）
    - 以 @@ 开头的行显示为青色（块头）
    - 以 --- 或 +++ 开头的行显示为粗体（文件头）

    参数:
        line: diff 文本中的一行。

    返回:
        添加了 ANSI 转义码的着色字符串。

    重要程度: """
    if line.startswith('+'):
        return f"\033[32m{line}\033[0m"  # Green
    elif line.startswith('-'):
        return f"\033[31m{line}\033[0m"  # Red
    elif line.startswith('@@'):
        return f"\033[36m{line}\033[0m"  # Cyan
    elif line.startswith('---') or line.startswith('+++'):
        return f"\033[1m{line}\033[0m"  # Bold
    else:
        return line


def _generate_diff(old_content: str, new_content: str, old_name: str, new_name: str, context_lines: int = 3) -> str:
    """生成 unified diff 格式的差异文本。

    使用 difflib.unified_diff 对旧内容和新内容进行逐行比较，
    输出标准 unified diff 格式结果。

    参数:
        old_content: 旧版本的完整文本内容。
        new_content: 新版本的完整文本内容。
        old_name: 旧文件的显示名称（通常以 a/ 开头）。
        new_name: 新文件的显示名称（通常以 b/ 开头）。
        context_lines: 每个差异块上下文的行数，默认为 3。

    返回:
        Unified diff 格式的字符串。

    重要程度: """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=old_name,
        tofile=new_name,
        n=context_lines,
    )

    return ''.join(diff)


def _generate_inline_diff(old_content: str, new_content: str, context_lines: int = 3) -> list[dict[str, Any]]:
    """生成 inline 格式的差异结构，按变更类型分组。

    使用 difflib.SequenceMatcher 对比两段文本，
    识别出 replace（替换）、delete（删除）、insert（插入）三种操作，
    并记录每处变更的行号范围和具体行内容。

    参数:
        old_content: 旧版本的完整文本内容。
        new_content: 新版本的完整文本内容。
        context_lines: 参数保留供扩展，当前未使用。

    返回:
        变更列表，每个元素为包含 type、old_start、old_end、new_start、new_end、
        old_lines、new_lines 的字典。

    重要程度: """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        changes.append({
            "type": tag,  # 'replace', 'delete', 'insert'
            "old_start": i1,
            "old_end": i2,
            "new_start": j1,
            "new_end": j2,
            "old_lines": old_lines[i1:i2],
            "new_lines": new_lines[j1:j2],
        })

    return changes


def _format_diff_output(diff_text: str, max_lines: int = 100) -> str:
    """格式化 diff 输出，截断过长内容并添加颜色。

    如果 diff 行数超过 max_lines，仅保留前 max_lines 行并追加截断提示。
    每行通过 _colorize_line 添加 ANSI 颜色。

    参数:
        diff_text: 原始 diff 文本。
        max_lines: 最大显示行数，默认为 100。

    返回:
        格式化后的 diff 文本字符串。

    重要程度: """
    lines = diff_text.split('\n')

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"\n... (diff truncated, showing first {max_lines} lines)")

    # Add colors (for terminals that support it)
    colored_lines = [_colorize_line(line) for line in lines]

    return '\n'.join(colored_lines)


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

def _validate(input_data: dict) -> dict:
    """验证 diff_viewer 工具的输入参数。

    检查 files 列表的结构是否合法（每个条目需包含 path 字段，
    以及 before 或 after 字段之一），验证 context_lines 和 format
    的取值范围。

    参数:
        input_data: 原始输入字典，包含 files、context_lines 和 format。

    返回:
        验证后的字典。

    抛出:
        ValueError: 当 files 格式不正确、context_lines 超出范围
                    或 format 值不合法时。

    重要程度: """
    files = input_data.get("files")
    if not isinstance(files, list):
        raise ValueError("files must be a list")
    if not files:
        raise ValueError("files cannot be empty")

    for i, file_entry in enumerate(files):
        if not isinstance(file_entry, dict):
            raise ValueError(f"files[{i}] must be an object")
        if "path" not in file_entry:
            raise ValueError(f"files[{i}] must have a 'path' field")
        if "before" not in file_entry and "after" not in file_entry:
            raise ValueError(f"files[{i}] must have 'before' or 'after' field")

    context_lines = int(input_data.get("context_lines", 3))
    if context_lines < 1 or context_lines > 10:
        raise ValueError("context_lines must be between 1 and 10")
    format_type = input_data.get("format", "unified")
    if format_type not in ("unified", "inline", "stat"):
        raise ValueError("format must be one of: unified, inline, stat")

    return {
        "files": files,
        "context_lines": context_lines,
        "format": format_type,
    }


def _run(input_data: dict, context) -> ToolResult:
    """执行文件差异对比，支持三种格式输出。

    遍历待比较的文件列表，对每个文件根据 format 类型生成差异：
    - unified：标准 unified diff 格式，统计增减行数
    - inline：逐处显示变更类型（替换/删除/插入）及行号
    - stat：仅显示每个文件增减的行数统计
    支持在 before/after 中使用 __file__ 标记从磁盘加载内容。

    参数:
        input_data: 包含 files（待比较文件列表）、context_lines（上下文行数）
                    和 format（输出格式）的字典。
        context: 工具运行时上下文，用于解析文件路径。

    返回:
        包含格式化差异输出文本的 ToolResult。

    重要程度: """
    files = input_data["files"]
    context_lines = input_data["context_lines"]
    format_type = input_data["format"]
    cwd = Path(context.cwd)

    all_diffs = []
    total_additions = 0
    total_deletions = 0
    files_with_changes = 0

    for file_entry in files:
        file_path = cwd / file_entry["path"]
        old_content = file_entry.get("before", "")
        new_content = file_entry.get("after", "")

        # Load from file if not provided
        if old_content == "__file__":
            if file_path.exists():
                old_content = file_path.read_text(encoding="utf-8")
            else:
                old_content = ""
        if new_content == "__file__":
            if file_path.exists():
                new_content = file_path.read_text(encoding="utf-8")
            else:
                new_content = ""

        # Skip if no changes
        if old_content == new_content:
            all_diffs.append({
                "file": file_entry["path"],
                "status": "unchanged",
                "diff": "",
            })
            continue

        files_with_changes += 1

        # Generate diff based on format
        if format_type == "stat":
            # Simple stats
            old_lines = old_content.count('\n') + 1 if old_content else 0
            new_lines = new_content.count('\n') + 1 if new_content else 0
            additions = max(0, new_lines - old_lines)
            deletions = max(0, old_lines - new_lines)

            total_additions += additions
            total_deletions += deletions

            all_diffs.append({
                "file": file_entry["path"],
                "status": "changed",
                "diff": f"{file_entry['path']}: +{additions} -{deletions} lines",
            })
        elif format_type == "inline":
            # Inline diff
            changes = _generate_inline_diff(old_content, new_content, context_lines)

            diff_lines = [f"📄 {file_entry['path']}", ""]
            for change in changes[:10]:  # Limit to 10 changes per file
                if change["type"] == "replace":
                    diff_lines.append(f"  L{change['old_start']+1} → L{change['new_start']+1}:")
                    for line in change["old_lines"]:
                        diff_lines.append(f"    - {line}")
                    for line in change["new_lines"]:
                        diff_lines.append(f"    + {line}")
                elif change["type"] == "delete":
                    diff_lines.append(f"  L{change['old_start']+1}: DELETED")
                    for line in change["old_lines"]:
                        diff_lines.append(f"    - {line}")
                elif change["type"] == "insert":
                    diff_lines.append(f"  L{change['new_start']+1}: INSERTED")
                    for line in change["new_lines"]:
                        diff_lines.append(f"    + {line}")

                diff_lines.append("")

            all_diffs.append({
                "file": file_entry["path"],
                "status": "changed",
                "diff": "\n".join(diff_lines),
            })
        else:
            # Unified diff (default)
            old_name = f"a/{file_entry['path']}"
            new_name = f"b/{file_entry['path']}"
            diff_text = _generate_diff(old_content, new_content, old_name, new_name, context_lines)

            # Count additions and deletions
            for line in diff_text.split('\n'):
                if line.startswith('+') and not line.startswith('+++'):
                    total_additions += 1
                elif line.startswith('-') and not line.startswith('---'):
                    total_deletions += 1

            all_diffs.append({
                "file": file_entry["path"],
                "status": "changed",
                "diff": diff_text,
            })

    # Format output
    lines = ["🔍 Diff Viewer", "=" * 70, ""]

    lines.append(f"Files compared: {len(files)}")
    lines.append(f"Files with changes: {files_with_changes}")

    if format_type == "stat":
        lines.append(f"Total additions: +{total_additions} lines")
        lines.append(f"Total deletions: -{total_deletions} lines")

    lines.append("")
    lines.append("-" * 70)
    lines.append("")

    for diff_entry in all_diffs:
        if diff_entry["status"] == "unchanged":
            lines.append(f"✓ {diff_entry['file']} (no changes)")
        else:
            lines.append(f"📝 {diff_entry['file']}")
            lines.append(diff_entry["diff"])

        lines.append("")
        lines.append("-" * 70)
        lines.append("")

    if files_with_changes == 0:
        lines.append("✓ All files are identical. No differences found.")

    return ToolResult(ok=True, output="\n".join(lines))


diff_viewer_tool = ToolDefinition(
    name="diff_viewer",
    description="View differences between file versions with unified diff, inline diff, or stats. Supports comparing files, showing before/after, or comparing against current file on disk.",
    input_schema={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "List of files to compare. Each entry: {path, before?, after?}. Use '__file__' to load from disk.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (for display)"},
                        "before": {"type": "string", "description": "Original content (or '__file__' to load from disk)"},
                        "after": {"type": "string", "description": "New content (or '__file__' to load from disk)"},
                    },
                    "required": ["path"],
                },
            },
            "context_lines": {"type": "number", "description": "Number of context lines around changes (default: 3)"},
            "format": {"type": "string", "enum": ["unified", "inline", "stat"], "description": "Diff format (default: unified)"},
        },
        "required": ["files"],
    },
    validator=_validate,
    run=_run,
)
