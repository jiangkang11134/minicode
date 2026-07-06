"""MiniCode Headless Runner --- 非交互式一次性执行。 灵感来源于 Hermes Agent 的 headless 模式，适用于 CI/CD 流水线和
自动化工作流。

用法:
  # 运行单个提示并退出
  python -m minicode.headless "帮我分析这个项目的结构"

  # 管道输入
  echo "解释这段代码" | python -m minicode.headless

  # 在 Docker 中
  docker compose run --rm headless "修复这个 bug"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _write_headless_messages_trace(
    trace_path: str | None,
    *,
    cwd: str,
    prompt: str,
    runtime: dict | None,
    result_messages: list[dict] | None,
    response_text: str | None,
    error_text: str | None = None,
) -> None:
    """将对话消息追踪写入 JSON 文件。

    参数:
        trace_path: 输出文件路径。若为 None 或空字符串则跳过写入。
        cwd: 当前工作目录。
        prompt: 用户输入的提示词。
        runtime: 运行时配置字典。
        result_messages: 对话消息列表。
        response_text: 助手的响应文本。
        error_text: 错误信息文本（可选）。

    返回:
        None
    """
    if not trace_path:
        return
    payload = {
        "cwd": cwd,
        "prompt": prompt,
        "model": (runtime or {}).get("model"),
        "messages": result_messages or [],
        "assistant_response": response_text,
        "error": error_text,
    }
    path = Path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _allow_edits_requested(cli_flag: bool = False) -> bool:
    """判断 headless 模式是否应自动批准编辑、命令和超出工作目录的访问。

    选择加入的非交互式 CI 模式。通过 --allow-edits 标志或
    MINI_CODE_ALLOW_EDITS 环境变量（1/true/yes/on）控制。

    参数:
        cli_flag: 命令行标志是否设置。

    返回:
        如果应自动批准编辑，返回 True；否则返回 False。
    """
    if cli_flag:
        return True
    return os.getenv("MINI_CODE_ALLOW_EDITS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _make_auto_approve_prompt():
    """构建一个非交互式权限提示处理器，自动批准当前运行的所有请求。

    决定是会话范围的（不持久化到权限存储）：编辑通过 allow_all_turn，
    路径/命令通过 allow_once。

    返回:
        自动批准处理函数。
    """

    def _auto_approve(request: dict) -> dict:
        """自动批准单个权限请求。

        参数:
            request: 权限请求字典，包含请求的 kind 等信息。

        返回:
            包含批准决策的字典。编辑请求返回 allow_all_turn，
            其他请求返回 allow_once。
        """
        if request.get("kind") == "edit":
            return {"decision": "allow_all_turn"}
        return {"decision": "allow_once"}

    return _auto_approve


def run_headless(prompt: str | None = None, allow_edits: bool = False) -> str:
    """以 headless 模式运行单个 agent 轮次并返回响应。

    参数:
        prompt: 要发送的用户消息。如果为 None，则从标准输入读取。
        allow_edits: 如果为 True（或通过 MINI_CODE_ALLOW_EDITS 环境变量），
            自动批准文件编辑、命令和超出工作目录的访问。
            对于 headless 修改文件是必需的（编辑通常需要 TTY 批准）。

    返回:
        助手的响应文本。
    """
    from minicode.agent_loop_lite import run_agent_turn
    from minicode.config import load_runtime_config
    from minicode.memory import MemoryManager
    from minicode.model_registry import create_model_adapter
    from minicode.permissions import PermissionManager
    from minicode.prompt import build_system_prompt
    from minicode.tools import create_default_tool_registry
    from minicode.logging_config import setup_logging, get_logger, structured_logging_requested

    setup_logging(
        level=os.environ.get("MINI_CODE_LOG_LEVEL", "WARNING"),
        structured=structured_logging_requested(),
    )
    logger = get_logger("headless")

    # Read prompt from stdin if not provided
    if prompt is None:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            print("Usage: python -m minicode.headless <prompt>", file=sys.stderr)
            sys.exit(1)

    if not prompt:
        print("Error: empty prompt", file=sys.stderr)
        sys.exit(1)

    cwd = str(Path.cwd())

    # Load config
    try:
        runtime = load_runtime_config(cwd)
    except Exception as exc:  # noqa: BLE001
        # Persist the failure to the log file (issue #5), not just stderr.
        logger.error("Config load failed: %s", exc, exc_info=True)
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Initialize components
    tools = create_default_tool_registry(cwd, runtime=runtime)
    auto_approve = _allow_edits_requested(cli_flag=allow_edits)
    if auto_approve:
        logger.warning(
            "Headless --allow-edits is active: file edits, commands, and "
            "out-of-cwd access will be auto-approved for this run "
            "(non-interactive CI mode; approvals are session-scoped)."
        )
    permissions = PermissionManager(cwd, prompt=_make_auto_approve_prompt() if auto_approve else None)
    memory_mgr = MemoryManager(project_root=Path(cwd))

    model = create_model_adapter(
        model=runtime.get("model", ""),
        tools=tools,
        runtime=runtime,
    )

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                cwd,
                permissions.get_summary(),
                {
                    "skills": tools.get_skills(),
                    "mcpServers": tools.get_mcp_servers(),
                    "memory_context": memory_mgr.get_relevant_context(),
                },
            ),
        },
        {"role": "user", "content": prompt},
    ]

    logger.info("Headless run: %s", prompt[:80])
    trace_output_path = os.environ.get("MINI_CODE_HEADLESS_MESSAGES_OUT", "").strip() or None

    try:
        result_messages = run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            permissions=permissions,
            runtime=runtime,
        )

        # Extract last assistant message
        last_assistant = next(
            (m for m in reversed(result_messages) if m["role"] == "assistant"),
            None,
        )
        response_text = last_assistant["content"] if last_assistant else "(no response)"
        _write_headless_messages_trace(
            trace_output_path,
            cwd=cwd,
            prompt=prompt,
            runtime=runtime,
            result_messages=result_messages,
            response_text=response_text,
        )
        return response_text

    except Exception as exc:  # noqa: BLE001
        logger.error("Headless error: %s", exc)
        response_text = f"Error: {exc}"
        _write_headless_messages_trace(
            trace_output_path,
            cwd=cwd,
            prompt=prompt,
            runtime=runtime,
            result_messages=[],
            response_text=response_text,
            error_text=str(exc),
        )
        return response_text
    finally:
        try:
            tools.dispose()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    """Headless 模式的 CLI 入口点。

    解析命令行参数，提取 --allow-edits 标志，将剩余参数拼接为提示词，
    调用 run_headless 执行并打印结果。

    返回:
        None
    """
    # Strip the --allow-edits flag (handled separately); everything else is the prompt.
    allow_edits = "--allow-edits" in sys.argv
    prompt_args = [arg for arg in sys.argv[1:] if arg != "--allow-edits"]
    prompt = " ".join(prompt_args) if prompt_args else None
    response = run_headless(prompt, allow_edits=allow_edits)
    print(response)


if __name__ == "__main__":
    main()
