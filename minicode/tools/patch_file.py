"""patch_file 工具实现——对单个文件执行多次精确文本替换。

此模块定义 patch_file 工具，支持在一次操作中对文件进行多次文本替换，
每次替换可选择单次替换或全部替换模式。替换完成后生成差异供用户审核批准。
"""
from __future__ import annotations

from minicode.file_review import apply_reviewed_file_change, load_existing_file
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


def _validate(input_data: dict) -> dict:
    """验证 patch_file 工具的输入数据。

    检查 path、replacements 和 patch 参数的有效性，并进行标准化处理。
    将 \\r\\n 统一转换为 \\n 以确保跨平台兼容。同时将简写的 patch 参数
    自动转换为标准的 replacements 格式。

    参数:
        input_data: 包含 path、replacements（列表）和/或 patch（字符串）的字典。

    返回:
        标准化后的字典，包含 path 和 replacements 列表。

    抛出:
        ValueError: 当 path 缺失、replacements 为空或格式不正确时。
    """  # path = input_data.get("path")
    replacements = input_data.get("replacements")
    patch = input_data.get("patch")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if replacements is None:
        if not isinstance(patch, str) or not patch:
            raise ValueError("patch must be a string")
        replacements = [{"search": patch, "replace": ""}]
    if not isinstance(replacements, list) or not replacements:
        raise ValueError("replacements must be a non-empty list")
    normalized = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise ValueError("replacement entries must be objects")
        search = replacement.get("search")
        replace = replacement.get("replace")
        replace_all = bool(replacement.get("replaceAll", replacement.get("replace_all", False)))
        if not isinstance(search, str) or not search:
            raise ValueError("replacement search must be a non-empty string")
        if not isinstance(replace, str):
            raise ValueError("replacement replace must be a string")
        # Normalize \r\n → \n so search/replace strings always match
        # file content (read_text uses universal newlines).
        search = search.replace("\r\n", "\n")
        replace = replace.replace("\r\n", "\n")
        normalized.append({"search": search, "replace": replace, "replace_all": replace_all})
    return {"path": path, "replacements": normalized}


def _run(input_data: dict, context):
    """执行文件的文本替换操作。

    解析输入数据后，依次对目标文件应用每一个替换规则。
    支持单次替换（replaceOnce）和全局替换（replaceAll）两种模式。
    替换完成后通过 apply_reviewed_file_change 生成差异供用户审核批准。

    参数:
        input_data: 包含 path 和 replacements 的字典。
        context: 工具运行时上下文，用于权限检查和路径解析。

    返回:
        ToolResult 对象。成功时 output 包含替换摘要信息；
        失败时 ok 为 False，output 包含错误描述。
    """  # target = resolve_tool_path(context, input_data["path"], "write")
    content = load_existing_file(target)
    applied: list[str] = []
    for index, replacement in enumerate(input_data["replacements"], start=1):
        if replacement["search"] not in content:
            return ToolResult(ok=False, output=f"Replacement {index} not found in {input_data['path']}")
        replace_all = bool(replacement.get("replace_all", replacement.get("replaceAll", False)))
        if replace_all:
            content = replacement["replace"].join(content.split(replacement["search"]))
            applied.append(f"#{index} replaceAll")
        else:
            content = content.replace(replacement["search"], replacement["replace"], 1)
            applied.append(f"#{index} replaceOnce")
    result = apply_reviewed_file_change(context, input_data["path"], target, content)
    if not result.ok:
        return result
    return ToolResult(
        ok=True,
        output=f"Patched {input_data['path']} with {len(applied)} replacement(s): {', '.join(applied)}",
    )


patch_file_tool = ToolDefinition(
    name="patch_file",
    description="Apply multiple exact-text replacements to one file in a single operation.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "replacements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string"},
                        "replace": {"type": "string"},
                        "replaceAll": {"type": "boolean"},
                    },
                    "required": ["search", "replace"],
                },
            },
        },
        "required": ["path", "replacements"],
    },
    validator=_validate,
    run=_run,
)  # 