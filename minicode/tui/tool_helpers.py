"""TUI 工具辅助函数。

提供工具调用结果的摘要生成、会话统计、转录保存等功能，
用于在 TUI 中展示工具调用信息。
"""

from __future__ import annotations

from typing import Any

from minicode.permissions import PermissionManager
from minicode.tooling import ToolContext
from minicode.tui.types import TranscriptEntry
from minicode.workspace import resolve_tool_path


def _get_session_stats(args: Any, state: Any) -> dict[str, int]:
    """返回会话的统计信息，用于横幅/底部状态栏展示。

    参数:
        args: 应用参数对象，包含 messages 和 tools 等属性
        state: 当前会话状态对象

    返回:
        dict[str, int]: 包含 transcriptCount、messageCount、skillCount、mcpCount 的字典
    """  # return {
        "transcriptCount": len(state.transcript),
        "messageCount": len(args.messages),
        "skillCount": len(args.tools.get_skills()),
        "mcpCount": len(args.tools.get_mcp_servers()),
    }


def _truncate_for_display(text: str, max_len: int = 180) -> str:
    """将过长的文本截断，用于在 TUI 中显示。

    如果文本长度超过 max_len，则在截断处添加 "..."。

    参数:
        text: 原始文本
        max_len: 最大显示长度，默认为 180

    返回:
        str: 截断后的文本
    """  # return text[:max_len] + "..." if len(text) > max_len else text


def _summarize_collapsed_tool_body(output: str) -> str:
    """生成工具调用输出内容的摘要，用于折叠状态展示。

    对 diff 格式的输出会统计增删行数；对普通输出取第一行非空内容。

    参数:
        output: 工具调用的原始输出文本

    返回:
        str: 摘要字符串，格式为 "+N -M"（diff 格式）或截断后的第一行文本
    """  # # Diff-aware summary: count additions and deletions
    if output.startswith("@@") or "\n@@" in output[:200] or output.startswith("diff "):
        additions = output.count("\n+") - output.count("\n+++")
        deletions = output.count("\n-") - output.count("\n---")
        if additions > 0 or deletions > 0:
            return f"+{additions} -{deletions}"
    line = next((part.strip() for part in output.split("\n") if part.strip()), "output collapsed")
    return line[:140] + "..." if len(line) > 140 else line


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """生成工具调用输入的摘要，用于在界面上展示。

    根据工具名称和输入参数的不同类型，生成可读性高的短摘要。
    对字典类型的参数会提取关键字段（如 path、command、replacements 等）。

    参数:
        tool_name: 工具名称
        tool_input: 工具输入参数，可以是字符串或字典

    返回:
        str: 工具输入的可读摘要
    """  # if isinstance(tool_input, str):
        return _truncate_for_display(" ".join(tool_input.split()).strip())

    if isinstance(tool_input, dict):
        path = str(tool_input.get("path", "")).strip()
        path_part = f" path={path}" if path else ""

        if tool_name == "patch_file":
            replacements = tool_input.get("replacements")
            count = len(replacements) if isinstance(replacements, list) else 0
            return f"patch_file{path_part} replacements={count}"
        if tool_name == "edit_file":
            return f"edit_file{path_part}"
        if tool_name == "read_file":
            extras: list[str] = []
            if tool_input.get("offset") is not None:
                extras.append(f"offset={tool_input['offset']}")
            if tool_input.get("limit") is not None:
                extras.append(f"limit={tool_input['limit']}")
            return f"read_file{path_part}{' ' + ' '.join(extras) if extras else ''}"
        if tool_name == "run_command":
            cmd = str(tool_input.get("command", "")).strip()
            return f"run_command{' ' + _truncate_for_display(cmd, 120) if cmd else ''}"
        if path:
            return f"{tool_name}{path_part}"

    try:
        return _truncate_for_display(str(tool_input))
    except Exception:
        return _truncate_for_display(repr(tool_input))


def _is_file_edit_tool(tool_name: str) -> bool:
    """判断工具名称是否为文件编辑类工具。

    参数:
        tool_name: 工具名称

    返回:
        bool: 如果是 edit_file、patch_file、modify_file 或 write_file 则返回 True
    """  # return tool_name in ("edit_file", "patch_file", "modify_file", "write_file")


def _extract_path_from_tool_input(tool_input: Any) -> str | None:
    """从工具输入参数中提取文件路径。

    参数:
        tool_input: 工具输入参数

    返回:
        str | None: 如果存在有效的 path 字段则返回该路径，否则返回 None
    """  # if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("path")
    return value if isinstance(value, str) and value.strip() else None


def _apply_tool_result_visual_state(
    entry: TranscriptEntry,
    tool_name: str,
    output: str,
    is_error: bool,
) -> None:
    """为工具调用结果条目应用一致的视觉状态。

    根据是否错误设置状态、折叠信息和折叠阶段。错误状态下展开显示，
    成功状态下自动折叠并生成摘要。

    参数:
        entry: 要更新的 TranscriptEntry 实例
        tool_name: 工具名称（当前保留供扩展使用）
        output: 工具调用的输出文本
        is_error: 是否为错误结果
    """  # entry.status = "error" if is_error else "success"
    entry.body = f"ERROR: {output}" if is_error else output
    if is_error:
        entry.collapsed = False
        entry.collapsedSummary = None
        entry.collapsePhase = None
    else:
        entry.collapsed = True
        entry.collapsedSummary = _summarize_collapsed_tool_body(output)
        entry.collapsePhase = 3


def _mark_unfinished_tools(state_obj: Any) -> int:
    """标记尚未完成的工具调用条目为错误状态，并清理相关状态。

    遍历所有 transcript 条目，将状态为 "running" 的工具条目标记为 "error"，
    并添加错误说明。同时清空 pending_tool_runs 和 active_tool。

    参数:
        state_obj: 会话状态对象

    返回:
        int: 被标记为错误的工具条目数量
    """  # count = 0
    for entry in state_obj.transcript:
        if entry.kind == "tool" and entry.status == "running":
            entry.status = "error"
            entry.body = (
                f"{entry.body}\n\n"
                "ERROR: Tool did not report a final result before the turn ended. "
                "This usually means the command kept running in the background "
                "or the tool lifecycle got out of sync."
            )
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            state_obj.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
            count += 1
    if hasattr(state_obj, "pending_tool_runs"):
        state_obj.pending_tool_runs = {}
    state_obj.active_tool = None
    return count


def _save_transcript(
    state_obj: Any,
    cwd: str,
    permissions: PermissionManager,
    output_path: str,
) -> str:
    """将 transcript 条目保存到指定路径的文件中。

    解析输出路径后，格式化 transcript 内容并写入文件。

    参数:
        state_obj: 会话状态对象，包含 transcript 属性
        cwd: 当前工作目录
        permissions: 权限管理器，用于路径解析
        output_path: 输出文件路径

    返回:
        str: 实际写入的文件绝对路径
    """  # from minicode.tui.transcript import format_transcript_text

    target = resolve_tool_path(
        ToolContext(cwd=cwd, permissions=permissions),
        output_path,
        "write",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_transcript_text(state_obj.transcript), encoding="utf-8")
    return str(target)
