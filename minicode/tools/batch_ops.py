"""批量文件操作工具集。

提供文件/目录的复制、移动和删除等批量操作工具。
所有操作均通过 resolve_tool_path 进行权限检查，确保操作安全合规。

依赖:
    minicode.tooling: ToolDefinition、ToolContext、ToolResult
    minicode.workspace: resolve_tool_path
"""

from __future__ import annotations

import shutil

from minicode.tooling import ToolContext, ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


def _validate_batch_copy(input_data: dict) -> dict:
    """验证 batch_copy 工具的输入参数。

    检查 source 和 destination 是否为非空字符串，去除首尾空格后返回。

    参数:
        input_data: 原始输入字典，包含 "source" 和 "destination" 键。

    返回:
        清洗后的字典，包含 stripped 后的 source 和 destination。

    抛出:
        ValueError: 如果 source 或 destination 缺失或为空。
    """
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_batch_copy(input_data: dict, context: ToolContext) -> ToolResult:
    """执行文件或目录复制。

    使用 shutil.copytree 复制目录，shutil.copy2 复制文件。
    对于目录：如果目标已存在则先删除再复制。
    对于文件：自动创建目标父目录。

    参数:
        input_data: 已验证的输入参数，含 "source" 和 "destination"。
        context: 工具执行上下文，用于解析路径和权限检查。

    返回:
        ToolResult: 复制成功时 ok=True，失败时 ok=False 并附带错误信息。
    """
    try:
        source = resolve_tool_path(context, input_data["source"], "read")
        destination = resolve_tool_path(context, input_data["destination"], "write")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))

    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")

    try:
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            return ToolResult(ok=True, output=f"Copied directory to {input_data['destination']}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return ToolResult(ok=True, output=f"Copied file to {input_data['destination']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Copy failed: {e}")


batch_copy_tool = ToolDefinition(
    name="batch_copy",
    description="Copy files or directories. Supports both files and directories.",
    input_schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path (relative to workspace)"},
            "destination": {"type": "string", "description": "Destination path (relative to workspace)"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_batch_copy,
    run=_run_batch_copy,
)  # # ---------------------------------------------------------------------------
# Batch Move Tool
# ---------------------------------------------------------------------------

def _validate_batch_move(input_data: dict) -> dict:
    """验证 batch_move 工具的输入参数。

    检查 source 和 destination 是否为非空字符串，去除首尾空格后返回。

    参数:
        input_data: 原始输入字典，包含 "source" 和 "destination" 键。

    返回:
        清洗后的字典，包含 stripped 后的 source 和 destination。

    抛出:
        ValueError: 如果 source 或 destination 缺失或为空。
    """
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_batch_move(input_data: dict, context: ToolContext) -> ToolResult:
    """执行文件或目录移动。

    使用 shutil.move 将源路径移动到目标路径。
    自动创建目标父目录。

    参数:
        input_data: 已验证的输入参数，含 "source" 和 "destination"。
        context: 工具执行上下文，用于解析路径和权限检查。

    返回:
        ToolResult: 移动成功时 ok=True，失败时 ok=False 并附带错误信息。
    """
    try:
        source = resolve_tool_path(context, input_data["source"], "read")
        destination = resolve_tool_path(context, input_data["destination"], "write")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))

    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return ToolResult(ok=True, output=f"Moved to {input_data['destination']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Move failed: {e}")


batch_move_tool = ToolDefinition(
    name="batch_move",
    description="Move files or directories to a new location.",
    input_schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path (relative to workspace)"},
            "destination": {"type": "string", "description": "Destination path (relative to workspace)"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_batch_move,
    run=_run_batch_move,
)  # # ---------------------------------------------------------------------------
# Batch Delete Tool
# ---------------------------------------------------------------------------

def _validate_batch_delete(input_data: dict) -> dict:
    """验证 batch_delete 工具的输入参数。

    检查 path 是否为非空字符串。recursive 为可选布尔值，默认为 False。

    参数:
        input_data: 原始输入字典，包含 "path" 和可选的 "recursive"。

    返回:
        清洗后的字典，含 stripped 后的 path 和 recursive 标志。

    抛出:
        ValueError: 如果 path 缺失或为空。
    """
    path = input_data.get("path", "")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path is required")
    return {"path": path.strip(), "recursive": input_data.get("recursive", False)}


def _run_batch_delete(input_data: dict, context: ToolContext) -> ToolResult:
    """执行文件或目录删除。

    删除文件时直接调用 unlink。删除目录时需要设置 recursive=True，
    使用 shutil.rmtree 递归删除。对目录操作若未设置 recursive 则返回错误。

    参数:
        input_data: 已验证的输入参数，含 "path" 和 "recursive"。
        context: 工具执行上下文，用于解析路径和权限检查。

    返回:
        ToolResult: 删除成功时 ok=True，失败时 ok=False 并附带错误信息。
    """
    try:
        target = resolve_tool_path(context, input_data["path"], "delete")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    recursive = input_data.get("recursive", False)

    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {input_data['path']}")

    try:
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
                return ToolResult(ok=True, output=f"Deleted directory: {input_data['path']}")
            else:
                return ToolResult(ok=False, output="Use recursive=true to delete directories")
        else:
            target.unlink()
            return ToolResult(ok=True, output=f"Deleted file: {input_data['path']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Delete failed: {e}")


batch_delete_tool = ToolDefinition(
    name="batch_delete",
    description="Delete files or directories. Directories require recursive=true.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to delete (relative to workspace)"},
            "recursive": {"type": "boolean", "description": "Required to delete directories"}
        },
        "required": ["path"]
    },
    validator=_validate_batch_delete,
    run=_run_batch_delete,
)  # 