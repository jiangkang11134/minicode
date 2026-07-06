"""Hooks event system for MiniCode Python. Inspired by Claude Code's hooks system (PreToolUse, PostToolUse, Stop, etc.)
and plugin event listeners.

Provides lifecycle hooks for:
- Tool execution (pre/post)
- Agent lifecycle (start/stop)
- Session events (save/resume)
- User interactions (input/output)

Hooks can trigger external scripts, logging, or custom behaviors.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Hook events
# ---------------------------------------------------------------------------

class HookEvent(str, Enum):
    """生命周期钩子事件枚举。

    定义系统中所有可触发钩子的事件，分为以下几类：
    - Tool lifecycle（工具生命周期）：工具执行前/后
    - Agent lifecycle（Agent 生命周期）：启动/停止/子 Agent 生成/完成
    - Session events（会话事件）：保存/恢复
    - User interactions（用户交互）：输入/输出
    - System（系统）：启动/关闭
    """
    # Tool lifecycle
    PRE_TOOL_USE = "pre_tool_use"       # 工具执行前
    POST_TOOL_USE = "post_tool_use"     # 工具执行后

    # Agent lifecycle
    AGENT_START = "agent_start"         # Agent 回合开始
    AGENT_STOP = "agent_stop"           # Agent 回合停止
    SUBAGENT_START = "subagent_start"   # 子 Agent 生成
    SUBAGENT_STOP = "subagent_stop"     # 子 Agent 完成

    # Session events
    SESSION_SAVE = "session_save"       # 会话自动保存
    SESSION_RESUME = "session_resume"   # 会话恢复

    # User interactions
    USER_INPUT = "user_input"           # 用户提交输入
    ASSISTANT_OUTPUT = "assistant_output"  # 助手生成回复

    # System
    STARTUP = "startup"                 # 应用启动
    SHUTDOWN = "shutdown"               # 应用关闭


# ---------------------------------------------------------------------------
# Hook context
# ---------------------------------------------------------------------------

@dataclass
class HookContext:
    """传递给钩子处理函数的上下文对象。

    封装事件类型、时间戳、事件数据以及额外元数据。
    通过属性方法提供对常用数据字段的类型安全访问。
    """
    event: HookEvent
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_name(self) -> str | None:
        """获取触发事件的工具名称。"""
        return self.data.get("tool_name")

    @property
    def tool_input(self) -> Any:
        """获取工具调用的输入参数。"""
        return self.data.get("tool_input")

    @property
    def tool_output(self) -> str | None:
        """获取工具执行的输出结果。"""
        return self.data.get("tool_output")

    @property
    def is_error(self) -> bool:
        """判断事件是否表示错误状态。"""
        return self.data.get("is_error", False)

    @property
    def session_id(self) -> str | None:
        """获取当前会话 ID。"""
        return self.data.get("session_id")

    @property
    def user_input(self) -> str | None:
        """获取用户提交的输入文本。"""
        return self.data.get("user_input")

    @property
    def assistant_output(self) -> str | None:
        """获取助手生成的回复文本。"""
        return self.data.get("assistant_output")


# ---------------------------------------------------------------------------
# Hook handler
# ---------------------------------------------------------------------------

HookHandler = Callable[[HookContext], None]
AsyncHookHandler = Callable[[HookContext], Any]


@dataclass
class HookRegistration:
    """已注册的钩子及其元数据记录。

    包含关联的事件类型、处理函数指针、同步/异步标志、
    启用状态、调用统计（次数、耗时、失败次数）和最后状态。
    """
    event: HookEvent
    handler: HookHandler | AsyncHookHandler
    is_async: bool = False
    enabled: bool = True
    description: str = ""
    created_at: float = field(default_factory=time.time)
    call_count: int = 0
    last_called: float | None = None
    total_duration_ms: int = 0
    failure_count: int = 0
    last_error: str = ""
    last_status: str = "idle"


# ---------------------------------------------------------------------------
# Hook manager
# ---------------------------------------------------------------------------

class HookManager:
    """钩子注册与执行管理器。

    管理所有钩子的注册、注销、同步/异步触发和执行统计。
    受 Claude Code 的 hooks 系统和插件事件监听器启发。
    线程安全：所有注册/注销操作通过 threading.Lock 保护。
    """

    def __init__(self):
        """初始化钩子管理器，为每个 HookEvent 创建空列表，默认启用。"""
        self._hooks: dict[HookEvent, list[HookRegistration]] = {
            event: [] for event in HookEvent
        }
        self._enabled = True
        self._lock = threading.Lock()
    
    def register(
        self,
        event: HookEvent,
        handler: HookHandler | AsyncHookHandler,
        description: str = "",
    ) -> Callable[[], None]:
        """为指定事件注册一个钩子处理函数。

        自动检测处理函数是同步还是异步（通过 asyncio.iscoroutinefunction），
        将注册信息添加到对应事件的钩子列表中。
        返回一个注销函数，调用后可从列表中移除该注册。

        参数:
            event: 要监听的事件类型
            handler: 处理函数（同步或异步均可）
            description: 可读的描述文本，用于调试和状态展示

        返回:
            注销函数，调用后移除该钩子注册
        """
        import asyncio
        
        with self._lock:
            registration = HookRegistration(
                event=event,
                handler=handler,
                is_async=asyncio.iscoroutinefunction(handler),
                description=description,
            )
            
            self._hooks[event].append(registration)
            
            def unregister():
                with self._lock:
                    if registration in self._hooks[event]:
                        self._hooks[event].remove(registration)
        
        return unregister
    
    async def fire(self, event: HookEvent, **kwargs: Any) -> list[Any]:
        """异步触发一个事件，按顺序调用所有已注册的钩子处理函数。

        为每个钩子创建 HookContext 并调用其处理函数
        （自动区分同步/异步处理）。钩子执行异常不会中断主流程，
        异常信息会作为字符串结果返回。

        参数:
            event: 要触发的事件类型
            **kwargs: 传递给钩子的事件数据（会放入 HookContext.data）

        返回:
            所有钩子的返回值列表；异常时返回 "Hook error: {e}" 字符串
        """
        if not self._enabled:
            return []
        
        context = HookContext(event=event, data=kwargs)
        results = []
        
        for registration in self._hooks[event]:
            if not registration.enabled:
                continue
            
            start_time = time.time()
            try:
                if registration.is_async:
                    result = await registration.handler(context)
                else:
                    result = registration.handler(context)
                
                registration.call_count += 1
                registration.last_called = time.time()
                registration.last_status = "success"
                registration.last_error = ""
                
                duration_ms = int((time.time() - start_time) * 1000)
                registration.total_duration_ms += duration_ms
                
                results.append(result)
            
            except Exception as e:
                # Don't let hook errors break main flow
                registration.failure_count += 1
                registration.last_called = time.time()
                registration.last_status = "error"
                registration.last_error = str(e)
                results.append(f"Hook error: {e}")
        
        return results
    
    def fire_sync(self, event: HookEvent, **kwargs: Any) -> list[Any]:
        """同步触发事件，仅执行同步（非异步）钩子处理函数。

        在锁保护下对钩子列表进行快照，避免迭代期间被修改。
        异步钩子（is_async=True）将被跳过，防止在同步上下文中意外 await。

        参数:
            event: 要触发的事件类型
            **kwargs: 传递给钩子的事件数据

        返回:
            所有同步钩子的返回值列表
        """
        if not self._enabled:
            return []
        
        context = HookContext(event=event, data=kwargs)
        results = []
        
        with self._lock:
            handlers = list(self._hooks[event])  # snapshot for safe iteration
        
        for registration in handlers:
            if not registration.enabled or registration.is_async:
                continue
            
            start_time = time.time()
            try:
                result = registration.handler(context)
                registration.call_count += 1
                registration.last_called = time.time()
                registration.last_status = "success"
                registration.last_error = ""
                
                duration_ms = int((time.time() - start_time) * 1000)
                registration.total_duration_ms += duration_ms
                
                results.append(result)
            
            except Exception as e:
                registration.failure_count += 1
                registration.last_called = time.time()
                registration.last_status = "error"
                registration.last_error = str(e)
                results.append(f"Hook error: {e}")
        
        return results
    
    def enable(self) -> None:
        """启用所有钩子（全局开关）。"""
        self._enabled = True

    def disable(self) -> None:
        """禁用所有钩子（全局开关），fire 和 fire_sync 均不执行。"""
        self._enabled = False

    def get_hook_stats(self, event: HookEvent | None = None) -> dict[str, Any]:
        """获取钩子执行统计信息。

        可指定事件类型筛选，或获取所有钩子的汇总统计。
        返回包含总钩子数、已启用数、总调用次数、总耗时和失败次数的字典，
        以及每个钩子的详细统计列表。

        参数:
            event: 可选的事件类型筛选器，为 None 时统计所有事件

        返回:
            包含统计摘要和详细钩子信息的字典
        """
        if event:
            hooks = self._hooks.get(event, [])
        else:
            hooks = [h for hooks_list in self._hooks.values() for h in hooks_list]
        
        return {
            "total_hooks": len(hooks),
            "enabled_hooks": sum(1 for h in hooks if h.enabled),
            "total_calls": sum(h.call_count for h in hooks),
            "total_duration_ms": sum(h.total_duration_ms for h in hooks),
            "failure_count": sum(h.failure_count for h in hooks),
            "hooks": [
                {
                    "event": h.event.value,
                    "description": h.description or getattr(h.handler, "__name__", "hook"),
                    "enabled": h.enabled,
                    "is_async": h.is_async,
                    "call_count": h.call_count,
                    "failure_count": h.failure_count,
                    "last_status": h.last_status,
                    "last_error": h.last_error,
                    "total_duration_ms": h.total_duration_ms,
                }
                for h in hooks
            ],
        }
    
    def format_hook_status(self) -> str:
        """格式化钩子状态信息为可读文本，用于展示和调试。

        遍历所有 HookEvent，列出每个事件下已注册的钩子及其状态；
        末尾汇总总钩子数、启用数、总调用次数和总耗时。

        返回:
            格式化的状态文本字符串
        """
        lines = ["Hooks Status", "=" * 50, ""]
        
        for event in HookEvent:
            hooks = self._hooks[event]
            if not hooks:
                continue
            
            lines.append(f"{event.value}:")
            for hook in hooks:
                status = "✓" if hook.enabled else "✗"
                lines.append(
                    f"  {status} {hook.description or hook.handler.__name__} "
                    f"({hook.call_count} calls, {hook.total_duration_ms}ms)"
                )
            lines.append("")
        
        stats = self.get_hook_stats()
        lines.extend([
            "-" * 50,
            f"Total hooks: {stats['total_hooks']}",
            f"Enabled: {stats['enabled_hooks']}",
            f"Total calls: {stats['total_calls']}",
            f"Total duration: {stats['total_duration_ms']}ms",
        ])
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

def create_logging_hook(log_file: Path | None = None) -> HookHandler:
    """创建一个记录所有事件的日志钩子处理函数。

    生成的钩子函数会提取时间戳、事件名、工具名和会话 ID 等信息，
    格式化为 `[HH:MM:SS] event_name tool=xxx session=xxxx` 形式。
    若指定 log_file，则追加写入该文件。

    参数:
        log_file: 可选的日志文件路径，指定后会将日志写入文件

    返回:
        日志钩子处理函数（同步）
    """
    def handler(ctx: HookContext) -> None:
        timestamp = time.strftime("%H:%M:%S", time.localtime(ctx.timestamp))
        message = f"[{timestamp}] {ctx.event.value}"
        
        if ctx.tool_name:
            message += f" tool={ctx.tool_name}"
        if ctx.session_id:
            message += f" session={ctx.session_id[:8]}"
        
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(message + "\n")
    
    return handler


def create_script_hook(script_path: Path) -> AsyncHookHandler:
    """创建一个执行外部脚本的钩子处理函数（异步）。

    生成的异步处理函数会根据脚本文件后缀自动选择解释器：
    - .py：使用当前 Python 解释器
    - .bat/.cmd：使用 cmd /c
    - .ps1：使用 powershell
    - .sh（Windows 上）：尝试 bash，回退 sh
    - 其他：直接执行

    传递事件名和事件数据作为脚本参数。

    参数:
        script_path: 要执行的外部脚本路径

    返回:
        异步钩子处理函数，返回脚本的标准输出或错误信息
    """
    async def handler(ctx: HookContext) -> str:
        try:
            # On Windows, CreateProcess can't directly execute script files
            # (.py, .sh, etc.).  Detect the script type and invoke through
            # the appropriate interpreter / shell.
            script_str = str(script_path)
            suffix = script_path.suffix.lower()
            if sys.platform == "win32" and suffix in (".py", ".sh", ".bat", ".cmd", ".ps1"):
                if suffix == ".py":
                    cmd_prefix = [sys.executable, script_str]
                elif suffix in (".bat", ".cmd"):
                    cmd_prefix = ["cmd", "/c", script_str]
                elif suffix == ".ps1":
                    cmd_prefix = ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_str]
                else:
                    # .sh on Windows — try bash if available, fall back to sh
                    cmd_prefix = ["bash", script_str]
            else:
                cmd_prefix = [script_str]

            process = await asyncio.create_subprocess_exec(
                *cmd_prefix,
                ctx.event.value,
                *([str(v) for v in ctx.data.values()]),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                return stdout.decode("utf-8", errors="replace")
            else:
                return f"Script failed: {stderr.decode('utf-8', errors='replace')}"
        
        except Exception as e:
            return f"Script execution failed: {e}"
    
    return handler


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_hook_manager = HookManager()


def get_hook_manager() -> HookManager:
    """获取全局单例的 HookManager 实例。"""
    return _hook_manager


def register_hook(
    event: HookEvent,
    handler: HookHandler | AsyncHookHandler,
    description: str = "",
) -> Callable[[], None]:
    """注册一个钩子（便利函数，使用全局 HookManager）。

    参数:
        event: 要监听的事件类型
        handler: 处理函数（同步或异步）
        description: 可读描述文本

    返回:
        注销函数
    """
    return _hook_manager.register(event, handler, description)


async def fire_hook(event: HookEvent, **kwargs: Any) -> list[Any]:
    """异步触发一个钩子事件（便利函数，使用全局 HookManager）。

    参数:
        event: 要触发的事件类型
        **kwargs: 传递给钩子的事件数据

    返回:
        所有钩子的返回值列表
    """
    return await _hook_manager.fire(event, **kwargs)


def fire_hook_sync(event: HookEvent, **kwargs: Any) -> list[Any]:
    """同步触发一个钩子事件（便利函数，使用全局 HookManager）。

    仅调用同步注册的钩子处理函数。如果事件没有注册任何监听器则提前返回空列表。

    参数:
        event: 要触发的事件类型
        **kwargs: 传递给钩子的事件数据

    返回:
        所有同步钩子的返回值列表
    """
    # Early return if no listeners registered for this event
    if not _hook_manager._hooks.get(event):
        return []
    # Cache context dict to avoid repeated creation
    context = kwargs.get("context") or {}
    if context is not kwargs.get("context"):
        kwargs = {**kwargs, "context": context}
    return _hook_manager.fire_sync(event, **kwargs)
