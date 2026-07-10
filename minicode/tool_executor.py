"""工具执行模块 — 单工具执行、超时保护、异常兜底、能力注册。

从 agent_loop_lite.py 拆分而来，职责：
1. 单工具执行（_execute_single_tool）：含 ThreadPoolExecutor 超时、状态更新
2. 工具能力注册（_register_tool_capabilities）：ToolRegistry → CapabilityRegistry
"""

from __future__ import annotations

import concurrent.futures
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicode.logging_config import get_logger
from minicode.state import increment_tool_calls, set_busy, set_idle
from minicode.tooling import ToolContext, ToolRegistry, ToolResult

logger = get_logger("tool_executor")

# 审查系统钩子
try:
    from minicode.review.hooks import get_review_hooks
    _review_hooks: Any | None = None  # 在 run_agent_turn 内通过 init_review_hooks 初始化
except ImportError:
    _review_hooks = None


def init_review_hooks(cwd: str, tools: ToolRegistry | None = None, tool_context: Any | None = None) -> None:
    """初始化审查系统钩子（延迟初始化，在 run_agent_turn 内调用）。"""
    global _review_hooks
    try:
        from minicode.review.hooks import get_review_hooks
        _review_hooks = get_review_hooks(cwd, tools, tool_context)
    except ImportError:
        _review_hooks = None


def _register_tool_capabilities(tools: ToolRegistry) -> None:
    """将工具注册到能力注册表。

    【为什么需要】ToolRegistry 是按名字找工具的，CapabilityRegistry 是按能力找工具的。
    后者支持高级路由，比如"找一个文件写入工具"。这个函数做一次性映射转换。

    参数:
        tools: 工具注册表实例
    """
    from minicode.capability_registry import (
        CapabilityMetadata,
        CapabilityScope,
        get_registry,
    )

    registry = get_registry()
    if registry.list_all():
        return
    for tool_name in tools.list_all():
        try:
            from minicode.capability_registry import (
                CapabilityDomain,
                CapabilityMetadata,
                CapabilityScope,
            )
            tool_def = tools.find(tool_name)
            if not tool_def:
                continue
            domain = CapabilityDomain.UNKNOWN
            if "file" in tool_name or "write" in tool_name or "read" in tool_name:
                domain = CapabilityDomain.FILE
            elif "search" in tool_name or "grep" in tool_name:
                domain = CapabilityDomain.SEARCH
            elif "web" in tool_name or "http" in tool_name or "fetch" in tool_name:
                domain = CapabilityDomain.WEB
            elif "command" in tool_name or "run" in tool_name or "exec" in tool_name:
                domain = CapabilityDomain.EXECUTION
            elif "code" in tool_name or "diff" in tool_name or "review" in tool_name:
                domain = CapabilityDomain.CODE
            elif "memory" in tool_name:
                domain = CapabilityDomain.MEMORY
            scope = CapabilityScope.READONLY
            if any(k in tool_name for k in ("write", "modify", "edit", "delete", "create")):
                scope = CapabilityScope.WRITE
            if any(k in tool_name for k in ("command", "exec", "run")):
                scope = CapabilityScope.DESTRUCTIVE
            if any(k in tool_name for k in ("web", "fetch", "http")):
                scope = CapabilityScope.EXTERNAL
            metadata = CapabilityMetadata(
                name=tool_name, domain=domain, scope=scope,
                description=tool_def.description or f"Tool: {tool_name}",
                tags=["tool", tool_name],
            )
            registry.register(
                metadata,
                lambda **kw: tools.execute(tool_name, kw, ToolContext(cwd=str(Path.cwd()))),
                None,
            )
        except Exception as e:
            logger.debug("Failed to register tool %s as capability: %s", tool_name, e)


def _execute_single_tool(
    call: dict,
    tools: ToolRegistry,
    cwd: str,
    permissions: Any | None,
    session: Any | None,
    runtime: dict | None,
    store: Any | None,
    step: int,
    on_tool_start: Callable[[str, dict], None] | None,
    on_tool_result: Callable[[str, str, bool], None] | None,
    tool_scheduler: Any | None = None,
) -> ToolResult:
    """执行单个工具调用，含超时保护、异常兜底和状态更新。

    完整流程：
    1. 提取 tool_name + tool_input
    2. 前置处理：通知 UI + 设 busy（仅串行模式）
    3. 带超时的工具执行（ThreadPoolExecutor + TOOL_TIMEOUT）
    4. 后置处理：递增计数 + 设 idle + 通知 UI（仅串行模式）
    5. 全局异常安全网：所有未捕获异常转 ToolResult(ok=False)
    """
    tool_name = call["toolName"]
    tool_input = call["input"]

    # ════════════════════════════════════════════════════════════════
    # 钩子 1：写前宽松审查（仅写入类工具）
    # ════════════════════════════════════════════════════════════════
    if _review_hooks and tool_name in ("write_file", "edit_file", "patch_file"):
        result = _review_hooks.on_before_write(tool_name, tool_input)
        if result:
            return result

    try:
        # 前置回调（仅串行模式）
        if on_tool_start:
            on_tool_start(tool_name, tool_input)
        if store:
            store.set_state(set_busy(tool_name))

        # 带超时保护的执行
        _base_timeout = int(os.environ.get("MINICODE_TOOL_TIMEOUT", "120"))
        TOOL_TIMEOUT = (
            int(getattr(tool_scheduler, '_force_tool_timeout', _base_timeout))
            if tool_scheduler and hasattr(tool_scheduler, '_force_tool_timeout')
            else _base_timeout
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    tools.execute, tool_name, tool_input,
                    ToolContext(cwd=cwd, permissions=permissions, session=session, _runtime=runtime),
                )
                result = future.result(timeout=TOOL_TIMEOUT)
        except concurrent.futures.TimeoutError:
            result = ToolResult(ok=False, output=f"Tool '{tool_name}' timed out after {TOOL_TIMEOUT}s")
        except Exception:
            result = tools.execute(
                tool_name, tool_input,
                ToolContext(cwd=cwd, permissions=permissions, session=session, _runtime=runtime),
            )

        # 后置处理（仅串行模式）
        if store:
            store.set_state(increment_tool_calls())
            store.set_state(set_idle())
        if on_tool_result:
            on_tool_result(tool_name, result.output, not result.ok)

        # ════════════════════════════════════════════════════════════════
        # 钩子 2：写后处理（更新 import map + 严格审查触发）
        # 审查触发条件（安全路径 / diff特征 / 历史问题率 / 新人代码）
        # 的判断在 review/hooks.py on_file_written() 中。
        # ════════════════════════════════════════════════════════════════
        if _review_hooks and result.ok and tool_name in ("write_file", "edit_file", "patch_file"):
            fp = tool_input.get("file_path") or tool_input.get("path") or ""
            if fp:
                _review_hooks.on_file_written(fp)

        return result

    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        import traceback
        tb_excerpt = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-3:]).strip()
        error_type = type(exc).__name__
        logger.error("Tool execution pipeline crashed (%s): %s", error_type, exc)
        if store:
            try:
                store.set_state(set_idle())
            except Exception:
                pass
        return ToolResult(
            ok=False,
            output=f"[{error_type}] Tool execution pipeline crashed: {exc}\nTraceback:\n{tb_excerpt}"
        )
