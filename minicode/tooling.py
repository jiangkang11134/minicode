"""SmartCode 工具（Tooling）模块 —— 工具定义、元数据、注册与执行。

提供智能输出截断、工具元数据分类（只读/破坏性/并发安全等）、
工具协议（Protocol）、以及 ToolRegistry 注册中心等基础设施。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from minicode.logging_config import get_logger, log_tool_execution

# ---------------------------------------------------------------------------
# Constants for smart truncation
# ---------------------------------------------------------------------------

# Default max output size (characters) — per tool type
_DEFAULT_MAX_OUTPUT = 30_000       # ~8K tokens, safe for context
_LARGE_OUTPUT_THRESHOLD = 50_000   # Trigger smart truncation above this

# Tool-specific output limits (characters)
_TOOL_OUTPUT_LIMITS: dict[str, int] = {
    "read_file": 40_000,
    "grep_files": 20_000,
    "run_command": 30_000,
    "run_with_debug": 30_000,
    "web_fetch": 20_000,
    "web_search": 15_000,
    "list_files": 15_000,
    "file_tree": 15_000,
    "code_review": 20_000,
    "diff_viewer": 20_000,
    "db_explorer": 20_000,
    "docker_helper": 20_000,
    "test_runner": 25_000,
    "api_tester": 15_000,
}


def _smart_truncate_output(output: str, tool_name: str, max_chars: int | None = None) -> str:
    """智能截断过大的工具输出，以节省上下文窗口。

    截断策略因工具类型而异：
    1.  如果输出未超过限制，则原样返回。
    2.  读取文件：保留头部 + 尾部（文件的开头和结尾最重要）。
    3.  命令输出：保留头部 + 错误行 + 尾部。
    4.  搜索/查找：保留前 N 条匹配结果 + 汇总信息。
    5.  通用：保留头部 + 尾部，附带行数汇总。

    参数:
        output: 原始输出字符串。
        tool_name: 工具名称，用于选择截断策略和限制值。
        max_chars: 可选的最大字符数，若不传则使用工具特定的默认限制。

    返回:
        截断后的输出字符串（若未超限制则原样返回）。
    """
    if not output:
        return output

    limit = max_chars or _TOOL_OUTPUT_LIMITS.get(tool_name, _DEFAULT_MAX_OUTPUT)

    if len(output) <= limit:
        return output

    lines = output.split("\n")
    total_lines = len(lines)
    total_chars = len(output)

    # Calculate how many lines we can keep (rough estimate)
    avg_line_len = total_chars / max(1, total_lines)
    max_lines = int(limit / max(40, avg_line_len))

    if tool_name == "read_file":
        # Keep head + tail — most important for understanding file structure
        head_lines = max(1, int(max_lines * 0.6))
        tail_lines = max(1, max_lines - head_lines)
        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
            f"{tail}"
        )

    if tool_name in ("run_command", "run_with_debug"):
        # Keep head + error lines + tail
        head_lines = max(1, int(max_lines * 0.4))
        tail_lines = max(1, int(max_lines * 0.4))

        # Also extract error/warning lines
        error_pattern = re.compile(r'(?i)(error|fail|exception|traceback|warning)', re.IGNORECASE)
        error_lines = [
            (i, line) for i, line in enumerate(lines)
            if error_pattern.search(line) and head_lines <= i < total_lines - tail_lines
        ]
        error_text = ""
        if error_lines:
            error_text = "\n\n[Key errors/warnings from omitted section:]\n" + "\n".join(
                f"L{i+1}: {line[:200]}" for i, line in error_lines[:20]
            )

        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...{error_text}\n\n"
            f"{tail}"
        )

    if tool_name in ("grep_files", "web_search"):
        # Keep first N matches + summary
        head = "\n".join(lines[:max_lines])
        omitted = total_lines - max_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} more lines omitted (output too large: {total_chars:,} chars, {total_lines} total lines)] ..."
        )

    # Generic: head + tail
    head_lines = max(1, int(max_lines * 0.5))
    tail_lines = max(1, max_lines - head_lines)
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    omitted = total_lines - head_lines - tail_lines
    return (
        f"{head}\n"
        f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
        f"{tail}"
    )


# ---------------------------------------------------------------------------
# Tool metadata (inspired by Claude Code's Tool type)
# ---------------------------------------------------------------------------

class ToolCapability(str, Enum):
    """工具能力标识枚举。

    用于标记工具具备哪些能力/约束，例如是否只读、是否具有破坏性等。
    """
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"
    REQUIRES_PERMISSION = "requires_permission"


@dataclass
class ToolMetadata:
    """工具元数据，用于分类和发现。

    受 Claude Code 的 Tool 类型定义启发，提供工具的静态描述信息。

    属性:
        name: 工具名称。
        description: 工具描述。
        capabilities: 工具能力标识集合。
        input_schema: 输入参数的 JSON Schema。
        is_enabled: 工具是否启用。
        max_result_size_chars: 最大结果字符数。
        tags: 工具标签列表。
    """
    name: str
    description: str
    capabilities: set[ToolCapability] = field(default_factory=set)
    input_schema: dict[str, Any] = field(default_factory=dict)
    is_enabled: bool = True
    max_result_size_chars: int = 10_000
    tags: list[str] = field(default_factory=list)

    @property
    def is_read_only(self) -> bool:
        """检查工具是否为只读。

        返回:
            如果工具具有 READ_ONLY 能力则为 True，否则为 False。
        """
        return ToolCapability.READ_ONLY in self.capabilities

    @property
    def is_destructive(self) -> bool:
        """检查工具是否可能修改或删除数据。

        返回:
            如果工具具有 DESTRUCTIVE 能力则为 True，否则为 False。
        """
        return ToolCapability.DESTRUCTIVE in self.capabilities

    @property
    def is_concurrency_safe(self) -> bool:
        """检查工具是否安全支持并发执行。

        返回:
            如果工具具有 CONCURRENCY_SAFE 能力则为 True，否则为 False。
        """
        return ToolCapability.CONCURRENCY_SAFE in self.capabilities


# ---------------------------------------------------------------------------
# Tool Protocol (inspired by Claude Code's Tool interface)
# ---------------------------------------------------------------------------

class Tool(Protocol):
    """工具协议，定义完整的工具生命周期接口。

    受 Claude Code 的 Tool 类型启发，包含以下方法：
    - call: 执行逻辑
    - description: 动态描述生成
    - validate_input: 输入校验
    - check_permissions: 权限检查
    - 元数据: is_read_only, is_destructive 等

    任何实现了此协议的对象均可作为工具使用。
    """
    @property
    def name(self) -> str: ...

    @property
    def description_template(self) -> str: ...

    def get_description(self, args: dict[str, Any], options: dict[str, Any] | None = None) -> str: ...
    def validate_input(self, args: dict[str, Any]) -> tuple[bool, str]: ...
    def check_permissions(self, args: dict[str, Any], context: ToolContext) -> tuple[bool, str]: ...
    def call(
        self,
        args: dict[str, Any],
        context: ToolContext,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ToolResult: ...
    def is_enabled(self) -> bool: ...
    def is_read_only(self, args: dict[str, Any]) -> bool: ...
    def is_destructive(self, args: dict[str, Any]) -> bool: ...


@dataclass(slots=True)
class BackgroundTaskResult:
    """后台任务执行结果。

    记录由工具启动的后台任务的基本信息，供后续查询和管理。

    属性:
        taskId: 任务唯一标识符。
        type: 任务类型。
        command: 执行的命令。
        pid: 进程 ID。
        status: 任务状态。
        startedAt: 启动时间戳。
    """
    taskId: str
    type: str
    command: str
    pid: int
    status: str
    startedAt: int


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。

    封装工具调用的返回值，包含执行状态和输出内容。

    属性:
        ok: 执行是否成功。
        output: 输出内容（成功时的结果或失败时的错误信息）。
        backgroundTask: 关联的后台任务信息（可选）。
        awaitUser: 是否需要等待用户输入（用于交互式工具）。
    """
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文。

    提供工具执行时所需的运行环境信息。

    属性:
        cwd: 当前工作目录。
        permissions: 权限管理器（可选）。
        session: 会话对象（可选）。
        _runtime: 运行时内部数据（可选）。
    """
    cwd: str
    permissions: Any | None = None
    session: Any | None = None
    _runtime: dict | None = None


Validator = Callable[[Any], Any]
Runner = Callable[[Any, ToolContext], ToolResult]


@dataclass(slots=True)
class ToolDefinition:
    """工具定义（声明式）—— 单一工具的描述、校验与执行封装。

    【为什么需要】ToolDefinition 将工具的元信息、输入规范、校验逻辑和执行逻辑
    聚合为一个不可变的数据单元，使 ToolRegistry 可以统一调度任意工具。

    ╔══════════════════ 完整执行流程 ══════════════════╗
    ║                                                      ║
    ║  ┌─ 各字段含义 ──────────────────────────────┐   ║
    ║  │  name:        str  ← 工具名称 (唯一标识符)        │   ║
    ║  │  description: str  ← 工具描述 (供 LLM 理解)      │   ║
    ║  │  input_schema: dict ← JSON Schema 输入规范        │   ║
    ║  │  validator:   Validator ← 校验 / 转换输入函数     │   ║
    ║  │  run:         Runner   ← 工具核心执行函数          │   ║
    ║  │  metadata:    ToolMetadata | None                 │   ║
    ║  │               ← 可选元数据 (只读/破坏性等)        │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ 工具调用完整链路 ──────────────────────┐   ║
    ║  │                                          │   ║
    ║  │  ① LLM 模型 → 输出工具调用请求            │   ║
    ║  │     {"name": tool_name,                  │   ║
    ║  │      "input": {...}}                     │   ║
    ║  │         │                                │   ║
    ║  │         v                                │   ║
    ║  │  ② ToolRegistry.find(name)               │   ║
    ║  │     → O(1) 返回对应的 ToolDefinition     │   ║
    ║  │         │                                │   ║
    ║  │         v                                │   ║
    ║  │  ③ ToolDefinition.validator(input)       │   ║
    ║  │     → 校验输入是否符合 schema            │   ║
    ║  │     → 清洗 / 转换输入数据                │   ║
    ║  │     → 失败: 返回 ToolResult(ok=False)    │   ║
    ║  │         │                                │   ║
    ║  │         v                                │   ║
    ║  │  ④ ToolDefinition.run(parsed, context)   │   ║
    ║  │     → 接收校验后的 parsed 数据            │   ║
    ║  │     → 执行工具核心逻辑                   │   ║
    ║  │     → 返回 ToolResult(ok=True / output)  │   ║
    ║  │         │                                │   ║
    ║  │         v                                │   ║
    ║  │  ⑤ ToolRegistry 将结果返回给调用方        │   ║
    ║  │     → agent_loop / run_agent_turn()      │   ║
    ║  │     → LLM 模型接收 ToolResult             │   ║
    ║  └──────────────────────────────────────────┘   ║
    ╚══════════════════════════════════════════════════╝
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Validator
    run: Runner
    metadata: ToolMetadata | None = None

    @property
    def is_read_only(self) -> bool:
        """检查此工具是否为只读（安全支持并发执行）。

        优先使用 metadata 中的判断结果，若没有 metadata 则
        根据工具名称进行启发式判断。

        返回:
            如果工具是只读的则为 True，否则为 False。
        """
        if self.metadata:
            return self.metadata.is_read_only
        # Fallback: heuristic based on tool name
        return self.name in _READ_ONLY_TOOL_NAMES

    @property
    def is_concurrency_safe(self) -> bool:
        """检查此工具是否安全支持并发执行。

        只读工具或具有 CONCURRENCY_SAFE 能力的工具视为并发安全。

        返回:
            如果工具是并发安全的则为 True，否则为 False。
        """
        if self.metadata:
            return self.metadata.is_concurrency_safe or self.metadata.is_read_only
        return self.is_read_only


# Heuristic: tool names that are known to be read-only
_READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "grep_files", "file_tree",
    "find_symbols", "find_references", "get_ast_info",
    "code_review", "diff_viewer", "db_explorer",
    "web_fetch", "web_search", "api_tester",
    "ask_user", "todo_write",
})


class ToolRegistry:
    """工具注册中心 —— 统一管理所有工具/skills/MCP 服务器的注册与执行。

    【为什么需要】作为工具系统的中央调度节点，ToolRegistry 集中管理工具的
    注册索引、执行调度和资源生命周期，隔离工具层与 LLM 交互层。

    ╔══════════════════ 完整执行流程 ══════════════════╗
    ║                                                      ║
    ║  ┌─ 内部数据结构 ────────────────────────────┐   ║
    ║  │  _tools:      list[ToolDefinition]             │   ║
    ║  │               ← 原始工具定义列表                │   ║
    ║  │  _tool_index: dict[str, ToolDefinition]        │   ║
    ║  │               ← name → ToolDefinition O(1)    │   ║
    ║  │  _skills:     list[dict]                       │   ║
    ║  │               ← 技能注册列表                    │   ║
    ║  │  _mcp_servers: list[dict]                      │   ║
    ║  │               ← MCP 服务器配置列表              │   ║
    ║  │  _disposer:   Callable | None                  │   ║
    ║  │               ← 资源释放回调                    │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ register() 注册链路 (构造时完成) ────────┐   ║
    ║  │  外部创建 ToolDefinition 列表                    │   ║
    ║  │  → __init__ 接收 tools 参数                    │   ║
    ║  │  → 存入 self._tools                           │   ║
    ║  │  → 建立 _tool_index 索引 (name → ToolDef)     │   ║
    ║  │  → skills / mcp_servers 分别存储              │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ execute() 执行链路 ─────────────────────┐   ║
    ║  │  find(tool_name)  → O(1) 查找                │   ║
    ║  │  → 不存在 → ToolResult(ok=False, 未知工具)   │   ║
    ║  │  → 存在 → 进入四阶段管道:                   │   ║
    ║  │   Phase ①: tool.validator(input_data)       │   ║
    ║  │   │  校验/转换输入                          │   ║
    ║  │   │  失败 → 返回 ToolResult(ok=False)       │   ║
    ║  │   Phase ②: tool.run(parsed, context)        │   ║
    ║  │   │  执行工具核心逻辑                      │   ║
    ║  │   Phase ③: 输出智能截断                    │   ║
    ║  │   │  result.output > 50K chars              │   ║
    ║  │   │  → _smart_truncate_output()             │   ║
    ║  │   Phase ④: 日志记录                        │   ║
    ║  │   │  log_tool_execution(耗时, 状态, 错误)   │   ║
    ║  │   └─ 返回 ToolResult                        │   ║
    ║  │  → 全局异常安全网:                           │   ║
    ║  │   KeyboardInterrupt / SystemExit → 上抛     │   ║
    ║  │   其他 Exception → 转为 ToolResult(ok=False) │   ║
    ║  └───────────────────────┬───────────────────────┘   ║
    ║                          v                            ║
    ║  ┌─ dispose() 资源释放 ─────────────────────┐   ║
    ║  │  _disposer() → 关闭 MCP 连接 / 清理        │   ║
    ║  └──────────────────────────────────────────┘   ║
    ╚══════════════════════════════════════════════════╝
    """
    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
    ) -> None:
        """初始化工具注册中心。

        参数:
            tools: 初始的工具定义列表。
            skills: 可选的技能列表。
            mcp_servers: 可选的 MCP 服务器列表。
            disposer: 可选的资源释放回调函数（在 dispose() 时调用）。
        """
        self._tools = tools
        self._skills = skills or []
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer
        # 工具查找缓存 - O(1) 查找代替 O(n) 遍历
        self._tool_index: dict[str, ToolDefinition] = {t.name: t for t in tools}

    def list(self) -> list[ToolDefinition]:
        """返回所有已注册的工具定义列表。

        返回:
            包含所有 ToolDefinition 的列表。
        """
        return list(self._tools)

    def list_all(self) -> list[str]:
        """返回所有已注册工具的名称列表。

        返回:
            包含所有工具名称字符串的列表。
        """
        return list(self._tool_index.keys())

    def get_skills(self) -> list[dict[str, Any]]:
        """返回所有已注册的技能列表。

        返回:
            包含所有技能字典的列表。
        """
        return list(self._skills)

    def get_mcp_servers(self) -> list[dict[str, Any]]:
        """返回所有已注册的 MCP 服务器列表。

        返回:
            包含所有 MCP 服务器字典的列表。
        """
        return list(self._mcp_servers)

    def find(self, name: str) -> ToolDefinition | None:
        """根据名称查找已注册的工具。

        通过内部索引实现 O(1) 查找效率。

        参数:
            name: 要查找的工具名称。

        返回:
            找到的 ToolDefinition，若不存在则返回 None。
        """
        # O(1) lookup via cached index
        return self._tool_index.get(name)

    def execute(self, tool_name: str, input_data: Any, context: ToolContext) -> ToolResult:
        """执行指定的工具，并提供全方位的异常保护。

        全局异常安全网会捕获除 KeyboardInterrupt 和 SystemExit 之外的
        所有异常，并将其转换为错误 ToolResult，防止单个工具崩溃
        导致整个会话失败。

        保护层级:
        1. 工具不存在 → 返回错误结果
        2. 输入校验失败 → 返回包含输入细节的错误结果
        3. 执行异常 → 返回包含堆栈摘要的错误结果
        4. 输出过大 → 自动智能截断
        5. 意外异常 → 返回错误结果（绝不传播到调用方）

        参数:
            tool_name: 要执行的工具名称。
            input_data: 工具的输入数据。
            context: 工具执行上下文。

        返回:
            工具执行结果（ToolResult），无论是否发生异常都会返回。
        """
        tool = self.find(tool_name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}")

        _logger = get_logger("tools")
        _start = time.monotonic()
        try:
            # Phase 1: Input validation (with error context)
            try:
                parsed = tool.validator(input_data)
            except (ValueError, TypeError, KeyError) as ve:
                log_tool_execution(
                    tool_name, False, (time.monotonic() - _start) * 1000,
                    error=f"input validation: {ve}",
                )
                return ToolResult(
                    ok=False,
                    output=f"Input validation error in {tool_name}: {ve}\n"
                           f"Input was: {str(input_data)[:200]}"
                )

            # Phase 2: Execution (with crash protection)
            result = tool.run(parsed, context)

            # Phase 3: Output sanitization
            if result.output is None:
                result.output = ""

            # Smart truncation for large outputs
            if result.output and len(result.output) > _LARGE_OUTPUT_THRESHOLD:
                result.output = _smart_truncate_output(result.output, tool_name)

            log_tool_execution(
                tool_name, bool(result.ok), (time.monotonic() - _start) * 1000,
                error=None if result.ok else (result.output or "")[:200],
            )
            return result

        except (KeyboardInterrupt, SystemExit):
            # These should always propagate upward
            raise
        except Exception as error:  # noqa: BLE001
            # Global safety net: convert any unhandled exception to error result
            # This prevents a single buggy tool from crashing the entire session
            duration_ms = (time.monotonic() - _start) * 1000
            # Persist the crash to the log file (searchable) while still
            # returning a ToolResult to the caller (issue #5).
            _logger.exception("Tool %s crashed", tool_name)
            log_tool_execution(tool_name, False, duration_ms, error=str(error))
            import traceback
            tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
            # Include last 5 lines of traceback for debugging
            tb_excerpt = "".join(tb_lines[-5:]).strip()
            error_type = type(error).__name__

            return ToolResult(
                ok=False,
                output=f"[{error_type}] Tool {tool_name} crashed: {error}\n"
                       f"Traceback (most recent):\n{tb_excerpt}"
            )

    def dispose(self) -> None:
        """释放注册中心持有的资源。

        如果构造时传入了 disposer 回调，则调用它以执行
        自定义清理逻辑（如关闭连接、停止后台任务等）。
        """
        if self._disposer is not None:
            self._disposer()
