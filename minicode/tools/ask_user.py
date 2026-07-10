"""用户询问工具。

提供一个简单的工具定义，用于暂停当前执行流程并向用户提出澄清性问题。
工具返回 awaitUser=True 标志，框架在收到回复后继续执行。

依赖:
    minicode.tooling: ToolDefinition、ToolResult
"""

from __future__ import annotations

from minicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    """验证 ask_user 工具的输入参数。

    检查 question 是否为非空字符串，去除首尾空格后返回。

    参数:
        input_data: 原始输入字典，包含 "question" 键。

    返回:
        清洗后的字典，包含 stripped 后的 question。

    抛出:
        ValueError: 如果 question 缺失或为空。
    """
    question = input_data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    return {"question": question.strip()}


def _run(input_data: dict, _context) -> ToolResult:
    """执行用户询问逻辑。

    将问题内容直接作为输出返回，同时设置 awaitUser=True
    以告知框架暂停并等待用户回复。

    参数:
        input_data: 已验证的输入参数，含 "question"。
        _context: 工具执行上下文（本工具未使用）。

    返回:
        ToolResult: 始终返回 ok=True，并将 awaitUser 设为 True。
    """
    return ToolResult(ok=True, output=input_data["question"], awaitUser=True)


ask_user_tool = ToolDefinition(
    name="ask_user",
    description="Pause the turn and ask the user a clarifying question.",
    input_schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    validator=_validate,
    run=_run,
)  #
