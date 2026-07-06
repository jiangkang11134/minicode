from __future__ import annotations

"""技能加载工具。

通过 SKILL.md 名称动态加载本地技能文件，
通过 ToolDefinition 注册到 MiniCode 工具系统中。
"""

from minicode.skills import load_skill
from minicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    """校验 load_skill 工具的输入参数。

    检查 name 是否为非空字符串。

    参数:
        input_data: 原始输入字典，必须包含 "name" 键

    返回:
        规范化后的字典，包含 name 键

    抛出:
        ValueError: name 缺失或为空字符串

    重要程度: """
    name = input_data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    return {"name": name.strip()}


def create_load_skill_tool(cwd: str) -> ToolDefinition:
    """创建一个用于加载本地技能文件的 ToolDefinition。

    根据当前工作目录 cwd 加载对应路径下的 SKILL.md 文件，
    返回包含技能名称、来源路径和完整内容的工具定义。

    参数:
        cwd: 当前工作目录，用于确定技能文件的搜索路径

    返回:
        ToolDefinition 实例，注册名为 "load_skill"

    重要程度: """
    def _run(input_data: dict, _context) -> ToolResult:
        """执行技能加载操作。

        根据输入的名称从本地文件系统加载对应的 SKILL.md 内容。

        参数:
            input_data: 经 _validate 校验后的输入字典
            _context: 工具运行上下文（未使用）

        返回:
            ToolResult，包含技能名称、来源路径和内容，或未找到的错误信息

        重要程度: """
        skill = load_skill(cwd, input_data["name"])
        if skill is None:
            return ToolResult(ok=False, output=f"Unknown skill: {input_data['name']}")
        return ToolResult(
            ok=True,
            output="\n".join(
                [
                    f"SKILL: {skill.name}",
                    f"SOURCE: {skill.source}",
                    f"PATH: {skill.path}",
                    "",
                    skill.content,
                ]
            ),
        )

    return ToolDefinition(
        name="load_skill",
        description="Load a local SKILL.md by name.",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        validator=_validate,
        run=_run,
    )
