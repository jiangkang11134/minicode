"""
MiniCode Python 主入口模块。

这是整个应用的 CLI 入口。用户敲 minicode-py 时第一个执行的模块。
核心职责：解析参数 → 初始化所有子系统 → 分发到 TTY 或非交互模式。

使用方式:
    minicode-py                           # 启动 TUI 交互模式
    minicode-py --session <id>            # 指定会话 ID 启动
    minicode-py --rewind latest           # 回退最新会话的文件编辑
    minicode-py --list-sessions           # 列出所有历史会话
    echo "explain this" | minicode-py     # 非交互管道模式
"""

from __future__ import annotations

import argparse
import sys
import os
from pathlib import Path

from minicode.agent_loop_lite import run_agent_turn
from minicode.cli_commands import try_handle_local_command
from minicode.config import load_runtime_config
from minicode.history import load_history_entries, save_history_entries
from minicode.local_tool_shortcuts import parse_local_tool_shortcut
from minicode.manage_cli import maybe_handle_management_command
from minicode.model_registry import create_model_adapter
from minicode.permissions import PermissionManager
from minicode.prompt import build_system_prompt_bundle
from minicode.session import (
    format_rewind_preview,
    format_session_checkpoints,
    format_session_inspect,
    format_session_replay,
    format_session_resume,
    get_latest_session,
    load_session,
    rewind_session,
)
from minicode.tools import create_default_tool_registry
from minicode.tooling import ToolContext
from minicode.tui.transcript import format_transcript_text
from minicode.tui.types import TranscriptEntry
from minicode.tty_app import run_tty_app
from minicode.workspace import resolve_tool_path


def _handle_local_command(user_input: str, tools) -> str | None:
    """处理本地命令（如 /tools、/help、/skills 等不涉及 LLM 调用的命令）。

    优先匹配 /tools 特殊处理（列出所有可用工具），其余委托给 cli_commands 模块。

    参数:
        user_input: 用户原始输入字符串
        tools: ToolRegistry 实例，包含所有已注册工具

    返回:
        命令输出文本，如果非本地命令则返回 None
    """
    if user_input == "/tools":
        return "\n".join(f"{tool.name}: {tool.description}" for tool in tools.list())
    local_result = try_handle_local_command(user_input, tools=tools, cwd=str(Path.cwd()))
    return local_result


def _render_banner(runtime: dict | None, cwd: str, permission_summary: list[str], counts: dict[str, int]) -> str:
    """渲染启动时的 ASCII 艺术横幅，展示运行时关键信息。

    显示内容：模型名称、当前工作目录、权限摘要、Skills/MCP/Transcript 计数。
    只展示前 2 个权限摘要行，避免占用过多终端空间。

    参数:
        runtime: 运行时配置字典（含 model 等字段），None 时显示 "unconfigured"
        cwd: 当前工作目录路径
        permission_summary: 权限摘要字符串列表
        counts: 统计数据字典，包含 skillCount、mcpCount、transcriptCount

    返回:
        格式化后的横幅文本
    """
    model = runtime["model"] if runtime else "unconfigured"
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║  🤖 MiniCode Python - Your Terminal Coding Assistant    ║",
        "╠══════════════════════════════════════════════════════════╣",
        f"║  Model: {model:<46} ║",
        f"║  CWD: {cwd:<50} ║",
    ]
    if permission_summary:
        for perm in permission_summary[:2]:  # 只显示前2个权限摘要
            lines.append(f"║  {perm:<60} ║")
    lines.append("╠══════════════════════════════════════════════════════════╣")
    lines.append(
        f"║  📊 Skills: {counts['skillCount']:>2} | MCP Servers: {counts['mcpCount']:>2} | "
        f"Transcript: {counts['transcriptCount']:>3} ║"
    )
    lines.append("╚══════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def _render_quick_start() -> str:
    """渲染快速入门指南，列出常用工具和示例提示语。

    在每次启动时（非 TTY 模式或 MINI_CODE_SHOW_GUIDE=1）展示给用户，
    帮助新用户快速了解可用命令和典型使用场景。

    返回:
        格式化后的快速入门文本
    """
    return """
💡 Quick Start Guide:
  📝 Edit files:     edit_file.py or patch_file.py
  🔍 Search code:    /grep <pattern> or grep_files tool
  🏃 Run commands:   /cmd <command> or run_command tool
  🧠 Think deeply:   Use sequential_thinking MCP tool
  📚 View skills:    /skills
  ❓ Get help:       /help

🚀 Try saying:
  "帮我分析这个项目的结构"
  "用 TDD 方式实现 XX 功能"
  "系统性地调试这个 bug"
  "帮我写个技术方案"
"""


def _append_transcript(transcript: list[TranscriptEntry], **kwargs) -> None:
    """向会话转录列表追加一条记录。

    自动递增 ID，支持灵活的关键字参数构造 TranscriptEntry。
    转录条目用于 /session-replay 和 /transcript-save 等功能。

    参数:
        transcript: 转录条目列表
        **kwargs: TranscriptEntry 的字段，如 kind、body、toolName、status 等
    """
    transcript.append(TranscriptEntry(id=len(transcript) + 1, **kwargs))


def _make_cli_permission_prompt():
    """创建基于 CLI 文本交互的权限审批回调函数。

    当标准输入是 TTY 但 TUI 未启动时（如管道模式），用简单的 print/input
    替代图形化审批界面。支持预定义的选项列表和通用 y/n 两种模式。

    返回:
        一个可调用对象，接收 permission request dict，返回审批决策 dict。
        决策格式: {"decision": "allow_once" | "deny_once"}
    """
    def _prompt(request: dict) -> dict:
        print(f"\n{request.get('summary', 'Permission Request')}")
        choices = request.get("choices", [])
        if choices:
            for choice in choices:
                print(f"  [{choice.get('key', '')}] {choice.get('label', '')}")
            answer = input("Choose: ").strip()
            for choice in choices:
                if answer == choice.get("key"):
                    return {"decision": choice.get("decision", "allow_once")}
        answer = input("Allow? (y/n): ").strip().lower()
        return {"decision": "allow_once" if answer in ("y", "yes") else "deny_once"}
    return _prompt


def _configure_stdio_for_unicode() -> None:
    """配置标准 I/O 流的编码为 UTF-8，确保多语言文本正常显示。

    解决三个问题：
    1. PowerShell 管道可能向 stdin 注入 UTF-8 BOM（\xef\xbb\xbf），
       用 utf-8-sig 解码自动过滤 BOM，防止 "/memory" 变成 "锘?/memory"
    2. stdout 和 stderr 统一使用 UTF-8 编码，支持 CJK 字符
    3. errors='replace' 确保解码失败时不崩溃
    """
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        try:
            # PowerShell pipelines may prefix UTF-8 BOM bytes on stdin. Decode
            # with utf-8-sig so local slash commands are not polluted by BOM
            # artifacts like "锘?/memory" and accidentally routed to the model.
            stdin_reconfigure(encoding="utf-8-sig", errors="replace")
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _save_transcript_file(cwd: str, permissions, transcript: list[TranscriptEntry], output_path: str) -> str:
    """将会话转录内容保存到指定文件。

    使用 resolve_tool_path 做路径安全解析（确保不越界），自动创建父目录。

    参数:
        cwd: 当前工作目录
        permissions: PermissionManager 实例，用于路径权限检查
        transcript: 转录条目列表
        output_path: 目标文件路径（可以是相对路径）

    返回:
        保存后的绝对路径字符串
    """
    target = resolve_tool_path(ToolContext(cwd=cwd, permissions=permissions), output_path, "write")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_transcript_text(transcript), encoding="utf-8")
    return str(target)


def _resolve_target_session(cwd: str, session_id: str | None):
    """将 session_id 参数解析为实际的 Session 对象。

    session_id 为 None / "" / "latest" 时自动加载当前工作区的最新会话，
    否则按指定 ID 加载。统一了 --rewind、--inspect-session 等命令的会话查找逻辑。

    参数:
        cwd: 当前工作目录，用于确定工作区范围
        session_id: 会话 ID 或 "latest" / None

    返回:
        Session 对象，或 None（未找到会话）
    """
    workspace = str(Path(cwd).resolve())
    return (
        get_latest_session(workspace=workspace)
        if session_id in (None, "", "latest")
        else load_session(session_id)
    )


def _handle_list_checkpoints_request(cwd: str, session_id: str | None) -> int:
    """处理 --list-checkpoints 参数：列出指定会话的所有 checkpoint。

    参数:
        cwd: 当前工作目录
        session_id: 会话 ID 或 None（使用最新会话）

    返回:
        退出码（0=成功, 1=未找到会话）
    """
    target_session = _resolve_target_session(cwd, session_id)
    if target_session is None:
        print("No saved session found for checkpoint inspection.", file=sys.stderr)
        return 1

    print(format_session_checkpoints(target_session))
    return 0


def _handle_inspect_session_request(cwd: str, session_id: str | None) -> int:
    """处理 --inspect-session 参数：展示会话的运行时摘要、checkpoint 统计和转录概览。

    参数:
        cwd: 当前工作目录
        session_id: 会话 ID 或 None（使用最新会话）

    返回:
        退出码（0=成功, 1=未找到会话）
    """
    target_session = _resolve_target_session(cwd, session_id)
    if target_session is None:
        print("No saved session found for inspection.", file=sys.stderr)
        return 1

    print(format_session_inspect(target_session))
    return 0


def _handle_replay_session_request(cwd: str, session_id: str | None) -> int:
    """处理 --replay-session 参数：回放会话的完整过程，包含 checkpoint、提示历史和转录时间线。

    参数:
        cwd: 当前工作目录
        session_id: 会话 ID 或 None（使用最新会话）

    返回:
        退出码（0=成功, 1=未找到会话）
    """
    target_session = _resolve_target_session(cwd, session_id)
    if target_session is None:
        print("No saved session found for replay.", file=sys.stderr)
        return 1

    print(format_session_replay(target_session))
    return 0


def _handle_rewind_request(
    cwd: str,
    session_id: str | None,
    steps: int,
    checkpoint_id: str | None,
) -> int:
    """处理 --rewind 参数：将会话的 checkpoint 回退到指定步骤或 checkpoint ID。

    回退后打印恢复的 checkpoint 清单和会话恢复提示。
    支持按步数回退（--rewind-steps N）和按 checkpoint ID 回退（--rewind-to ID）两种方式。

    参数:
        cwd: 当前工作目录
        session_id: 会话 ID 或 None（使用最新会话）
        steps: 回退的 checkpoint 步数（不指定 checkpoint_id 时生效）
        checkpoint_id: 回退到指定 checkpoint ID（优先级高于 steps）

    返回:
        退出码（0=成功, 1=未找到会话或无可用 checkpoint）
    """
    target_session = _resolve_target_session(cwd, session_id)
    if target_session is None:
        print("No saved session found to rewind.", file=sys.stderr)
        return 1

    session, restored = rewind_session(
        target_session.session_id,
        steps=steps,
        checkpoint_id=checkpoint_id,
    )
    if session is None or not restored:
        print("No checkpoints available to rewind for that session.", file=sys.stderr)
        return 1

    if checkpoint_id:
        print(
            f"Rewound {len(restored)} checkpoint(s) through {checkpoint_id[:8]} "
            f"for session {session.session_id[:8]}."
        )
    else:
        print(f"Rewound {len(restored)} checkpoint(s) for session {session.session_id[:8]}.")
    for checkpoint in restored:
        print(f"  - [{checkpoint.checkpoint_id[:8]}] {checkpoint.file_path}")
    print(format_session_resume(session))
    return 0


def _handle_preview_rewind_request(
    cwd: str,
    session_id: str | None,
    steps: int,
    checkpoint_id: str | None,
) -> int:
    """处理 --preview-rewind 参数：预览回退效果而不实际执行文件恢复。

    展示如果执行回退会恢复哪些文件、恢复成什么内容，让用户在真正 rewind 前
    确认操作的正确性（安全措施）。

    参数:
        cwd: 当前工作目录
        session_id: 会话 ID 或 None（使用最新会话）
        steps: 预览回退的 checkpoint 步数
        checkpoint_id: 预览回退到指定 checkpoint ID

    返回:
        退出码（0=成功, 1=未找到会话或无 checkpoint）
    """
    target_session = _resolve_target_session(cwd, session_id)
    if target_session is None:
        print("No saved session found to preview.", file=sys.stderr)
        return 1

    preview = format_rewind_preview(
        target_session,
        steps=steps,
        checkpoint_id=checkpoint_id,
    )
    if preview.startswith("No checkpoints available"):
        print(preview, file=sys.stderr)
        return 1
    print(preview)
    return 0

def main() -> None:
    """MiniCode Python 主入口函数。

    【为什么需要】作为整个应用的 CLI 入口点，main() 统一管理初始化、路由和
    资源释放的生命周期，确保所有子系统的正确启动顺序和优雅关闭。

    ╔══════════════════ 完整执行流程 ══════════════════╗
    ║                                                      ║
    ║  ┌─ 第1步: 编码初始化 ──────────────────────────┐   ║
    ║  │  _configure_stdio_for_unicode()                 │   ║
    ║  │  → stdin:  utf-8-sig (过滤 PowerShell BOM)     │   ║
    ║  │  → stdout: utf-8, errors=replace               │   ║
    ║  │  → stderr: utf-8, errors=replace               │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 第2步: 参数解析 ───────────────────────────┐   ║
    ║  │  argparse.ArgumentParser() 解析命令行            │   ║
    ║  │  → args, remaining_argv                        │   ║
    ║  │  → setup_logging(args.log_level)               │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 第3步: 路由到纯 CLI 命令 ────────────────┐   ║
    ║  │  --validate-config  → format_config_diag()       │   ║
    ║  │  --install          → install_main()             │   ║
    ║  │  --list-checkpoints → _handle_list_checkpoints() │   ║
    ║  │  --inspect-session  → _handle_inspect_session()  │   ║
    ║  │  --replay-session   → _handle_replay_session()   │   ║
    ║  │  --rewind           → _handle_rewind_request()   │   ║
    ║  │  --preview-rewind   → _handle_preview_rewind()   │   ║
    ║  │  裸参数             → maybe_handle_management()  │   ║
    ║  │  (以上均 return / raise SystemExit)              │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 第4步: 初始化所有子系统 ─────────────────┐   ║
    ║  │  ① load_runtime_config()                       │   ║
    ║  │     └─ 失败 → 打印修复指引, fallback mock     │   ║
    ║  │  ② create_default_tool_registry(cwd, runtime)  │   ║
    ║  │  ③ PermissionManager(cwd, prompt)              │   ║
    ║  │  ④ create_model_adapter(model, tools, runtime) │   ║
    ║  │  ⑤ ContextManager(model)    → 上下文窗口管理   │   ║
    ║  │  ⑥ MemoryManager(project)   → 跨会话记忆      │   ║
    ║  │  ⑦ UserProfileManager(cwd)  → 用户画像        │   ║
    ║  │  ⑧ create_app_store()       → 全局状态机      │   ║
    ║  │  ⑨ build_system_prompt_bundle() → 系统提示包   │   ║
    ║  │  ⑩ load_history_entries() + transcript: []    │   ║
    ║  │  ⑪ _render_banner() + _render_quick_start()   │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 第5步: 分叉到 TTY 或管道模式 ───────────┐   ║
    ║  │  sys.stdin.isatty()?                              │   ║
    ║  │  ├─ False → 管道模式 (逐行读取 stdin):            │   ║
    ║  │  │  ├─ /exit              → break 退出           │   ║
    ║  │  │  ├─ /transcript-save   → 保存转录文件         │   ║
    ║  │  │  ├─ memory 命令       → MemoryManager 处理    │   ║
    ║  │  │  ├─ 本地命令 (/\w+)   → _handle_local_command │   ║
    ║  │  │  ├─ 快捷工具          → parse_local_tool_... │   ║
    ║  │  │  └─ LLM Agent         → run_agent_turn()      │   ║
    ║  │  └─ True  → TTY 模式:                            │   ║
    ║  │     run_tty_app()                                │   ║
    ║  │     → 全屏 TUI (alt-screen, raw 输入)            │   ║
    ║  │     → 会话管理 / 节流渲染 / 转录展示             │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 第6步 (finally): 清理资源 ─────────────┐   ║
    ║  │  tools.dispose() → 关闭 MCP 连接          │   ║
    ║  │  日志记录 shutdown 完成                   │   ║
    ║  └──────────────────────────────────────────┘   ║
    ╚══════════════════════════════════════════════════╝
    """
    _configure_stdio_for_unicode()

    parser = argparse.ArgumentParser(
        description="MiniCode Python - A lightweight terminal coding assistant",
        add_help=True,
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Resume a previous session (use 'latest' or session ID)",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List all saved sessions and exit",
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="Start with a specific session ID",
    )
    parser.add_argument(
        "--rewind",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Rewind the latest checkpointed file edit for a saved session",
    )
    parser.add_argument(
        "--preview-rewind",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Preview the latest checkpointed file edit that would be rewound for a saved session",
    )
    parser.add_argument(
        "--list-checkpoints",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="List saved rewind checkpoints for a session",
    )
    parser.add_argument(
        "--inspect-session",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Inspect a saved session with runtime, checkpoint, and transcript summary",
    )
    parser.add_argument(
        "--replay-session",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Replay a saved session with checkpoint, prompt history, and transcript timeline",
    )
    parser.add_argument(
        "--rewind-steps",
        type=int,
        default=1,
        metavar="N",
        help="Number of checkpoints to rewind when used with --rewind (default: 1)",
    )
    parser.add_argument(
        "--rewind-to",
        default=None,
        metavar="CHECKPOINT_ID",
        help="Rewind back through a specific checkpoint ID instead of using --rewind-steps",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Run the interactive installer",
    )
    parser.add_argument(
        "--validate-config",
        "--valid-config",
        action="store_true",
        help="Validate configuration and exit",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (default: WARNING)",
    )
    parser.add_argument(
        "--structured-logs",
        action="store_true",
        help="Emit JSON structured logs (also enabled via MINI_CODE_LOG_STRUCTURED=true)",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="Load project-level .mcp.json (disabled by default for security; also via MINI_CODE_TRUST_PROJECT_MCP=1)",
    )

    args, remaining_argv = parser.parse_known_args()
    if remaining_argv and not any(not arg.startswith("--") for arg in remaining_argv):
        parser.error(f"unrecognized arguments: {' '.join(remaining_argv)}")

    # Initialize logging
    from minicode.logging_config import setup_logging, structured_logging_requested
    setup_logging(
        level=args.log_level,
        structured=structured_logging_requested(cli_flag=args.structured_logs),
    )

    # Run config validation if requested
    if args.validate_config:
        from minicode.config import format_config_diagnostic
        print(format_config_diagnostic())
        return
    
    # Run installer if requested
    if args.install:
        from minicode.install import main as install_main
        install_main()
        return
    
    cwd = str(Path.cwd())
    argv = remaining_argv

    if args.list_checkpoints is not None:
        raise SystemExit(_handle_list_checkpoints_request(cwd, args.list_checkpoints))

    if args.inspect_session is not None:
        raise SystemExit(_handle_inspect_session_request(cwd, args.inspect_session))

    if args.replay_session is not None:
        raise SystemExit(_handle_replay_session_request(cwd, args.replay_session))

    if args.rewind is not None:
        raise SystemExit(
            _handle_rewind_request(
                cwd,
                args.rewind,
                max(1, args.rewind_steps),
                args.rewind_to,
            )
        )

    if args.preview_rewind is not None:
        raise SystemExit(
            _handle_preview_rewind_request(
                cwd,
                args.preview_rewind,
                max(1, args.rewind_steps),
                args.rewind_to,
            )
        )
    
    # Filter out our custom args before passing to management commands
    management_argv = [a for a in argv if not a.startswith("--")]
    if maybe_handle_management_command(cwd, management_argv):
        return

    runtime = None
    try:
        runtime = load_runtime_config(cwd, trust_project_mcp=args.trust_project_mcp)
    except Exception as e:  # noqa: BLE001
        runtime = None
        print(
            f"⚠️  警告：加载运行时配置失败: {e}\n",
            file=sys.stderr,
        )
        print(
            "🔧 修复方法：\n"
            "  1. 设置模型名称：export ANTHROPIC_MODEL=claude-sonnet-4-20250514\n"
            "  2. 设置 API 密钥：export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  3. 或者编辑 ~/.mini-code/settings.json 文件：\n"
            '     {"model": "claude-sonnet-4-20250514", "env": {"ANTHROPIC_API_KEY": "sk-ant-..."}}\n'
            "  4. 重启 MiniCode\n\n"
            "📖 更多信息：https://github.com/QUSETIONS/MiniCode-Python\n"
            "   暂时降级到模拟模型运行...\n",
            file=sys.stderr,
        )

    prompt_handler = _make_cli_permission_prompt() if sys.stdin.isatty() else None
    tools = create_default_tool_registry(cwd, runtime=runtime)
    permissions = PermissionManager(cwd, prompt=prompt_handler)
    
    # Use unified model registry for adapter creation
    force_mock = runtime is None
    model = create_model_adapter(
        model=runtime.get("model", "") if runtime else "",
        tools=tools,
        runtime=runtime,
        force_mock=force_mock,
    )
    
    # Initialize ContextManager for context window management
    from minicode.context_manager import ContextManager
    from minicode.logging_config import get_logger
    logger = get_logger("main")
    context_mgr = None
    if runtime:
        context_mgr = ContextManager(model=runtime.get("model", "default"))
        logger.info("Context manager initialized for model: %s", runtime.get("model", "unknown"))
    
    # Initialize MemoryManager for cross-session knowledge retention
    from minicode.memory import MemoryManager
    memory_mgr = MemoryManager(project_root=Path(cwd))
    logger.info("Memory manager initialized")
    
    # Initialize UserProfileManager for user preferences
    from minicode.user_profile import UserProfileManager
    profile_manager = UserProfileManager(cwd=cwd)
    profile_manager.load_merged()
    logger.info("User profile manager initialized (global=%s, project=%s)",
                profile_manager.global_path.exists(),
                profile_manager.project_path.exists())
    
    # Initialize Store for global state management (inspired by Claude Code's Zustand store)
    from minicode.state import create_app_store
    app_store = create_app_store(
        initial={
            "session_id": args.session or "new",
            "workspace": cwd,
            "model": runtime.get("model", "mock") if runtime else "mock",
        }
    )
    logger.info("Store initialized with session: %s", app_store.get_state().session_id)
    
    prompt_bundle = build_system_prompt_bundle(
        cwd,
        permissions.get_summary(),
        {
            "skills": tools.get_skills(),
            "mcpServers": tools.get_mcp_servers(),
            "memory_context": memory_mgr.get_relevant_context(),  # Inject memory
            "runtime": runtime,
        },
    )
    messages = [
        {
            "role": "system",
            "content": prompt_bundle.prompt,
        }
    ]
    history = load_history_entries()
    transcript: list[TranscriptEntry] = []

    print(
        _render_banner(
            runtime,
            cwd,
            permissions.get_summary(),
            {
                "transcriptCount": 0,
                "messageCount": len(messages),
                "skillCount": len(tools.get_skills()),
                "mcpCount": len(tools.get_mcp_servers()),
            },
        )
    )
    
    # 显示快速入门指南
    if not sys.stdin.isatty() or os.environ.get("MINI_CODE_SHOW_GUIDE", "1") == "1":
        print(_render_quick_start())
    else:
        print("")

    try:
        if not sys.stdin.isatty():
            for raw_input in sys.stdin:
                user_input = raw_input.strip()
                if not user_input:
                    continue
                if user_input == "/exit":
                    break
                if user_input.startswith("/transcript-save "):
                    output_path = user_input[len("/transcript-save ") :].strip()
                    if not output_path:
                        print("Usage: /transcript-save <path>")
                        continue
                    saved_path = _save_transcript_file(cwd, permissions, transcript, output_path)
                    print(f"Saved transcript to {saved_path}")
                    continue
                memory_result = memory_mgr.handle_user_memory_input(user_input)
                if memory_result is not None:
                    _append_transcript(transcript, kind="user", body=user_input)
                    _append_transcript(transcript, kind="assistant", body=memory_result)
                    print(memory_result)
                    continue
                local_result = _handle_local_command(user_input, tools)
                if local_result is not None:
                    _append_transcript(transcript, kind="user", body=user_input)
                    _append_transcript(transcript, kind="assistant", body=local_result)
                    print(local_result)
                    continue
                shortcut = parse_local_tool_shortcut(user_input)
                if shortcut is not None:
                    _append_transcript(transcript, kind="user", body=user_input)
                    result = tools.execute(
                        shortcut["toolName"],
                        shortcut["input"],
                        context=ToolContext(cwd=cwd, permissions=permissions),
                    )
                    _append_transcript(
                        transcript,
                        kind="tool",
                        body=result.output,
                        toolName=shortcut["toolName"],
                        status="success" if result.ok else "error",
                    )
                    print(result.output)
                    continue
                _append_transcript(transcript, kind="user", body=user_input)
                messages.append({"role": "user", "content": user_input})
                history.append(user_input)
                save_history_entries(history)
                prompt_bundle = build_system_prompt_bundle(
                    cwd,
                    permissions.get_summary(),
                    {
                        "skills": tools.get_skills(),
                        "mcpServers": tools.get_mcp_servers(),
                        "memory_context": memory_mgr.get_relevant_context(query=user_input),
                        "runtime": runtime,
                    },
                )
                messages[0] = {
                    "role": "system",
                    "content": prompt_bundle.prompt,
                }
                permissions.begin_turn()
                messages = run_agent_turn(
                    model=model,
                    tools=tools,
                    messages=messages,
                    cwd=cwd,
                    permissions=permissions,
                    store=app_store,
                    context_manager=context_mgr,
                    runtime=runtime,
                )
                permissions.end_turn()
                
                # Log context usage after turn
                if context_mgr:
                    stats = context_mgr.get_stats()
                    logger.debug("After turn: %d tokens (%.0f%%)", stats.total_tokens, stats.usage_percentage)
                last_assistant = next((message for message in reversed(messages) if message["role"] == "assistant"), None)
                if last_assistant:
                    _append_transcript(transcript, kind="assistant", body=last_assistant["content"])
                    print(last_assistant["content"])
            return

        run_tty_app(
            runtime=runtime,
            tools=tools,
            model=model,
            messages=messages,
            cwd=cwd,
            permissions=permissions,
            resume_session=args.resume,
            list_sessions_only=args.list_sessions,
            memory_manager=memory_mgr,
            context_manager=context_mgr,
            prompt_bundle=prompt_bundle,
            product_snapshot=prompt_bundle.product_snapshot,
        )
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Shutting down gracefully...")
    finally:
        # Graceful shutdown: clean up all resources
        from minicode.logging_config import get_logger
        logger = get_logger("main")
        logger.info("Shutting down...")
        
        # Dispose tools (closes MCP connections)
        try:
            tools.dispose()
            logger.info("Tools disposed successfully")
        except Exception as e:
            logger.warning("Error disposing tools: %s", e)
        
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
