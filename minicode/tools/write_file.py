"""写入文件工具。

提供文件写入功能，将内容写入工作区中的指定文件。写入操作会经过文件审阅流程。
"""
from __future__ import annotations

from minicode.file_review import apply_reviewed_file_change
from minicode.tooling import ToolDefinition
from minicode.workspace import resolve_tool_path


def _validate(input_data: dict) -> dict:
    """验证并规范化写入文件工具的输入参数。

    参数:
        input_data: 包含 path 和 content 字段的原始输入字典。

    返回:
        规范化后的参数字典，包含 path 和 content 键。

    抛出:
        ValueError: 如果 path 为空或类型错误、content 不是字符串。
    """  # path = input_data.get("path")
    content = input_data.get("content")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    return {"path": path, "content": content}


def _run(input_data: dict, context):
    """执行写入文件操作。

    解析文件路径并将内容写入目标文件，写入前会经过文件审阅流程。

    参数:
        input_data: 已验证的输入字典，包含 path 和 content。
        context: 工具执行上下文，用于解析工作区路径和进行文件审阅。
    """  # target = resolve_tool_path(context, input_data["path"], "write")
    return apply_reviewed_file_change(context, input_data["path"], target, input_data["content"])


write_file_tool = ToolDefinition(
    name="write_file",
    description="Write a UTF-8 text file relative to the workspace root.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    validator=_validate,
    run=_run,
)  # 