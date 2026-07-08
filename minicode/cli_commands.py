"""SmartCode CLI 命令处理模块。 提供斜杠命令的格式化、匹配、补全以及本地命令的分发处理。
包含所有内置斜杠命令的定义、帮助信息格式化、命令自动补全
以及会话管理、扩展管理、检查点回滚等功能的命令处理逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from minicode.config import (
    CLAUDE_SETTINGS_PATH,
    MINI_CODE_MCP_PATH,
    MINI_CODE_PERMISSIONS_PATH,
    MINI_CODE_SETTINGS_PATH,
    load_runtime_config,
    save_mini_code_settings,
)
from minicode.product_surfaces import (
    build_product_snapshot,
    extension_manifest_payload,
    resolve_extension_manifest,
    set_extension_enabled,
)
from minicode.session import (
    format_rewind_preview,
    format_session_checkpoints,
    format_session_inspect,
    format_session_list,
    format_session_replay,
    format_session_resume,
    get_latest_session,
    list_sessions,
    load_session,
    rewind_session,
    rewind_session_data,
)


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """表示一个斜杠命令的定义。

    保存命令的名称、用法字符串和描述信息，是不可变数据类。

    参数:
        name: 命令名称（如 "/help"）
        usage: 命令的完整用法字符串（包含参数说明）
        description: 命令的功能描述
    """

    name: str
    usage: str
    description: str


SLASH_COMMANDS = [
    SlashCommand("/help", "/help", "Show available slash commands."),
    SlashCommand("/tools", "/tools", "List tools available to the coding agent and tool shortcuts."),
    SlashCommand("/state", "/state", "Show detailed application state and Store summary."),
    SlashCommand("/status", "/status", "Show application state summary and current model."),
    SlashCommand("/cost", "/cost [--detailed]", "Show API cost and usage report."),
    SlashCommand("/context", "/context", "Show context window usage."),
    SlashCommand("/cybernetics", "/cybernetics", "Show cybernetic control system status."),
    SlashCommand("/tasks", "/tasks", "Show current task list."),
    SlashCommand("/memory", "/memory", "Show memory system status."),
    SlashCommand("/config", "/config", "Show configuration diagnostics and validation."),
    SlashCommand("/history", "/history", "Show recent prompt history from ~/.mini-code/history.json."),
    SlashCommand("/clear", "/clear", "Clear the current transcript view."),
    SlashCommand("/collapse", "/collapse", "Collapse all expanded tool-output blocks in the transcript."),
    SlashCommand("/retry", "/retry", "Retry the last natural-language prompt in this session."),
    SlashCommand("/session", "/session", "Inspect the active session, runtime, checkpoints, and recent transcript."),
    SlashCommand("/session", "/session <session-id|latest>", "Inspect a saved session for the current workspace."),
    SlashCommand("/session-replay", "/session-replay", "Replay the active session with checkpoint, history, and transcript timeline."),
    SlashCommand("/session-replay", "/session-replay <session-id|latest>", "Replay a saved session for the current workspace."),
    SlashCommand("/sessions", "/sessions", "List saved sessions for the current workspace."),
    SlashCommand("/instructions", "/instructions", "Inspect the active instruction layering surface."),
    SlashCommand("/hooks", "/hooks", "Inspect active hooks and recent hook telemetry."),
    SlashCommand("/delegation", "/delegation", "Inspect background delegation capacity and running tasks."),
    SlashCommand("/extensions", "/extensions", "Inspect local extension manifests for this workspace."),
    SlashCommand("/extension-inspect", "/extension-inspect <name>", "Inspect a local extension manifest and source path."),
    SlashCommand("/extension-enable", "/extension-enable <name>", "Enable a local extension manifest."),
    SlashCommand("/extension-disable", "/extension-disable <name>", "Disable a local extension manifest."),
    SlashCommand("/readiness", "/readiness", "Inspect provider/runtime readiness for the current workspace."),
    SlashCommand("/checkpoints", "/checkpoints", "List checkpoints for the active session."),
    SlashCommand("/checkpoints", "/checkpoints <session-id|latest>", "List checkpoints for a saved session in the current workspace."),
    SlashCommand("/rewind-preview", "/rewind-preview [latest|steps|checkpoint-id]", "Preview checkpointed file edits that would be rewound for the active session."),
    SlashCommand("/rewind", "/rewind [latest|steps|checkpoint-id]", "Rewind checkpointed file edits for the active session."),
    SlashCommand("/session-rewind-preview", "/session-rewind-preview <session-id|latest> [latest|steps|checkpoint-id]", "Preview checkpointed file edits that would be rewound for a saved session."),
    SlashCommand("/session-rewind", "/session-rewind <session-id|latest> [latest|steps|checkpoint-id]", "Rewind checkpointed file edits for a saved session in the current workspace."),
    SlashCommand("/transcript-save", "/transcript-save <path>", "Save the current session transcript to a text file."),
    SlashCommand("/model", "/model", "Show the current model."),
    SlashCommand("/model", "/model <model-name>", "Persist a model override into ~/.mini-code/settings.json."),
    SlashCommand("/config-paths", "/config-paths", "Show mini-code and Claude fallback settings paths."),
    SlashCommand("/skills", "/skills", "List discovered SKILL.md workflows."),
    SlashCommand("/mcp", "/mcp", "Show configured MCP servers and connection state."),
    SlashCommand("/permissions", "/permissions", "Show mini-code permission storage path."),
    SlashCommand("/exit", "/exit", "Exit mini-code."),
    SlashCommand("/debug", "/debug", "Show scroll and terminal diagnostics."),
    SlashCommand("/user", "/user", "Show or manage user profile (preferences, coding style)."),
    SlashCommand("/ls", "/ls [path]", "List files in a directory."),
    SlashCommand("/grep", "/grep <pattern>::[path]", "Search text in files."),
    SlashCommand("/read", "/read <path>", "Read a file directly."),
    SlashCommand("/write", "/write <path>::<content>", "Write a file directly."),
    SlashCommand("/modify", "/modify <path>::<content>", "Replace a file, showing a reviewable diff before applying it."),
    SlashCommand("/edit", "/edit <path>::<search>::<replace>", "Edit a file by exact replacement."),
    SlashCommand("/patch", "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...", "Apply multiple replacements to one file in one command."),
    SlashCommand("/cmd", "/cmd [cwd::]<command> [args...]", "Run an allowed development command directly."),
]


def format_slash_commands() -> str:
    """格式化所有可用的斜杠命令为带边框的帮助信息字符串。

    将 SLASH_COMMANDS 中的命令按功能分组（核心命令、工具命令、状态信息、
    文件操作、会话管理），生成一个带 Unicode 边框的格式化表格。

    返回:
        格式化后的帮助信息字符串，包含命令分组、名称和说明
    """
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║  📚 Available Commands                                  ║",
        "╠══════════════════════════════════════════════════════════╣",
    ]

    command_groups = {
        "🔧 Core Commands": [
            ("/help", "Show this help message"),
            ("/exit", "Exit mini-code"),
            ("/clear", "Clear the current transcript view"),
            ("/history", "Show recent prompt history"),
        ],
        "🛠️ Tool Commands": [
            ("/tools", "List all available tools"),
            ("/skills", "List discovered SKILL.md workflows"),
            ("/mcp", "Show MCP servers and connection state"),
            ("/cmd", "Run development commands directly"),
        ],
        "📊 Status & Info": [
            ("/status", "Show application state summary"),
            ("/model", "Show or change current model"),
            ("/user", "Show or manage user profile"),
            ("/cost", "Show API cost and usage report"),
            ("/context", "Show context window usage"),
            ("/cybernetics", "Show control-system status"),
            ("/tasks", "Show current task list"),
            ("/memory", "Show memory system status"),
        ],
        "✏️ File Operations": [
            ("/ls [path]", "List files in directory"),
            ("/grep <pattern>", "Search text in files"),
            ("/read <path>", "Read a file directly"),
            ("/write <path>", "Write content to file"),
            ("/edit <path>", "Edit file by exact replacement"),
            ("/patch <path>", "Apply multiple replacements in one go"),
            ("/modify <path>", "Replace file with reviewable diff"),
        ],
        "💾 Session Management": [
            ("/session", "Inspect current session state"),
            ("/session <id>", "Inspect saved session or latest"),
            ("/session-replay", "Replay active session timeline"),
            ("/session-replay <id>", "Replay saved session timeline"),
            ("/sessions", "List saved sessions for workspace"),
            ("/instructions", "Inspect active instruction layering"),
            ("/hooks", "Inspect hook telemetry and failures"),
            ("/delegation", "Inspect background task capacity"),
            ("/extensions", "Inspect local extension manifests"),
            ("/extension-inspect <name>", "Inspect one extension in detail"),
            ("/extension-enable <name>", "Enable a local extension"),
            ("/extension-disable <name>", "Disable a local extension"),
            ("/readiness", "Inspect provider/runtime readiness"),
            ("/checkpoints", "List active session checkpoints"),
            ("/checkpoints <id>", "List saved session checkpoints"),
            ("/rewind-preview [arg]", "Preview active session rewind plan"),
            ("/rewind [arg]", "Rewind active session file edits"),
            ("/session-rewind-preview <id> [arg]", "Preview saved session rewind plan"),
            ("/session-rewind <id> [arg]", "Rewind saved session file edits"),
            ("/transcript-save <path>", "Save transcript to text file"),
            ("/retry", "Retry the last prompt"),
            ("/permissions", "Show permission storage path"),
            ("/config-paths", "Show settings file paths"),
        ],
    }

    for group_name, commands in command_groups.items():
        lines.append(f"║  {group_name:<54}║")
        for cmd, desc in commands:
            cmd_display = f"    {cmd}"
            lines.append(f"║  {cmd_display:<20} {desc:<33} ║")
        lines.append("╠══════════════════════════════════════════════════════════╣")

    lines.extend([
        "║  💡 Tips:                                              ║",
        "║  - Use Tab to autocomplete commands                    ║",
        "║  - Prefix with / to access any command                 ║",
        "║  - Type naturally - I'll understand Chinese & English  ║",
        "╚══════════════════════════════════════════════════════════╝",
    ])

    return "\n".join(lines)


def find_matching_slash_commands(user_input: str) -> list[str]:
    """查找与用户输入匹配的斜杠命令。

    首先尝试精确前缀匹配，如果匹配不到则降级为模糊子序列匹配。
    例如输入 "mem" 可以匹配到 "/memory"。

    参数:
        user_input: 用户输入的字符串

    返回:
        匹配的命令用法字符串列表，若无匹配则返回所有命令列表
    """
    commands = [c.usage for c in SLASH_COMMANDS]
    prefix_matches = [c for c in commands if c.startswith(user_input)]
    if prefix_matches:
        return prefix_matches
    # Fuzzy fallback: subsequence match (e.g., "mem" matches "/memory")
    lower = user_input.lower()
    fuzzy = [c for c in commands if all(ch in c.lower() for ch in lower)]
    return fuzzy if fuzzy else commands


def complete_slash_command(line: str) -> tuple[list[str], str]:
    """为斜杠命令提供 Tab 补全功能。

    根据当前输入的行内容，返回匹配的命令列表和原始输入行。
    优先进行前缀匹配，若无匹配则降级为模糊子序列匹配。

    参数:
        line: 当前输入的命令行字符串

    返回:
        (匹配的命令用法字符串列表, 原始输入行) 的元组
    """
    commands = [c.usage for c in SLASH_COMMANDS]
    hits = [c for c in commands if c.startswith(line)]
    if not hits and line:
        lower = line.lower()
        hits = [c for c in commands if all(ch in c.lower() for ch in lower)]
    return (hits if hits else commands, line)


def try_handle_local_command(
    user_input: str,
    tools=None,
    cwd: str | None = None,
    session=None,
) -> str | None:
    """尝试处理本地斜杠命令。

    根据用户输入的字符串，在函数内部匹配并分发到对应的命令处理逻辑。
    支持的命令包括：/help、/session、/checkpoints、/rewind、/extensions、
    /model、/status、/skills、/mcp 等。

    参数:
        user_input: 用户输入的完整命令行字符串
        tools:      tools 对象（可选），提供工具相关的方法如 get_skills()、get_mcp_servers()
        cwd:        当前工作目录路径（可选）
        session:    当前会话对象（可选）

    返回:
        命令执行结果的字符串，若无法识别命令则返回 None
    """
    def _product_snapshot() -> dict:
        """构建当前会话或工作空间的产品快照。

        优先从 session 对象中提取指令层、钩子状态、委托任务、
        扩展清单和就绪报告等信息；若 session 不可用则回退到
        从工作空间路径构建产品快照。

        返回:
            包含产品快照信息的字典
        """
        if session is not None:
            instruction_layers = list(getattr(session, "instruction_layers", []) or [])
            hook_status = dict(getattr(session, "hook_status", {}) or {})
            delegated_tasks = list(getattr(session, "delegated_tasks", []) or [])
            delegation_status = dict(getattr(session, "delegation_status", {}) or {})
            extension_manifests = list(getattr(session, "extension_manifests", []) or [])
            readiness_report = dict(getattr(session, "readiness_report", {}) or {})
            if any(
                [
                    instruction_layers,
                    hook_status,
                    delegated_tasks,
                    delegation_status,
                    extension_manifests,
                    readiness_report,
                ]
            ):
                metadata = getattr(session, "metadata", None)
                return {
                    "instruction_layers": instruction_layers,
                    "instruction_summary": getattr(metadata, "instruction_summary", ""),
                    "hook_status": hook_status,
                    "hook_summary": getattr(metadata, "hook_summary", ""),
                    "delegated_tasks": delegated_tasks,
                    "delegation_status": delegation_status,
                    "delegation_summary": getattr(metadata, "delegation_summary", ""),
                    "extension_manifests": extension_manifests,
                    "extension_summary": getattr(metadata, "extension_summary", ""),
                    "readiness_report": readiness_report,
                    "readiness_summary": getattr(metadata, "readiness_summary", ""),
                }
        if cwd is None:
            return {}
        return build_product_snapshot(cwd)

    def _format_instruction_surface(snapshot: dict) -> str:
        """格式化指令层信息为可读字符串。

        从快照中提取指令层列表和摘要信息，展示每个层的范围、
        类型、激活状态和路径。

        参数:
            snapshot: 产品快照字典，应包含 instruction_layers 和 instruction_summary

        返回:
            格式化后的指令层信息字符串
        """
        layers = list(snapshot.get("instruction_layers", []) or [])
        lines = [
            "Instruction surface:",
            snapshot.get("instruction_summary", "instructions: unavailable"),
        ]
        if not layers:
            lines.append("No instruction layers discovered for this workspace.")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"Layers ({len(layers)}):")
        for layer in layers:
            scope = str(layer.get("scope") or "unknown")
            kind = str(layer.get("kind") or "unknown")
            exists = "active" if layer.get("exists") else "missing"
            path = str(layer.get("path") or "")
            preview = str(layer.get("preview") or "")
            detail = f"- {scope}/{kind}: {exists}"
            if path:
                detail += f" [{path}]"
            lines.append(detail)
            if preview:
                lines.append(f"  preview: {preview}")
        return "\n".join(lines)

    def _format_hook_surface(snapshot: dict) -> str:
        """格式化钩子系统状态信息为可读字符串。

        从快照中提取钩子状态数据，显示已注册钩子的启用数量、
        调用次数、持续时间和错误信息。

        参数:
            snapshot: 产品快照字典，应包含 hook_status 和 hook_summary

        返回:
            格式化后的钩子状态信息字符串
        """
        status = dict(snapshot.get("hook_status", {}) or {})
        lines = [
            "Hook surface:",
            snapshot.get("hook_summary", "hooks: unavailable"),
        ]
        if not status:
            lines.append("No hook telemetry is available.")
            return "\n".join(lines)
        lines.extend(
            [
                "",
                f"Registered hooks: {status.get('enabled_hooks', 0)}/{status.get('total_hooks', 0)} enabled",
                f"Calls: {status.get('total_calls', 0)}",
                f"Duration: {status.get('total_duration_ms', 0)}ms",
            ]
        )
        failure_count = status.get("failure_count")
        last_status = status.get("last_status")
        last_error = status.get("last_error")
        if failure_count is not None:
            lines.append(f"Failures: {failure_count}")
        if last_status:
            lines.append(f"Last status: {last_status}")
        if last_error:
            lines.append(f"Last error: {last_error}")
        return "\n".join(lines)

    def _format_delegation_surface(snapshot: dict) -> str:
        """格式化任务委托状态信息为可读字符串。

        从快照中提取委托状态和任务列表，显示运行中的任务数量、
        跟踪总数、可用槽位和当前任务详情。

        参数:
            snapshot: 产品快照字典，应包含 delegation_status、delegated_tasks 和 delegation_summary

        返回:
            格式化后的委托状态信息字符串
        """
        status = dict(snapshot.get("delegation_status", {}) or {})
        tasks = list(snapshot.get("delegated_tasks", []) or [])
        lines = [
            "Delegation surface:",
            snapshot.get("delegation_summary", "delegation: unavailable"),
        ]
        if not status and not tasks:
            lines.append("No delegation state is available.")
            return "\n".join(lines)
        if status:
            lines.extend(
                [
                    "",
                    f"Running tasks: {status.get('running_tasks', 0)}",
                    f"Tracked tasks: {status.get('total_tracked', 0)}",
                    f"Slots: {status.get('available_slots', 0)}/{status.get('max_slots', 0)} free",
                ]
            )
            labels = list(status.get("active_labels", []) or [])
            if labels:
                lines.append(f"Active labels: {', '.join(str(label) for label in labels)}")
        if tasks:
            lines.append("")
            lines.append(f"Tracked task details ({min(len(tasks), 5)} shown):")
            for task in tasks[:5]:
                label = str(task.get("label") or task.get("command") or task.get("taskId") or "task")
                task_status = str(task.get("status") or "unknown")
                lines.append(f"- {label} [{task_status}]")
        return "\n".join(lines)

    def _format_extension_surface(snapshot: dict) -> str:
        """格式化扩展清单信息为可读字符串。

        从快照中提取扩展清单列表，显示每个扩展的名称、范围、
        启用状态、版本号和入口点等信息。

        参数:
            snapshot: 产品快照字典，应包含 extension_manifests 和 extension_summary

        返回:
            格式化后的扩展清单信息字符串
        """
        manifests = list(snapshot.get("extension_manifests", []) or [])
        lines = [
            "Extension surface:",
            snapshot.get("extension_summary", "extensions: unavailable"),
        ]
        if not manifests:
            lines.append("No extension manifests were discovered.")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"Extensions ({len(manifests)}):")
        for manifest in manifests:
            name = str(manifest.get("name") or "extension")
            scope = str(manifest.get("scope") or "unknown")
            enabled = "enabled" if manifest.get("enabled", True) else "disabled"
            version = str(manifest.get("version") or "").strip()
            detail = f"- {name} [{scope}, {enabled}]"
            if version:
                detail += f" v{version}"
            lines.append(detail)
            description = str(manifest.get("description") or "").strip()
            entrypoint = str(manifest.get("entrypoint") or "").strip()
            if description:
                lines.append(f"  {description}")
            if entrypoint:
                lines.append(f"  entrypoint: {entrypoint}")
        return "\n".join(lines)

    def _format_readiness_surface(snapshot: dict) -> str:
        """格式化提供商/运行时就绪状态信息为可读字符串。

        从快照中提取就绪报告，显示提供商状态、提供商通道、
        备用提供商候选列表和就绪情况。

        参数:
            snapshot: 产品快照字典，应包含 readiness_report 和 readiness_summary

        返回:
            格式化后的就绪状态信息字符串
        """
        report = dict(snapshot.get("readiness_report", {}) or {})
        lines = [
            "Readiness surface:",
            snapshot.get("readiness_summary", "readiness: unavailable"),
        ]
        if not report:
            lines.append("No readiness report is available.")
            return "\n".join(lines)
        status = str(report.get("status") or "unknown")
        provider = str(report.get("provider") or "unknown")
        provider_ready = bool(report.get("provider_ready"))
        fallback_ready = bool(report.get("fallback_ready"))
        fallback_candidates = [
            str(candidate)
            for candidate in list(report.get("fallback_candidates", []) or [])
            if str(candidate).strip()
        ]
        viable_fallbacks = [
            str(candidate)
            for candidate in list(report.get("viable_fallbacks", []) or [])
            if str(candidate).strip()
        ]
        lines.extend(
            [
                "",
                f"Status: {status}",
                f"Provider: {provider}",
                f"Provider ready: {'yes' if provider_ready else 'no'}",
                f"Channel: {str(report.get('provider_channel') or 'unknown')}",
                f"Fallback ready: {'yes' if fallback_ready else 'no'}",
            ]
        )
        if fallback_candidates:
            lines.append(
                f"Configured fallbacks ({len(viable_fallbacks)}/{len(fallback_candidates)} locally ready):"
            )
            for candidate in fallback_candidates:
                label = "ready" if candidate in viable_fallbacks else "not-ready"
                lines.append(f"- {candidate} [{label}]")
        issues = [str(issue) for issue in list(report.get("issues", []) or []) if str(issue).strip()]
        if issues:
            lines.append("Issues:")
            lines.extend(f"- {issue}" for issue in issues)
        guidance = [
            str(item)
            for item in list(report.get("fallback_guidance", []) or [])
            if str(item).strip()
        ]
        if guidance:
            lines.append("Guidance:")
            lines.extend(f"- {item}" for item in guidance)
        return "\n".join(lines)

    def _format_extension_manifest_detail(identifier: str) -> str:
        """格式化单个扩展清单的详细信息为可读字符串。

        根据扩展标识符解析扩展清单并获取其完整载荷信息，
        包括名称、范围、启用状态、版本、描述和入口点等。

        参数:
            identifier: 扩展的标识符字符串（如 "scope:name"）

        返回:
            格式化后的扩展详细信息字符串，若解析失败返回错误信息
        """
        if cwd is None:
            return "No workspace is available for extension inspection."
        try:
            manifest = resolve_extension_manifest(cwd, identifier)
            payload = extension_manifest_payload(manifest)
        except ValueError as exc:
            return str(exc)
        lines = [
            f"Extension inspect: {manifest.name}",
            f"Scope: {manifest.scope}",
            f"Enabled: {'yes' if manifest.enabled else 'no'}",
            f"Manifest: {manifest.path}",
        ]
        if manifest.version:
            lines.append(f"Version: {manifest.version}")
        if manifest.description:
            lines.append(f"Description: {manifest.description}")
        if manifest.entrypoint:
            entrypoint = Path(manifest.path).parent / manifest.entrypoint
            exists = "yes" if entrypoint.exists() else "no"
            lines.append(f"Entrypoint: {manifest.entrypoint}")
            lines.append(f"Entrypoint path: {entrypoint}")
            lines.append(f"Entrypoint exists: {exists}")
        extra_keys = sorted(
            key for key in payload.keys()
            if key not in {"name", "version", "description", "enabled", "entrypoint"}
        )
        if extra_keys:
            lines.append("Extra manifest keys:")
            lines.extend(f"- {key}" for key in extra_keys)
        return "\n".join(lines)

    def _set_extension_state(identifier: str, enabled: bool) -> str:
        """设置扩展的启用或禁用状态。

        根据指定的标识符和启禁用标记，调用底层接口修改扩展状态。
        成功后再调用 _format_extension_manifest_detail 显示更新后的详情。

        参数:
            identifier: 扩展的标识符字符串
            enabled:    True 表示启用，False 表示禁用

        返回:
            操作结果字符串，包含新状态和扩展详情
        """
        if cwd is None:
            return "No workspace is available for extension changes."
        try:
            manifest = set_extension_enabled(cwd, identifier, enabled)
        except ValueError as exc:
            return str(exc)
        status = "enabled" if enabled else "disabled"
        return (
            f"Extension {manifest.scope}:{manifest.name} is now {status}.\n\n"
            f"{_format_extension_manifest_detail(f'{manifest.scope}:{manifest.name}')}"
        )

    def _format_rewind_result(target_session, restored, prefix: str) -> str:
        """格式化回滚操作结果为可读字符串。

        将回滚的检查点列表和恢复后的会话信息格式化为用户可读的文本，
        包含检查点 ID 前缀和对应文件名。

        参数:
            target_session: 回滚操作的目标会话对象
            restored:       已恢复的检查点项列表，每项包含 checkpoint_id 和 file_path
            prefix:         结果前缀文本（如 "Rewound"）

        返回:
            格式化后的回滚结果字符串
        """
        restored_preview = ", ".join(
            f"[{item.checkpoint_id[:8]}] {Path(item.file_path).name or item.file_path}"
            for item in restored
        )
        return (
            f"{prefix} {len(restored)} checkpoint(s) for session {target_session.session_id[:8]}.\n"
            f"Restored: {restored_preview}\n\n"
            f"{format_session_resume(target_session)}"
        )

    def _workspace_session(target: str):
        """获取工作空间对应的会话对象。

        根据目标标识符获取会话：如果目标为 "latest" 则获取最新会话，
        否则按指定会话 ID 加载。

        参数:
            target: 会话标识符，"latest" 表示最新会话，或指定会话 ID

        返回:
            会话对象，若未找到则返回 None
        """
        workspace = str(Path(cwd).resolve()) if cwd else None
        return (
            get_latest_session(workspace=workspace)
            if target == "latest"
            else load_session(target)
        )

    if user_input in {"/", "/help"}:
        return format_slash_commands()

    if user_input == "/config-paths":
        return "\n".join(
            [
                f"mini-code settings: {MINI_CODE_SETTINGS_PATH}",
                f"mini-code permissions: {MINI_CODE_PERMISSIONS_PATH}",
                f"mini-code mcp: {MINI_CODE_MCP_PATH}",
                f"compat fallback: {CLAUDE_SETTINGS_PATH}",
            ]
        )

    if user_input == "/permissions":
        return f"permission store: {MINI_CODE_PERMISSIONS_PATH}"

    if user_input == "/sessions":
        workspace = str(Path(cwd).resolve()) if cwd else None
        sessions = list_sessions()
        if workspace is not None:
            sessions = [meta for meta in sessions if meta.workspace == workspace]
        return format_session_list(sessions)

    if user_input == "/instructions":
        return _format_instruction_surface(_product_snapshot())

    if user_input == "/hooks":
        return _format_hook_surface(_product_snapshot())

    if user_input == "/delegation":
        return _format_delegation_surface(_product_snapshot())

    if user_input == "/extensions":
        return _format_extension_surface(_product_snapshot())

    if user_input.startswith("/extension-inspect "):
        identifier = user_input[len("/extension-inspect ") :].strip()
        if not identifier:
            return "Usage: /extension-inspect <name>"
        return _format_extension_manifest_detail(identifier)

    if user_input.startswith("/extension-enable "):
        identifier = user_input[len("/extension-enable ") :].strip()
        if not identifier:
            return "Usage: /extension-enable <name>"
        return _set_extension_state(identifier, True)

    if user_input.startswith("/extension-disable "):
        identifier = user_input[len("/extension-disable ") :].strip()
        if not identifier:
            return "Usage: /extension-disable <name>"
        return _set_extension_state(identifier, False)

    if user_input == "/readiness":
        return _format_readiness_surface(_product_snapshot())

    if user_input == "/session":
        if session is None:
            return "No active session."
        return format_session_inspect(session)

    if user_input == "/session-replay":
        if session is None:
            return "No active session."
        return format_session_replay(session)

    if user_input == "/checkpoints":
        if session is None:
            return "No active session."
        return format_session_checkpoints(session)

    if user_input.startswith("/session "):
        target = user_input[len("/session ") :].strip()
        if not target:
            return "Usage: /session <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_inspect(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for inspection."
        return format_session_inspect(target_session)

    if user_input.startswith("/session-replay "):
        target = user_input[len("/session-replay ") :].strip()
        if not target:
            return "Usage: /session-replay <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_replay(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for replay."
        return format_session_replay(target_session)

    if user_input.startswith("/checkpoints "):
        target = user_input[len("/checkpoints ") :].strip()
        if not target:
            return "Usage: /checkpoints <session-id|latest>"
        if session is not None and target == getattr(session, "session_id", None):
            return format_session_checkpoints(session)
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found for checkpoint inspection."
        return format_session_checkpoints(target_session)

    if user_input == "/rewind-preview" or user_input.startswith("/rewind-preview "):
        if session is None:
            return "No active session."
        target = user_input[len("/rewind-preview") :].strip()
        steps = 1
        checkpoint_id = None
        if target and target != "latest":
            if target.isdigit():
                steps = max(1, int(target))
            else:
                checkpoint_id = target
        return format_rewind_preview(
            session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )

    if user_input == "/rewind" or user_input.startswith("/rewind "):
        if session is None:
            return "No active session."
        target = user_input[len("/rewind") :].strip()
        steps = 1
        checkpoint_id = None
        if target and target != "latest":
            if target.isdigit():
                steps = max(1, int(target))
            else:
                checkpoint_id = target
        restored = rewind_session_data(
            session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )
        if not restored:
            return "No checkpoints available to rewind."
        return _format_rewind_result(session, restored, "Rewound")

    if user_input.startswith("/session-rewind "):
        raw = user_input[len("/session-rewind ") :].strip()
        if not raw:
            return "Usage: /session-rewind <session-id|latest> [latest|steps|checkpoint-id]"
        parts = raw.split(maxsplit=1)
        target = parts[0]
        rewind_arg = parts[1].strip() if len(parts) > 1 else "latest"
        steps = 1
        checkpoint_id = None
        if rewind_arg and rewind_arg != "latest":
            if rewind_arg.isdigit():
                steps = max(1, int(rewind_arg))
            else:
                checkpoint_id = rewind_arg
        if session is not None and target == getattr(session, "session_id", None):
            restored = rewind_session_data(
                session,
                steps=steps,
                checkpoint_id=checkpoint_id,
            )
            if not restored:
                return "No checkpoints available to rewind for that session."
            return _format_rewind_result(session, restored, "Rewound")
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found to rewind."
        rewound_session, restored = rewind_session(
            target_session.session_id,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )
        if rewound_session is None or not restored:
            return "No checkpoints available to rewind for that session."
        return _format_rewind_result(rewound_session, restored, "Rewound")

    if user_input.startswith("/session-rewind-preview "):
        raw = user_input[len("/session-rewind-preview ") :].strip()
        if not raw:
            return "Usage: /session-rewind-preview <session-id|latest> [latest|steps|checkpoint-id]"
        parts = raw.split(maxsplit=1)
        target = parts[0]
        rewind_arg = parts[1].strip() if len(parts) > 1 else "latest"
        steps = 1
        checkpoint_id = None
        if rewind_arg and rewind_arg != "latest":
            if rewind_arg.isdigit():
                steps = max(1, int(rewind_arg))
            else:
                checkpoint_id = rewind_arg
        if session is not None and target == getattr(session, "session_id", None):
            return format_rewind_preview(
                session,
                steps=steps,
                checkpoint_id=checkpoint_id,
            )
        target_session = _workspace_session(target)
        if target_session is None:
            return "No saved session found to preview."
        return format_rewind_preview(
            target_session,
            steps=steps,
            checkpoint_id=checkpoint_id,
        )

    if user_input == "/skills":
        skills = tools.get_skills() if tools else []
        if not skills:
            return "No skills discovered. Add skills under ~/.mini-code/skills/<name>/SKILL.md, .mini-code/skills/<name>/SKILL.md, .claude/skills/<name>/SKILL.md, or ~/.claude/skills/<name>/SKILL.md."
        return "\n".join(
            f"{skill['name']}  {skill['description']}  [{skill['source']}]"
            for skill in skills
        )

    if user_input == "/config":
        from minicode.config import format_config_diagnostic
        return format_config_diagnostic()

    if user_input == "/state":
        try:
            from minicode.state import handle_state_command
            return handle_state_command()
        except ImportError:
            return "State system not available. Please ensure state.py exists."

    if user_input == "/memory":
        # Memory system display
        try:
            from minicode.memory import MemoryManager
            memory_mgr = MemoryManager(project_root=Path(cwd) if cwd else Path.cwd())
            return memory_mgr.format_stats()
        except Exception as e:
            return f"Error loading memory: {e}"

    if user_input == "/context":
        # Context usage display
        try:
            from minicode.context_manager import load_context_state
            ctx_mgr = load_context_state()
            if ctx_mgr:
                return ctx_mgr.format_context_details()
            else:
                return "No context state available. Context tracking starts after first turn."
        except Exception as e:
            return f"Error loading context: {e}"

    if user_input == "/cybernetics":
        return format_cybernetics_status()

    if user_input == "/mcp":
        servers = tools.get_mcp_servers() if tools else []
        if not servers:
            return "No MCP servers configured. Add mcpServers to ~/.mini-code/settings.json, ~/.mini-code/mcp.json, or project .mcp.json."
        lines = []
        for server in servers:
            suffix = f"  error={server['error']}" if server.get("error") else ""
            protocol = f"  protocol={server['protocol']}" if server.get("protocol") else ""
            resources = f"  resources={server['resourceCount']}" if server.get("resourceCount") is not None else ""
            prompts = f"  prompts={server['promptCount']}" if server.get("promptCount") is not None else ""
            lines.append(
                f"{server['name']}  status={server['status']}  tools={server['toolCount']}{resources}{prompts}{protocol}{suffix}"
            )
        return "\n".join(lines)

    if user_input == "/status":
        try:
            runtime = load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        from minicode.model_registry import detect_provider
        provider = detect_provider(runtime["model"], runtime)
        auth_methods = []
        if runtime.get("authToken"):
            auth_methods.append("ANTHROPIC_AUTH_TOKEN")
        if runtime.get("apiKey"):
            auth_methods.append("ANTHROPIC_API_KEY")
        if runtime.get("openaiApiKey"):
            auth_methods.append("OPENAI_API_KEY")
        if runtime.get("openrouterApiKey"):
            auth_methods.append("OPENROUTER_API_KEY")
        if runtime.get("customApiKey"):
            auth_methods.append("CUSTOM_API_KEY")
        return "\n".join(
            [
                f"model: {runtime['model']}",
                f"provider: {provider.value}",
                f"baseUrl: {runtime['baseUrl']}",
                f"auth: {', '.join(auth_methods) or 'none'}",
                f"mcp servers: {len(runtime.get('mcpServers', {}))}",
                runtime["sourceSummary"],
            ]
        )

    if user_input == "/model":
        try:
            runtime = load_runtime_config()
            from minicode.model_registry import format_model_status
            return format_model_status(runtime["model"], runtime)
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"

    if user_input.startswith("/model "):
        arg = user_input[len("/model "):].strip()
        if not arg:
            from minicode.model_registry import format_model_list
            return format_model_list()
        # Subcommands
        if arg in ("status", "info"):
            try:
                runtime = load_runtime_config()
                from minicode.model_registry import format_model_status
                return format_model_status(runtime["model"], runtime)
            except Exception as error:  # noqa: BLE001
                return f"runtime not configured: {error}"
        if arg in ("list", "ls"):
            from minicode.model_registry import format_model_list
            return format_model_list()
        # Provider filter: /model anthropic, /model openrouter, etc.
        from minicode.model_registry import Provider, format_model_list
        for p in Provider:
            if arg.lower() == p.value:
                return format_model_list(provider=p)
        # Otherwise: set model name
        save_mini_code_settings({"model": arg})
        return f"saved model={arg} to {MINI_CODE_SETTINGS_PATH}\nRestart SmartCode for the change to take effect."

    if user_input == "/user" or user_input.startswith("/user "):
        from minicode.user_profile import handle_user_command
        args = user_input[len("/user"):].strip()
        return handle_user_command(args)

    return None


def format_cybernetics_status() -> str:
    """格式化控制系统（Cybernetic）的状态报告为可读字符串。

    加载控制系统监督器报告和上下文状态，展示所有控制器的名称及功能描述，
    当前的总体健康度、风险等级以及推荐操作。

    返回:
        格式化后的控制系统状态信息字符串
    """
    from minicode.cybernetic_supervisor import CyberneticSupervisor, load_supervisor_report
    from minicode.context_manager import load_context_state

    controllers = [
        ("ContextCyberneticsOrchestrator", "context pressure PID + prediction"),
        ("CostControlLoop", "budget PID for tool-result persistence"),
        ("VerificationController", "risk-adaptive verification planning"),
        ("ToolSchedulerController", "error/latency-aware concurrency control"),
        ("MemoryInjectionController", "context-aware memory injection"),
        ("ModelSelectionController", "cost/latency/failure-aware model routing"),
        ("ProgressController", "health/stall task progress control"),
        ("CyberneticSupervisor", "global health and risk aggregation"),
    ]

    ctx = load_context_state()
    snapshots = []
    if ctx:
        stats = ctx.get_stats()
        usage = stats.usage_percentage / 100.0
        snapshots.append(CyberneticSupervisor().snapshot_from_context({
            "sensor": {"current_usage": usage},
            "predictor": {"urgency": 0.0},
        }))
    persisted_report = load_supervisor_report()
    report = persisted_report or CyberneticSupervisor().report(snapshots)

    lines = [
        "Cybernetic Control System",
        "=" * 50,
        f"overall_health: {report.overall_health:.2f}",
        f"risk_level: {report.risk_level.value}",
        f"source: {'latest agent-loop report' if persisted_report else 'current persisted context'}",
        "",
        "Controllers:",
    ]
    for name, desc in controllers:
        lines.append(f"  - {name}: {desc}")
    lines.extend([
        "",
        "Runtime aggregation:",
        "  - pipeline outputs: progress_control + verification_plan + cybernetic_supervisor",
        "  - agent loop logs: context + cost + tool scheduling supervisor report",
    ])
    if report.recommended_actions:
        lines.append("")
        lines.append("Current actions:")
        for action in report.recommended_actions[:5]:
            lines.append(f"  - {action}")
    return "\n".join(lines)
