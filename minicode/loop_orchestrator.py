"""Agent 循环编排 — Prelude → Recurrent Kernel → Coda 三阶段。

从 agent_loop_lite.py 拆分而来，职责：
1. run_agent_turn() 主循环，编排三个阶段的调度
2. Prelude：状态准备、Task 构建、上下文预检
3. Recurrent Kernel：Step A（策略）→ Step B（模型调用）→ Step C（判断）→ Step D（工具）
4. Coda：清理、指标、记忆反馈

辅助函数：
  _upsert_stable_task_state_message — 消息列表中替换"稳定任务状态"
  _is_empty_assistant_response — 判断模型是否返回空内容
  _extract_task_description — 从消息中提取用户原始请求
  _build_work_chain_task — 构建 TaskObject
  _build_layered_context — 构建分层上下文
  _format_diagnostics — 格式化诊断信息
  _is_recoverable_thinking_stop — 判断思考中断是否可恢复
  _should_treat_assistant_as_progress — 判断模型输出是进度还是最终答案
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from typing import Any

from minicode.agent_intelligence import ErrorClassifier, NudgeGenerator, ToolScheduler

# ── Agent 智能辅助 ──
from minicode.agent_metrics import AgentMetricsCollector
from minicode.circuit_breaker import CompactionCircuitBreaker

# ── 上下文管理 ──
from minicode.context_manager import ContextManager, estimate_message_tokens
from minicode.decision_audit import DecisionOutcome, get_auditor
from minicode.hooks import HookEvent, fire_hook_sync

# ── 任务系统 ──
from minicode.intent_parser import parse_intent
from minicode.layered_context import ContextBuilder, LayeredContext
from minicode.logging_config import get_logger
from minicode.micro_compact import MicroCompactor

# ── 子模块（拆分目标） ──
from minicode.model_caller import (
    _infer_active_model_id,
    _is_at_blocking_limit,
    _model_next,
    _summarize_model_api_failure,
)
from minicode.permissions import PermissionManager
from minicode.runtime_profiles import resolve_runtime_profile
from minicode.state import AppState, Store
from minicode.task_graph import TaskGraph
from minicode.task_graph import TaskState as GraphTaskState
from minicode.task_object import TaskObject, TaskState, build_task
from minicode.tool_executor import _execute_single_tool, _register_tool_capabilities, _review_hooks, init_review_hooks
from minicode.tooling import ToolContext, ToolRegistry, ToolResult

# ── 回合状态机 ──
from minicode.turn_kernel import (
    TurnPreludeState,
    TurnRecurrentState,
    TurnVerificationState,
    build_stable_task_pack,
    build_turn_coda_summary,
    build_widening_transition_nudge,
    decide_assistant_turn,
    decide_tool_turn,
    derive_turn_step_policy,
    finalize_work_chain_task,
    render_turn_policy_message,
)
from minicode.types import (
    AgentStep,
    ChatMessage,
    ModelAdapter,
    RuntimeEvent,
)
from minicode.working_memory import get_working_memory, protect_context

logger = get_logger("loop_orchestrator")

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

NUDGE_CONTINUE = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete. "
    "Prefer taking the next concrete action over explaining what you plan to do."
)

NUDGE_AFTER_TOOL_RESULT = (
    "You have received tool results. Review them briefly, then take the next "
    "concrete action: call another tool, edit code, or give an explicit <final> "
    "answer only if the task is truly complete. Do not restate what you just saw."
)

NUDGE_AFTER_EMPTY_RESPONSE = (
    "Your last response was empty. This often happens after tool errors or when "
    "the model is uncertain. Pick the most likely next action and try it — you can "
    "adjust based on results. Call a tool, edit code, or give <final> if done."
)

NUDGE_AFTER_EMPTY_NO_TOOLS = (
    "Your last response was empty but you have not used any tools yet. Start by "
    "inspecting the relevant files (read_file, grep_files, list_files) to understand "
    "the codebase before making changes."
)

RESUME_AFTER_PAUSE = (
    "Resume from the previous pause. Continue with the next concrete tool call, "
    "code change, or <final> answer."
)

RESUME_AFTER_MAX_TOKENS = (
    "Your previous response was cut short by the token limit. Resume immediately "
    "with the next concrete action — pick up where you left off."
)

STABLE_TASK_STATE_MARKER = "[Stable task state]"


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

def _upsert_stable_task_state_message(
    messages: list[ChatMessage],
    stable_text: str,
) -> list[ChatMessage]:
    """替换"稳定任务状态"消息，避免旧状态无限堆积浪费 token。

    【为什么需要】每步迭代后任务状态会变化，如果每次都 append 新状态，
    旧状态会无限堆积。这个函数用"替换"代替"追加"，同一时间只有一条最新的状态消息。

    实现：删除所有以 [Stable task state] 开头的 system 消息，然后把新的追加进去。

    参数:
        messages: 当前对话消息列表
        stable_text: 当前稳定的任务状态文本

    返回:
        更新后的消息列表（只有一条稳定状态消息）
    """
    filtered = [
        m for m in messages
        if not (m.get("role") == "system" and str(m.get("content", "")).startswith(STABLE_TASK_STATE_MARKER))
    ]
    filtered.append({"role": "system", "content": f"{STABLE_TASK_STATE_MARKER}\n{stable_text}"})
    return filtered


def _is_empty_assistant_response(content: str) -> bool:
    """判断模型返回的内容是否为空（纯空白字符也算空）。

    【为什么需要】模型返回空响应通常意味着卡住了或不确定。
    后面 decide_assistant_turn 会根据这个判断是重试还是给 nudge 提示。

    参数:
        content: 模型返回的内容字符串

    返回:
        True=内容是空的
    """
    return len(content.strip()) == 0


def _extract_task_description(messages: list[ChatMessage]) -> str:
    """从消息列表中找到用户原始的请求。

    【为什么需要】消息列表里混着系统提示、nudge 提示语（"Continue from your progress..."）、
    历史对话等各种内容。原始任务描述只有一条（用户第一次提出的请求），后面的都是辅助指令。
    这个函数把真正的那条任务描述捞出来用于构建 TaskObject。

    实现：遍历消息，找第一条 role=user 且不以 "Continue" 或 "Your last" 开头的消息。

    参数:
        messages: 对话消息列表

    返回:
        任务描述文本（前 500 字符），没找到则返回 "Unknown task"
    """
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            content = str(msg["content"])
            if not content.startswith("Continue") and not content.startswith("Your last"):
                return content[:500]
    return "Unknown task"


def _build_work_chain_task(messages: list[ChatMessage]) -> tuple[TaskObject | None, dict]:
    """构建工作链任务对象。

    【为什么需要】agent 不能只在"字符串层面"理解任务。把用户的自然语言输入转成
    结构化的 TaskObject（含意图类型、动作类型、复杂度等），后续路由、进度跟踪、
    记忆反馈才有结构化数据可用。

    流程：_extract_task_description() → parse_intent() → build_task()

    参数:
        messages: 对话消息列表

    返回:
        (TaskObject, metadata) 元组，如果无法识别任务则返回 (None, {})
    """
    raw_input = _extract_task_description(messages)
    if raw_input == "Unknown task":
        return None, {}
    intent = parse_intent(raw_input)
    task = build_task(intent, raw_input)
    metadata = {
        "intent_type": intent.intent_type.value,
        "action_type": intent.action_type.value,
        "confidence": intent.confidence,
        "entities": intent.entities,
        "complexity": intent.complexity_hint,
    }
    return task, metadata


def _build_layered_context(
    messages: list[ChatMessage],
    system_prompt: str = "",
    project_context: str = "",
    task: TaskObject | None = None,
) -> tuple[LayeredContext, ContextBuilder]:
    """构建分层上下文。

    【为什么需要】agent 的上下文不是一个平面文本，它分多层（系统提示、项目知识、
    对话历史、任务草稿）。分层管理可以在压缩时保留关键层、检索时按层过滤。

    参数:
        messages: 对话消息列表
        system_prompt: 系统提示词（可选）
        project_context: 项目上下文（可选）
        task: 任务对象（可选）

    返回:
        (LayeredContext, ContextBuilder) 元组
    """
    context = LayeredContext()
    builder = ContextBuilder(context)
    if system_prompt:
        builder.set_system_prompt(system_prompt)
    if project_context:
        builder.add_project_memory(project_context)
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            builder.add_session_message(role, content)
    if task:
        scratchpad = (
            f"Task: {task.title}\nGoal: {task.goal}\n"
            f"Constraints: {len(task.constraints)}\nExpected outputs: {len(task.expected_outputs)}"
        )
        builder.add_scratchpad(scratchpad)
    return context, builder


def _format_diagnostics(stop_reason, block_types, ignored_block_types) -> str:
    """格式化诊断信息字符串。

    【为什么需要】decide_assistant_turn 需要把模型的停止原因和阻塞信息拼到 nudge 提示里。
    这个函数统一格式，生成类似 "stop_reason=max_tokens; blocks=thinking" 的文本。

    参数:
        stop_reason: 停止原因（如 done, max_tokens, pause_turn）
        block_types: 阻塞类型列表（如 thinking, tool_use）
        ignored_block_types: 被忽略的阻塞类型列表

    返回:
        格式化的诊断字符串，无内容则返回空字符串
    """
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(*, is_empty, stop_reason, ignored_block_types) -> bool:
    """判断空响应是否是"思考被截断"（可恢复）还是"模型不会答"（需换策略）。

    【为什么需要】模型有时因为思考过程被截断或用户打断了而返回空内容。
    这不同于"模型不知道该怎么回答"的空响应。前者只需要告诉模型"接着想"，
    后者需要换策略。区分两者避免在可恢复的情况下浪费一次重试。

    参数:
        is_empty: 响应是否为空
        stop_reason: 停止原因
        ignored_block_types: 被忽略的阻塞类型列表

    返回:
        True=是可恢复的思考中断，可以重试
    """
    if not is_empty:
        return False
    if stop_reason not in {"pause_turn", "max_tokens"}:
        return False
    return "thinking" in (ignored_block_types or [])


def _should_treat_assistant_as_progress(*, kind, content, saw_tool_result) -> bool:
    """判断模型输出是进度消息还是最终答案。

    【为什么需要】模型有时会先输出一段"我正在分析..."的中间思考再回答问题。
    如果不区分进度消息和最终答案，agent 可能把中间思考误认为最终结论而过早停止工具调用。

    参数:
        kind: 响应类型（progress/final/None）
        content: 响应内容（未使用，保留为接口一致）
        saw_tool_result: 是否已看到工具执行结果

    返回:
        True=应视为进度消息，False=应视为最终答案
    """
    if kind == "progress":
        return True
    if kind == "final":
        return False
    if not saw_tool_result:
        return False
    return False


# ══════════════════════════════════════════════════════════════
# 核心
# ══════════════════════════════════════════════════════════════

def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    session: Any | None = None,
    store: Store[AppState] | None = None,
    max_steps: int = 50,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    on_runtime_event: Callable[[RuntimeEvent], None] | None = None,
    on_assistant_stream_chunk: Callable[[str], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
    runtime: dict | None = None,
    metrics_collector: AgentMetricsCollector | None = None,
    system_prompt: str = "",
    project_context: str = "",
    enable_work_chain: bool = True,
) -> list[ChatMessage]:
    """运行单轮 agent 交互循环。

    三阶段：
    1. Prelude：准备 turn 状态、构建任务、上下文预检 + 微压缩
    2. Recurrent Kernel（while 循环）：策略推导→调模型→判断返回→执行工具
    3. Coda：钩子触发、收尾

    异常处理：ConnectionError/TimeoutError/其他→错误消息返回，不崩溃。
    """
    # ════════════════════════════════════════════════════════════
    # Prelude
    # ════════════════════════════════════════════════════════════

    current_messages = list(messages)
    runtime = runtime or {}

    # 确定模型名
    configured_runtime_model = (
        str(runtime.get("configuredModel", "")).strip()
        or str(runtime.get("model", "")).strip()
        or str(getattr(model, "model_id", "") or "").strip()
    )
    if configured_runtime_model:
        runtime.setdefault("configuredModel", configured_runtime_model)

    # 初始化审查系统钩子（延迟初始化）
    init_review_hooks(cwd, tools, ToolContext(cwd=cwd, permissions=permissions, session=session, _runtime=runtime))

    # profile 和状态跟踪器
    runtime_profile = resolve_runtime_profile(runtime, fallback_max_steps=max_steps)
    turn_state = TurnRecurrentState(
        max_steps=runtime_profile.max_steps,
        profile_name=runtime_profile.name,
        widen_after_step=runtime_profile.widen_after_step,
        empty_response_retry_limit=runtime_profile.empty_response_retry_limit,
        recoverable_thinking_retry_limit=runtime_profile.recoverable_thinking_retry_limit,
        verification_state=TurnVerificationState(
            strict=runtime_profile.strict_step_verification,
            requires_explicit_final=runtime_profile.strict_step_verification,
        ),
    )
    max_steps = runtime_profile.max_steps

    # 事件通知
    def emit_runtime_event(
        *, category, message, emit_progress=True,
        stop_reason="", widening_reason="", evidence_summary="",
    ) -> None:
        policy = turn_state.step_policy
        event = RuntimeEvent(
            category=category, message=message,
            step=turn_state.step or None,
            profile=runtime_profile.name,
            phase=policy.phase if policy else "",
            verification_focus=policy.verification_focus if policy else "",
            stop_reason=stop_reason, widening_reason=widening_reason,
            evidence_summary=evidence_summary,
        )
        if on_runtime_event:
            on_runtime_event(event)
        if emit_progress and on_progress_message:
            on_progress_message(message)

    tool_scheduler = ToolScheduler(metrics_collector=metrics_collector)
    prelude = TurnPreludeState(auditor=get_auditor() if enable_work_chain else None)

    # 构建任务（非控制论，是任务跟踪用的）
    if enable_work_chain:
        prelude.task, prelude.task_metadata = _build_work_chain_task(current_messages)
        if prelude.task:
            prelude.task_graph = TaskGraph(name=f"turn-{prelude.task.id}")
            graph_task = prelude.task_graph.add_task(
                name=prelude.task.title or prelude.task.id,
                description=prelude.task.goal or prelude.task.description,
            )
            prelude.task_graph_id = graph_task.id
            slot = prelude.task_graph.assign_slot(graph_task.id, slot_name="turn")
            prelude.task_slot_key = f"{slot.slot_name}:{slot.task_id}"
            prelude.task_graph.start_task(prelude.task_slot_key)
        prelude.layered_context, prelude.context_builder = _build_layered_context(
            current_messages, system_prompt, project_context, prelude.task,
        )
        _register_tool_capabilities(tools)

    # 上下文预检 + 微压缩
    micro_compactor = MicroCompactor()  # 轻量微压缩（非控制论，是必要的内存管理）
    compaction_breaker = CompactionCircuitBreaker()

    if context_manager:
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info("Context: %d tokens (%.0f%%), %d messages",
                     stats.total_tokens, stats.usage_percentage, stats.messages_count)

        # Layer 1: 微压缩
        current_messages, mc_stats = micro_compactor.compact(current_messages)
        if mc_stats.reason != "no_action":
            context_manager.messages = current_messages

        # 基本压缩（非控制论版本）
        if context_manager.should_auto_compact():
            if compaction_breaker.is_allowed():
                try:
                    logger.warning("Context near limit, auto-compacting...")
                    current_messages = getattr(context_manager, 'compact_messages', lambda: current_messages)()
                    compaction_breaker.record_success()
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Auto-compact failed: %s", exc)

    # ════════════════════════════════════════════════════════════
    # Recurrent Kernel
    # ════════════════════════════════════════════════════════════

    try:
        while turn_state.has_remaining_steps():
            step = turn_state.begin_step()

            # ── Step A: 策略推导 ──
            previous_policy = turn_state.step_policy
            current_policy = derive_turn_step_policy(turn_state)
            policy_message = render_turn_policy_message(previous_policy=previous_policy, current_policy=current_policy)
            if policy_message:
                turn_state.set_progress_summary(policy_message)
                emit_runtime_event(category="phase", message=policy_message)
                logger.info("Turn policy update: %s", policy_message)

            # 激进步策略要求压缩
            if current_policy.should_compact_aggressively and context_manager and context_manager.should_auto_compact() and compaction_breaker.is_allowed():
                try:
                    current_messages = getattr(context_manager, 'compact_messages', lambda: current_messages)()
                    compaction_breaker.record_success()
                except Exception as exc:
                    compaction_breaker.record_failure()
                    logger.warning("Aggressive compaction failed: %s", exc)

            # 更新稳定任务状态
            protected_context = get_working_memory().get_protected_content()
            turn_state.stable_task_pack = build_stable_task_pack(
                task=prelude.task,
                task_metadata=prelude.task_metadata,
                protected_context=protected_context,
                task_graph=prelude.task_graph,
                task_slot_key=prelude.task_slot_key,
                latest_tool_result_summary=turn_state.latest_tool_result_summary,
                progress_state=turn_state.progress_state,
                verification_state=turn_state.verification_state,
                budget_signals=turn_state.budget_signals,
            )
            if turn_state.stable_task_pack:
                stable_text = turn_state.stable_task_pack.to_protected_text()
                current_messages = _upsert_stable_task_state_message(current_messages, stable_text)
                if runtime_profile.name == "single-deep":
                    protect_context(content=stable_text, entry_type="active_task",
                                    ttl_seconds=runtime_profile.working_memory_ttl_seconds,
                                    importance=runtime_profile.working_memory_importance)
                if context_manager:
                    context_manager.messages = current_messages

            # Hook
            fire_hook_sync(HookEvent.AGENT_START, step=step, cwd=cwd)

            # ── Step B: 模型调用 ──
            next_step: AgentStep
            try:
                # Layer 0: 预判式上下文守卫
                if context_manager:
                    cm_stats = context_manager.get_stats()
                    if _is_at_blocking_limit(cm_stats.total_tokens, context_manager.context_window):
                        blocking_msg = (
                            f"Context near limit ({cm_stats.total_tokens} / {context_manager.context_window} tokens). "
                            "Use /compact manually, or reduce task scope."
                        )
                        logger.warning("Preemptive guard: %s", blocking_msg)
                        emit_runtime_event(category="stop", message=blocking_msg, stop_reason="blocked")
                        if on_assistant_message:
                            on_assistant_message(blocking_msg)
                        current_messages.append({"role": "assistant", "content": blocking_msg})
                        return current_messages

                next_step = _model_next(
                    model, current_messages,
                    on_stream_chunk=on_assistant_stream_chunk,
                    on_thinking_chunk=on_thinking_chunk,
                    store=store,
                )
            except KeyboardInterrupt:
                raise
            except ConnectionError as error:
                fallback = f"Network error (connection failed or dropped): {error}"
                logger.error("Model API connection error: %s", error)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(category="stop", message=fallback, emit_progress=False, stop_reason="blocked")
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                return current_messages
            except TimeoutError as error:
                fallback = f"Model API timeout: {error}"
                logger.error("Model API timeout: %s", error)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(category="stop", message=fallback, emit_progress=False, stop_reason="blocked")
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                return current_messages
            except Exception as error:
                error_type = type(error).__name__
                active_model_id = _infer_active_model_id(model, runtime, error)
                fallback = _summarize_model_api_failure(
                    error_type=error_type, error=error, active_model_id=active_model_id, runtime=runtime,
                )
                logger.error("Model API error (%s): %s", error_type, error)

                # 尝试反应式压缩（如果是 prompt too long）
                error_str = str(error).lower()
                needs_recovery = "prompt" in error_str and ("too long" in error_str or "exceeds" in error_str)
                if needs_recovery:
                    # 不用 ContextCybernetics，直接用基础压缩
                    if context_manager:
                        try:
                            recovered = getattr(context_manager, 'compact_messages', lambda: current_messages)()
                            if len(recovered) < len(current_messages):
                                current_messages = recovered
                                logger.info("Reactive compact recovered, retrying...")
                                continue
                        except Exception:
                            pass

                if on_assistant_message:
                    on_assistant_message(fallback)
                turn_state.set_stop_reason("blocked")
                emit_runtime_event(category="stop", message=fallback, emit_progress=False, stop_reason="blocked")
                current_messages.append({"role": "assistant", "content": fallback})
                return current_messages

            # ── Step C: 处理模型返回 ──
            if next_step.type == "assistant":
                is_empty = _is_empty_assistant_response(next_step.content)
                diagnostics = next_step.diagnostics
                assistant_decision = decide_assistant_turn(
                    turn_state=turn_state,
                    step_content=next_step.content,
                    step_kind=getattr(next_step, "kind", None),
                    stop_reason=diagnostics.stopReason if diagnostics else None,
                    block_types=diagnostics.blockTypes if diagnostics else None,
                    ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
                    is_empty=is_empty,
                    treat_as_progress=(not is_empty and _should_treat_assistant_as_progress(
                        kind=getattr(next_step, "kind", None), content=next_step.content,
                        saw_tool_result=turn_state.saw_tool_result,
                    )),
                    is_recoverable_thinking_stop=_is_recoverable_thinking_stop(
                        is_empty=is_empty,
                        stop_reason=diagnostics.stopReason if diagnostics else None,
                        ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
                    ),
                    format_diagnostics=_format_diagnostics,
                    nudge_continue=NUDGE_CONTINUE,
                    nudge_after_tool_result=NUDGE_AFTER_TOOL_RESULT,
                    resume_after_pause=RESUME_AFTER_PAUSE,
                    resume_after_max_tokens=RESUME_AFTER_MAX_TOKENS,
                    nudge_after_empty_response=NUDGE_AFTER_EMPTY_RESPONSE,
                    nudge_after_empty_no_tools=NUDGE_AFTER_EMPTY_NO_TOOLS,
                    step_policy=turn_state.step_policy,
                )

                if assistant_decision.kind == "progress":
                    if assistant_decision.assistant_content:
                        turn_state.set_progress_summary(assistant_decision.assistant_content)
                        if assistant_decision.runtime_event_category is not None:
                            emit_runtime_event(
                                category=assistant_decision.runtime_event_category,
                                message=assistant_decision.assistant_content,
                                evidence_summary=(
                                    turn_state.verification_state.evidence_summary
                                    or turn_state.latest_tool_result_summary
                                ),
                            )
                        elif on_progress_message:
                            on_progress_message(assistant_decision.assistant_content)
                        current_messages.append({"role": "assistant_progress", "content": assistant_decision.assistant_content})
                    if assistant_decision.user_content:
                        current_messages.append({"role": "user", "content": assistant_decision.user_content})
                    continue

                if assistant_decision.kind == "retry":
                    if assistant_decision.user_content:
                        current_messages.append({"role": "user", "content": assistant_decision.user_content})
                    continue

                if assistant_decision.kind == "fallback":
                    if assistant_decision.stop_reason == "widen_needed":
                        transitioned = turn_state.activate_widening(extra_steps=runtime_profile.widening_step_bonus)
                        if transitioned:
                            widening_message = (
                                assistant_decision.assistant_content
                                or "Depth stalled; switching to widened mode."
                            )
                            if turn_state.widening_trigger_reason:
                                widening_message += f" Escalation trigger: {turn_state.widening_trigger_reason}."
                            turn_state.set_progress_summary("runtime widened after the narrow path stalled")
                            emit_runtime_event(
                                category="widening", message=widening_message,
                                widening_reason=turn_state.widening_trigger_reason,
                                evidence_summary=turn_state.widening_trigger_evidence,
                            )
                            current_messages.append({"role": "assistant_progress", "content": widening_message})
                            current_messages.append({
                                "role": "user",
                                "content": build_widening_transition_nudge(
                                    turn_state.latest_tool_result_summary,
                                    widening_reason=turn_state.widening_trigger_reason,
                                    widening_evidence_summary=turn_state.widening_trigger_evidence,
                                ),
                            })
                            continue
                    if assistant_decision.stop_reason:
                        turn_state.set_stop_reason(assistant_decision.stop_reason)
                        emit_runtime_event(
                            category="stop",
                            message=assistant_decision.assistant_content or "Turn stopped without a final answer.",
                            emit_progress=False, stop_reason=assistant_decision.stop_reason,
                            evidence_summary=turn_state.verification_state.evidence_summary or turn_state.latest_tool_result_summary,
                        )
                    if assistant_decision.assistant_content and on_assistant_message:
                        on_assistant_message(assistant_decision.assistant_content)
                    if assistant_decision.assistant_content:
                        current_messages.append({"role": "assistant", "content": assistant_decision.assistant_content})
                    return current_messages

                if assistant_decision.stop_reason:
                    turn_state.set_stop_reason(assistant_decision.stop_reason)
                    emit_runtime_event(
                        category="stop",
                        message=assistant_decision.assistant_content or "Turn completed.",
                        emit_progress=False, stop_reason=assistant_decision.stop_reason,
                        evidence_summary=turn_state.verification_state.evidence_summary or turn_state.latest_tool_result_summary,
                    )
                if assistant_decision.assistant_content:
                    turn_state.set_progress_summary("assistant finalized the turn")
                    if on_assistant_message:
                        on_assistant_message(assistant_decision.assistant_content)
                    current_messages.append({"role": "assistant", "content": assistant_decision.assistant_content})
                if assistant_decision.protect_final_answer and assistant_decision.assistant_content:
                    protect_context(
                        content=assistant_decision.assistant_content[:500], entry_type="key_decision",
                        ttl_seconds=runtime_profile.working_memory_ttl_seconds,
                        importance=runtime_profile.working_memory_importance,
                    )
                return current_messages

            # ── Step D: 执行工具 ──
            if next_step.content:
                role = "assistant_progress" if next_step.contentKind == "progress" else "assistant"
                if role == "assistant_progress":
                    turn_state.set_progress_summary(next_step.content)
                    if on_progress_message:
                        on_progress_message(next_step.content)
                    current_messages.append({"role": role, "content": next_step.content})
                    current_messages.append({"role": "user", "content": NUDGE_CONTINUE})
                else:
                    turn_state.set_progress_summary(next_step.content)
                    if on_assistant_message:
                        on_assistant_message(next_step.content)
                    current_messages.append({"role": role, "content": next_step.content})

            if not next_step.calls and next_step.content and next_step.contentKind != "progress":
                turn_state.set_stop_reason("done")
                emit_runtime_event(category="stop", message=next_step.content, emit_progress=False, stop_reason="done")
                return current_messages

            # 执行工具
            calls = next_step.calls
            _results: list[tuple[dict, ToolResult]] = []

            if len(calls) <= 1:
                # 单工具，串行
                for call in calls:
                    if metrics_collector:
                        metrics_collector.start_tool(call["toolName"])
                    result = _execute_single_tool(
                        call, tools, cwd, permissions, session, runtime, store, step,
                        on_tool_start, on_tool_result, tool_scheduler,
                    )
                    if metrics_collector:
                        metrics_collector.end_tool(success=result.ok, error=result.output if not result.ok else "")
                    _results.append((call, result))
            else:
                # 多工具：调度器分类
                concurrent_calls, serial_calls = tool_scheduler.schedule_calls(calls, tools)
                _results.clear()

                # Phase 1: 并行执行只读工具
                if concurrent_calls:
                    max_workers = tool_scheduler.get_recommended_max_workers(
                        concurrent_calls,
                        error_rate=turn_state.tool_error_count / max(step, 1),
                        avg_latency=step * 2.0,
                        recent_failures=turn_state.tool_error_count,
                    )
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mc-tool") as pool:
                        future_to_call = {
                            pool.submit(_execute_single_tool, call, tools, cwd, permissions, session, runtime,
                                        None, step, None, None, tool_scheduler): call
                            for call in concurrent_calls
                        }
                        for future in concurrent.futures.as_completed(future_to_call):
                            call = future_to_call[future]
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = ToolResult(ok=False, output=f"Concurrent execution error: {exc}")
                            _results.append((call, result))

                # Phase 2: 串行执行写入工具
                if serial_calls:
                    for call in serial_calls:
                        if metrics_collector:
                            metrics_collector.start_tool(call["toolName"])
                        result = _execute_single_tool(
                            call, tools, cwd, permissions, session, runtime, store, step,
                            on_tool_start, on_tool_result, tool_scheduler,
                        )
                        if metrics_collector:
                            metrics_collector.end_tool(success=result.ok, error=result.output if not result.ok else "")
                        _results.append((call, result))
                        if result.awaitUser:
                            break

            # 处理所有工具结果
            call_order = {call["id"]: idx for idx, call in enumerate(calls)}
            _results.sort(key=lambda pair: call_order.get(pair[0]["id"], 999))

            for call, result in _results:
                # Hook
                fire_hook_sync(HookEvent.POST_TOOL_USE, tool_name=call["toolName"], tool_output=result.output,
                               is_error=not result.ok, step=step)

                tool_summary = f"{call['toolName']}: {result.output[:200]}"
                turn_state.record_tool_result(result.ok, summary=tool_summary)
                tool_decision = decide_tool_turn(tool_name=call["toolName"], result_output=result.output, await_user=result.awaitUser)

                if tool_decision.progress_summary:
                    turn_state.set_progress_summary(tool_decision.progress_summary)

                # 错误处理 + nudge
                if not result.ok:
                    classified = ErrorClassifier.classify(result.output, tool_name=call["toolName"])
                    nudge = NudgeGenerator.generate(classified, retry_count=turn_state.tool_error_count)
                    result_output = result.output + "\n\n[System note: " + nudge + "]"
                else:
                    result_output = result.output

                # （ReadDedup 去重已在精简版中省略，不影响核心流程）

                current_messages.append({
                    "role": "assistant_tool_call", "toolUseId": call["id"],
                    "toolName": call["toolName"], "input": call["input"],
                })
                current_messages.append({
                    "role": "tool_result", "toolUseId": call["id"],
                    "toolName": call["toolName"], "content": result_output, "isError": not result.ok,
                })

                if tool_decision.kind == "await_user":
                    if tool_decision.stop_reason:
                        turn_state.set_stop_reason(tool_decision.stop_reason)
                        emit_runtime_event(category="stop", message=tool_decision.assistant_content or result_output,
                                           emit_progress=False, stop_reason=tool_decision.stop_reason,
                                           evidence_summary=turn_state.latest_tool_result_summary)
                    if tool_decision.assistant_content and on_assistant_message:
                        on_assistant_message(tool_decision.assistant_content)
                    current_messages.append({"role": "assistant", "content": tool_decision.assistant_content or result_output})
                    return current_messages

            continue

        # while 正常退出（步数用尽）
        fallback = "Reached the maximum tool step limit for this turn."
        turn_state.set_stop_reason("max_steps")
        emit_runtime_event(category="stop", message=fallback, emit_progress=False, stop_reason="max_steps",
                           evidence_summary=turn_state.verification_state.evidence_summary or turn_state.latest_tool_result_summary)
        if on_assistant_message:
            on_assistant_message(fallback)
        current_messages.append({"role": "assistant", "content": fallback})
        return current_messages

    finally:
        # Coda: 收尾
        # 钩子 3：注入审查结果 + 沉淀
        if _review_hooks:
            _review_hooks.on_turn_end(current_messages)

        fire_hook_sync(HookEvent.AGENT_STOP, step=turn_state.step, tool_errors=turn_state.tool_error_count)

        if metrics_collector and metrics_collector._current_turn is not None:
            total_tokens = sum(estimate_message_tokens(m) for m in current_messages) if context_manager else 0
            metrics_collector.end_turn(total_tokens=total_tokens)

        context_usage = context_manager.get_stats().usage_percentage / 100.0 if context_manager else 0.0
        coda_summary = build_turn_coda_summary(turn_state=turn_state, context_usage=context_usage)

        if enable_work_chain and prelude.task:
            finalize_work_chain_task(
                task=prelude.task, auditor=prelude.auditor,
                coda_summary=coda_summary, success_outcome=DecisionOutcome.SUCCESS,
                failure_outcome=DecisionOutcome.FAILURE,
            )
            if prelude.task_graph and prelude.task_slot_key:
                try:
                    if coda_summary.task_state is TaskState.COMPLETED:
                        prelude.task_graph.complete_task(prelude.task_slot_key, result=prelude.task.result_summary)
                    elif coda_summary.task_state is TaskState.PAUSED:
                        slot = prelude.task_graph.slots.get(prelude.task_slot_key)
                        if slot is not None:
                            slot.state = GraphTaskState.QUEUED
                            slot.result = prelude.task.result_summary
                            prelude.task_graph.updated_at = time.time()
                    else:
                        prelude.task_graph.fail_task(prelude.task_slot_key, prelude.task.result_summary)
                except Exception:
                    logger.debug("TaskGraph finalization skipped", exc_info=True)
