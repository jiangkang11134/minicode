from __future__ import annotations

"""文件列表工具。

列出工作区中指定路径下的文件和目录，
通过 ToolDefinition 注册到 SmartCode 工具系统中。
"""

from pathlib import Path

from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


def _validate(input_data: dict) -> dict:
    """校验 list_files 工具的输入参数。

    确保 path 字段为字符串类型（如果提供），否则默认为当前目录。

    参数:
        input_data: 原始输入字典，可选包含 "path" 键

    返回:
        规范化后的字典，包含 path 键，默认为 "."

    抛出:
        ValueError: path 存在但类型不是字符串
    """
    if "path" in input_data and not isinstance(input_data["path"], str):
        raise ValueError("path must be a string")
    return {"path": input_data.get("path", ".")}


def _run(input_data: dict, context) -> ToolResult:
    """列出指定路径下的文件和目录。

    如果目标路径是一个文件，则直接返回文件名；
    如果是目录，则按名称排序列出所有条目（dir 前缀表示目录，file 前缀表示文件），
    最多输出 200 条。

    参数:
        input_data: 经 _validate 校验后的输入字典
        context: 工具运行上下文，用于解析工作区安全路径

    返回:
        ToolResult，包含文件/目录列表，或路径不存在的错误信息
    """
    target = resolve_tool_path(context, input_data["path"], "list")
    if not target.exists():
        return ToolResult(ok=False, output=f"Path does not exist: {input_data['path']}")
    if target.is_file():
        return ToolResult(ok=True, output=f"file {Path(input_data['path']).name}")

    entries = sorted(Path(target).iterdir(), key=lambda item: item.name.lower())
    lines = []
    for entry in entries:
        lines.append(f"{'dir ' if entry.is_dir() else 'file'} {entry.name}")
    return ToolResult(ok=True, output="\n".join(lines[:200]) if lines else "(empty)")


list_files_tool = ToolDefinition(
    name="list_files",
    description="List files and directories relative to the workspace root.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    validator=_validate,
    run=_run,
)  # 