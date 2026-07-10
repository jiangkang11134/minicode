"""Task 工具——启动子代理处理复杂的多步骤任务。

灵感来源于 Claude Code 的 Task 工具，该工具启动一个独立代理循环，
拥有自己的上下文窗口，与主对话隔离。

子代理运行完整的代理循环（模型 + 工具），包含：
- 针对任务类型定制的系统提示词
- 基于代理类型过滤的工具集合
- 防止无限执行的最大轮数限制
- 将结果摘要返回给父级上下文
"""
from __future__ import annotations

import os
import time

from minicode.agent_loop import run_agent_turn
from minicode.tooling import ToolDefinition, ToolResult

# ---------------------------------------------------------------------------
# Agent type definitions
# ---------------------------------------------------------------------------

AGENT_TYPES = {
    "explore": {
        "name": "Explore",
        "description": "Fast, read-only agent for codebase exploration and search",
        "system_prompt": (
            "You are an exploration agent. Your job is to quickly search and "
            "understand codebases. You should be fast and focused on finding "
            "relevant files and understanding structure. "
            "You can only use read-only tools. "
            "When done, provide a concise summary of your findings."
        ),
        "allowed_tools": {"read_file", "list_files", "grep_files", "file_tree", "find_symbols", "find_references", "get_ast_info"},
        "max_turns": 5,
    },
    "plan": {
        "name": "Plan",
        "description": "Thorough agent for gathering context and understanding code",
        "system_prompt": (
            "You are a planning agent. Your job is to thoroughly understand "
            "the codebase and task before acting. Read multiple files, trace "
            "code paths, and build a complete mental model. "
            "You can only use read-only tools. "
            "When done, provide a detailed analysis with actionable recommendations."
        ),
        "allowed_tools": {"read_file", "list_files", "grep_files", "file_tree", "find_symbols", "find_references", "get_ast_info", "code_review"},
        "max_turns": 8,
    },
    "general": {
        "name": "General",
        "description": "Full-featured agent for complex multi-step tasks",
        "system_prompt": (
            "You are a general-purpose coding agent. You can read, write, "
            "and modify code. Follow best practices and explain your changes. "
            "Break complex tasks into smaller steps. "
            "When done, provide a summary of what you did and any important findings."
        ),
        "allowed_tools": None,  # None = all tools allowed
        "max_turns": 15,
    },
    "review": {
        "name": "Review",
        "description": "Code reviewer — cross-file impact analysis and code quality, does NOT run tests",
        "system_prompt": (
            "You are a code reviewer. Your job is to analyze code changes and find issues."
            "\n\nProcess:"
            "\n1. Read the import map at .mini-code-import-map/import-map.json"
            "\n2. Find which files reference the changed symbols"
            "\n3. Read affected files and check backward compatibility"
            "\n4. Run code_review on the changed file"
            "\n5. Output a structured report with severity levels"
            "\n\nAt the end of your report, output one of these on a new line:"
            "\n  [REVIEW_RESULT: PASS] — if you found NO issues that need fixing"
            "\n  [REVIEW_RESULT: FAIL] — if you found any issues that need fixing"
            "\n\nYou can ONLY READ files. Do NOT modify anything."
            "\nDo NOT run tests — that is handled by a separate agent."
            "\nWork autonomously. Do NOT ask the user questions."
        ),
        "allowed_tools": {"read_file", "grep_files", "file_tree",
                           "find_symbols", "find_references",
                           "diff_viewer", "code_review"},
        "max_turns": 5,
    },
    "test": {
        "name": "Test",
        "description": "Test agent — runs tests in Docker sandbox (auto-triggered, no manual use needed)",
        "system_prompt": (
            "You are a test agent. Call sandbox_test to run tests."
        ),
        "allowed_tools": {"sandbox_test", "read_file"},
        "max_turns": 3,
    },
}


def _validate(input_data: dict) -> dict:
    """验证 Task 工具的输入数据。

    检查 description 和 agent_type 参数的有效性。
    description 为必填字段，须为非空字符串；
    agent_type 可选，但必须是 AGENT_TYPES 中定义的键之一。

    参数:
        input_data: 包含 description（必填）、agent_type（可选，默认 general）
                    和 prompt（可选）的字典。

    返回:
        标准化后的字典，包含 description、agent_type 和 prompt 字段。

    抛出:
        ValueError: 当 description 缺失或 agent_type 无效时。

    重要程度: """
    description = input_data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")

    agent_type = input_data.get("agent_type", "general")
    if agent_type not in AGENT_TYPES:
        valid = ", ".join(AGENT_TYPES.keys())
        raise ValueError(f"agent_type must be one of: {valid}. Got: {agent_type}")

    return {
        "description": description.strip(),
        "agent_type": agent_type,
        "prompt": input_data.get("prompt", description.strip()),
    }


def _run(input_data: dict, context) -> ToolResult:
    """执行子代理任务。

    创建独立的代理循环，包含：
    - 独立的消息历史（系统提示词 + 任务提示词）
    - 基于代理类型过滤的工具集合
    - 最大轮数限制防止无限执行
    - 将运行结果摘要返回给父级上下文

    参数:
        input_data: 包含 description、agent_type 和 prompt 的字典。
        context: 工具运行时上下文，用于获取模型配置、权限管理和工作目录。

    返回:
        ToolResult 对象。成功时 output 包含代理执行结果摘要；
        失败时 ok 为 False，output 包含错误信息。

    重要程度: """
    from minicode.model_registry import create_model_adapter
    from minicode.permissions import PermissionManager
    from minicode.tools import create_default_tool_registry

    agent_type = input_data["agent_type"]
    agent_def = AGENT_TYPES[agent_type]
    task_prompt = input_data["prompt"]

    # ── 子 Agent 独立 API 配置 ──
    # 如果 task_input 带了 sub_api_key/sub_api_base，临时覆写环境变量
    # 让子 Agent 用不同的 provider（如审查用 DeepSeek，主 Agent 用中转站）
    _restore_env = {}
    if "sub_api_key" in input_data:
        _restore_env["CUSTOM_API_KEY"] = os.environ.get("CUSTOM_API_KEY", "")
        os.environ["CUSTOM_API_KEY"] = input_data["sub_api_key"]
    if "sub_api_base" in input_data:
        _restore_env["CUSTOM_API_BASE_URL"] = os.environ.get("CUSTOM_API_BASE_URL", "")
        os.environ["CUSTOM_API_BASE_URL"] = input_data["sub_api_base"]
    if "model" in input_data:
        _restore_env["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "")
        os.environ["ANTHROPIC_MODEL"] = input_data["model"]

    # Try to get the model from context or fall back to creating one
    # The context object carries runtime info needed for the model adapter
    runtime = None
    model = None

    # Attempt to extract runtime from the ToolContext
    if hasattr(context, '_runtime') and context._runtime:
        runtime = context._runtime

    if not runtime:
        # Try loading from config
        try:
            from minicode.config import load_runtime_config
            runtime = load_runtime_config(context.cwd)
        except Exception:
            pass

    if not runtime:
        return ToolResult(
            ok=False,
            output="Cannot run sub-agent: no model configuration available. Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL."
        )

    # Create a filtered tool registry for this agent type
    full_tools = create_default_tool_registry(context.cwd, runtime=runtime)
    allowed = agent_def["allowed_tools"]

    if allowed is not None:
        filtered_tools = [t for t in full_tools.list() if t.name in allowed]
        from minicode.tooling import ToolRegistry
        tools = ToolRegistry(filtered_tools)
    else:
        tools = full_tools

    # Create model adapter
    model = create_model_adapter(
        model=runtime.get("model", ""),
        tools=tools,
        runtime=runtime,
    )

    # Create isolated permissions (no prompts — auto-deny writes for read-only agents)
    if agent_def["allowed_tools"] is not None:
        # Read-only agent: create permission manager that denies writes
        sub_permissions = PermissionManager(context.cwd, prompt=None)
    else:
        # General agent: inherit parent's permission prompt handler
        sub_permissions = PermissionManager(context.cwd, prompt=getattr(context.permissions, 'prompt', None))

    # Build isolated message list
    sub_messages = [
        {
            "role": "system",
            "content": agent_def["system_prompt"]
            + f"\n\nCurrent cwd: {context.cwd}"
            + "\n\nIMPORTANT: When you have completed your task, end with <final> and provide your findings."
            + " Do not ask the user questions — work autonomously with the tools available."
            + " Be concise and focused."
        },
        {
            "role": "user",
            "content": task_prompt,
        },
    ]

    # Run the sub-agent loop
    start_time = time.time()
    max_turns = agent_def["max_turns"]

    try:
        try:
            result_messages = run_agent_turn(
                model=model,
                tools=tools,
                messages=sub_messages,
                cwd=context.cwd,
                permissions=sub_permissions,
                max_steps=max_turns,
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                output=f"Sub-agent ({agent_def['name']}) failed: {type(e).__name__}: {e}"
            )
    finally:
        # 恢复子 Agent 覆写的环境变量
        for k, v in _restore_env.items():
            os.environ[k] = v

    elapsed = time.time() - start_time

    # Extract the final assistant message as the result
    final_message = None
    for msg in reversed(result_messages):
        if msg.get("role") == "assistant" and msg.get("content", "").strip():
            final_message = msg["content"]
            break

    if not final_message:
        final_message = "(sub-agent completed without a final message)"

    # Build summary
    tool_calls_count = sum(1 for m in result_messages if m.get("role") == "assistant_tool_call")
    user_messages_count = sum(1 for m in result_messages if m.get("role") == "user")

    header = (
        f"[Sub-agent {agent_def['name']} completed]\n"
        f"  Type: {agent_type}\n"
        f"  Turns: {user_messages_count} (tool calls: {tool_calls_count})\n"
        f"  Duration: {elapsed:.1f}s\n"
        f"  Max turns: {max_turns}\n"
    )

    # Truncate very long results
    result_text = final_message
    MAX_RESULT_LEN = 8000
    if len(result_text) > MAX_RESULT_LEN:
        result_text = result_text[:MAX_RESULT_LEN] + f"\n\n... (truncated, {len(final_message)} chars total)"

    return ToolResult(ok=True, output=header + "\n" + result_text)


task_tool = ToolDefinition(
    name="task",
    description=(
        "Launch a sub-agent to handle a complex task autonomously. "
        "The sub-agent runs in its own isolated context with a turn limit. "
        "Use 'explore' for fast read-only codebase exploration, "
        "'plan' for thorough analysis, or 'general' for full-featured multi-step work. "
        "The sub-agent's final result is returned to you."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short 3-5 word description of the task",
            },
            "prompt": {
                "type": "string",
                "description": "Full task description for the sub-agent. If not provided, uses 'description'.",
            },
            "agent_type": {
                "type": "string",
                "enum": ["explore", "plan", "general", "review", "test"],
                "description": "Type of sub-agent: 'explore' (fast, read-only), 'plan' (thorough, read-only), 'general' (full tools, default), 'review' (code analysis, read-only), 'test' (sandboxed test execution)",
            },
        },
        "required": ["description"],
    },
    validator=_validate,
    run=_run,
)
