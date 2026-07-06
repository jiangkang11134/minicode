"""文件审阅与变更应用工具。

提供文件 diff 生成、内容加载以及审核后的文件变更应用功能。
用于代码审阅流程中安全地将审阅通过的修改写回文件系统。
"""
from __future__ import annotations

import difflib
from pathlib import Path

from minicode.session import create_file_checkpoint
from minicode.tooling import ToolContext, ToolResult


def build_unified_diff(file_path: str, before: str, after: str) -> str:
    """生成两个文件内容之间的 unified diff 字符串。

    使用 difflib.unified_diff 生成标准 diff 格式，上下文行数为 3。
    自动过滤冗余的分隔线（全等号行）使输出更紧凑。
    若前后内容相同则返回 "(no changes for ...)"。

    参数:
        file_path: 文件路径，用于 diff 头部标识
        before: 原始文件内容
        after: 修改后的文件内容

    返回:
        格式化的 diff 字符串，或无变更提示
    """
    # if before == after:
        return f"(no changes for {file_path})"
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
        n=3,
    )
    # Strip redundant separator lines (e.g. "=" lines) for compact display
    lines = [line for line in diff if not (line.startswith("=") and set(line.strip()) == {"="})]
    return "\n".join(lines)


def load_existing_file(target_path: str | Path) -> str:
    """以 UTF-8 编码读取指定文件的内容，文件不存在时返回空字符串。

    参数:
        target_path: 文件路径（字符串或 Path 对象）

    返回:
        文件内容字符串；若文件不存在则返回空字符串
    """
    # file_path = Path(target_path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8")


def apply_reviewed_file_change(
    context: ToolContext,
    file_path: str,
    target_path: str | Path,
    next_content: str,
) -> ToolResult:
    """应用审阅通过的文件变更，将新内容安全写入文件系统。

    流程：
    1. 读取目标文件的当前内容
    2. 若内容无变化则直接返回（No changes needed）
    3. 生成 diff 并通过权限系统校验（若 permissions 可用）
    4. 创建文件变更检查点（用于回滚）
    5. 确保目标父目录存在
    6. 将新内容写入文件

    参数:
        context: 工具上下文，包含 session 和 permissions 信息
        file_path: 代码中引用的逻辑文件路径（用于 diff 和日志）
        target_path: 文件系统上的实际目标路径
        next_content: 审阅通过后的新文件内容

    返回:
        包含操作结果的 ToolResult 对象
    """
    # target = Path(target_path)
    previous_content = load_existing_file(target)
    if previous_content == next_content:
        return ToolResult(ok=True, output=f"No changes needed for {file_path}")

    diff = build_unified_diff(file_path, previous_content, next_content)
    if context.permissions is not None:
        context.permissions.ensure_edit(str(target), diff)

    create_file_checkpoint(
        context.session,
        file_path=str(target),
        existed=target.exists(),
        previous_content=previous_content,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(next_content, encoding="utf-8")
    return ToolResult(ok=True, output=f"Applied reviewed changes to {file_path}")
