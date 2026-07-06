"""轮次内核模块。

定义 Agent 单次运行轮次（Turn）的数据结构、策略推导、决策逻辑以及结果汇总。
包含预算信号、验证状态、步骤策略、稳定任务包、轮次状态管理等核心组件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any, Callable, Literal

from minicode.layered_context import ContextBuilder, LayeredContext
from minicode.task_object import TaskObject, TaskState
from minicode.decision_audit import DecisionAuditor, DecisionOutcome
from minicode.types import RuntimeEventCategory

TurnStopReason = Literal[
    "done",
    "max_steps",
    "await_user",
    "blocked",
    "verification_failed",
    "widen_needed",
]

TurnStepPhase = Literal["explore", "execute", "verify"]


@dataclass(slots=True)
class TurnBudgetSignals:
    """轮次预算信号。

    记录当前轮次的剩余步数、是否达到最大步数、工具错误次数以及是否观察到工具结果。

    【为什么需要】为 step policy 和 agent_loop 提供步数预算决策依据。

    调用位置: 被 TurnRecurrentState._refresh_budget_signals() 内部刷新，被 agent_loop_lite.py Step A 读取。
    """
    remaining_steps: int | None = None  # 剩余步数（None 表示无限制）
    hit_max_steps: bool = False         # 是否已达到最大步数上限
    tool_error_count: int = 0           # 本轮累计的工具执行错误次数
    saw_tool_result: bool = False       # 本轮是否已观察到至少一个工具结果


@dataclass(slots=True)
class TurnVerificationState:
    """轮次验证状态。

    记录验证模式（严格/非严格）、是否需要显式最终答案、是否需要证据、
    证据准备状态、证据摘要及最后验证记录。

    【为什么需要】贯穿 agent_loop 全程，在 verification gate 阶段控制验证行为，决定是否要求模型提供证据后才放行。

    调用位置: 被 agent_loop_lite.py 全程使用，被 TurnRecurrentState 持有。
    """
    strict: bool = False                    # 是否启用严格验证模式
    requires_explicit_final: bool = False   # 是否需要模型显式声明 <final>
    requires_evidence: bool = False         # 是否需要模型引用证据才能 final
    evidence_ready: bool = False            # 是否有足够的证据可用
    evidence_summary: str = ""              # 证据摘要文本
    last_verification_note: str = ""        # 上次验证记录的备注


@dataclass(slots=True)
class TurnStepPolicy:
    """轮次步骤策略。

    定义当前步骤所处的阶段（探索/执行/验证）、阶段索引、剩余步数、
    引导信息、验证焦点、是否允许拓宽及拓宽状态等。

    【为什么需要】每步策略是 agent_loop Step A 的核心产出，指导后续所有决策（助手响应、工具执行、验证守卫）。

    调用位置: 被 derive_turn_step_policy() 构建，被 agent_loop_lite.py Step A 使用。
    """
    phase: TurnStepPhase = "explore"              # 当前阶段: explore/execute/verify
    phase_index: int = 0                          # 阶段内的步数索引
    remaining_steps: int | None = None            # 本轮剩余步数（None 表示无限制）
    guidance: str = ""                            # 给模型的引导提示文本
    verification_focus: str = "light"             # 验证严格程度: light/normal/strict
    allow_widening: bool = False                  # 是否允许触发拓宽模式
    widening_active: bool = False                 # 是否已处于拓宽模式
    widening_reason: str = ""                     # 触发拓宽的原因描述
    widening_evidence_summary: str = ""           # 触发拓宽时的证据摘要
    should_compact_aggressively: bool = False     # 是否应激进压缩上下文

    def terminal_summary(self) -> str:
        """生成终端可见的策略摘要字符串。

        将当前阶段、引导信息、拓宽状态等关键信息拼接为一行文本。

        【为什么需要】为 agent_loop 提供紧凑的策略日志输出，方便调试和监控。

        调用位置: 被 agent_loop_lite.py 调用（日志输出）。

        返回:
            策略摘要字符串
        """
        parts = [f"phase={self.phase}"]
        if self.guidance:
            parts.append(self.guidance)
        if self.allow_widening:
            if self.widening_reason:
                parts.append(
                    f"widening is now allowed because {self.widening_reason}"
                )
            else:
                parts.append("widening is now allowed if depth stalls")
        if self.widening_active:
            parts.append("widened mode is active")
        if self.should_compact_aggressively:
            parts.append("favor compact evidence over long narration")
        return " | ".join(parts)


@dataclass(slots=True)
class StableTaskPack:
    """稳定任务包。

    封装轮次中跨步骤保持稳定的任务上下文信息，包括任务标题、目标、描述、
    意图类型、任务图摘要、受保护上下文及各类总结报告。

    【为什么需要】为 Step A 提供跨步骤稳定的任务摘要，减少每步之间的上下文抖动，确保模型始终持有核心任务信息。

    调用位置: 被 build_stable_task_pack() 构建，被 agent_loop_lite.py Step A 读取。
    """
    task_title: str = ""                       # 任务标题
    task_goal: str = ""                        # 任务目标描述
    task_description: str = ""                 # 任务详细描述
    intent_type: str = ""                      # 意图类型（code/debug/refactor 等）
    action_type: str = ""                      # 动作类型（create/update/read 等）
    task_graph_summary: str = ""               # 任务图进度摘要
    protected_context: list[str] = field(default_factory=list)  # 受保护上下文列表（压缩时保留）
    latest_tool_result_summary: str = ""       # 最近一次工具结果的摘要
    progress_summary: str = ""                 # 当前进度摘要
    verification_summary: str = ""             # 验证状态摘要
    budget_summary: str = ""                   # 预算状态摘要

    def to_protected_text(self) -> str:
        """将稳定任务包转换为文本格式，用于注入受保护上下文。

        只输出非空字段，受保护上下文最多输出前 5 项，每项截断至 240 字符。

        【为什么需要】将 StableTaskPack 序列化为纯文本嵌入到系统提示中，确保任务信息不丢失。

        调用位置: 被 agent_loop_lite.py 调用。

        返回:
            格式化后的文本字符串
        """
        lines: list[str] = []
        if self.task_title:
            lines.append(f"Task: {self.task_title}")
        if self.task_goal:
            lines.append(f"Goal: {self.task_goal}")
        if self.task_description:
            lines.append(f"Description: {self.task_description}")
        if self.intent_type or self.action_type:
            lines.append(
                f"Intent: {self.intent_type or 'unknown'} / {self.action_type or 'unknown'}"
            )
        if self.task_graph_summary:
            lines.append(f"Task graph: {self.task_graph_summary}")
        if self.progress_summary:
            lines.append(f"Progress: {self.progress_summary}")
        if self.latest_tool_result_summary:
            lines.append(f"Latest tool result: {self.latest_tool_result_summary}")
        if self.verification_summary:
            lines.append(f"Verification: {self.verification_summary}")
        if self.budget_summary:
            lines.append(f"Budget: {self.budget_summary}")
        if self.protected_context:
            lines.append("Protected context:")
            for item in self.protected_context[:5]:
                lines.append(f"- {item[:240]}")
        return "\n".join(lines)


@dataclass(slots=True)
class TurnPreludeState:
    """轮次前奏状态。

    在循环开始前一次性准备好的构件，包括任务对象、元数据、分层上下文、审计器、任务图等。

    【为什么需要】一次性组件，避免在循环中反复解析任务元数据；所有字段在循环开始前准备完毕，循环中只读。

    调用位置: 被 agent_loop_lite.py 在循环开始前构建，仅供 Step A 读取。
    """
    task: TaskObject | None = None                                   # 从用户输入构建的任务对象
    task_metadata: dict[str, Any] = field(default_factory=dict)     # 任务元数据（意图类型、复杂度等）
    layered_context: LayeredContext | None = None                   # 分层上下文
    context_builder: ContextBuilder | None = None                   # 上下文构建器
    auditor: DecisionAuditor | None = None                          # 决策审计器
    task_graph: Any | None = None                                   # 任务图（追踪子任务进度）
    task_graph_id: str | None = None                                # 任务图中的当前任务 ID
    task_slot_key: str | None = None                                # 任务图槽位键名


@dataclass(slots=True)
class TurnRecurrentState:
    """轮次循环状态。

    单个 Agent 轮次中的可变循环状态，包含步数预算、重试计数、
    拓宽状态、验证状态、决策信号等。

    【为什么需要】agent_loop 的核心可变状态，所有步骤共享的读写上下文，
    每一步都会读取和更新此状态。

    调用位置: 被 agent_loop_lite.py 全程使用，贯穿 Step A/B/C/D。
    """
    max_steps: int | None                                           # 本轮最大步数（None 表示无限制）
    profile_name: str = "single"                                    # 运行配置名称（single/single-deep）
    widen_after_step: int | None = None                             # 到达第几步后可触发 widen
    empty_response_retry_limit: int = 2                            # 空响应最大重试次数
    recoverable_thinking_retry_limit: int = 3                      # 可恢复思考中断最大重试次数
    saw_tool_result: bool = False                                  # 本轮是否已看到工具结果
    empty_response_retry_count: int = 0                            # 当前空响应重试次数
    recoverable_thinking_retry_count: int = 0                      # 当前可恢复思考重试次数
    tool_error_count: int = 0                                      # 工具执行错误累计次数
    tool_observation_count: int = 0                                # 工具执行总次数
    successful_tool_observation_count: int = 0                     # 工具执行成功次数
    step: int = 0                                                  # 当前步数（从 0 开始，begin_step 后 +1）
    widening_active: bool = False                                  # 是否已激活拓宽模式
    widening_transition_count: int = 0                             # 拓宽模式切换次数
    widening_trigger_reason: str = ""                              # 触发拓宽的原因
    widening_trigger_evidence: str = ""                            # 触发拓宽时的证据
    latest_tool_result_summary: str = ""                           # 最近一次工具结果摘要
    progress_state: dict[str, Any] = field(default_factory=dict)   # 进度状态字典（含 summary 键）
    verification_state: TurnVerificationState = field(default_factory=TurnVerificationState)  # 验证状态
    budget_signals: TurnBudgetSignals = field(default_factory=TurnBudgetSignals)  # 预算信号
    stop_reason: TurnStopReason | None = None                      # 停止原因
    stable_task_pack: StableTaskPack | None = None                 # 当前稳定任务包
    step_policy: TurnStepPolicy = field(default_factory=TurnStepPolicy)  # 当前步骤策略

    def has_remaining_steps(self) -> bool:
        """检查是否还有剩余步数。

        当 max_steps 为 None（无限制）或当前步数小于上限时返回 True。

        【为什么需要】agent_loop 每步循环的终止条件之一。

        调用位置: 被 agent_loop_lite.py Step A 调用。

        返回:
            是否还有剩余步数
        """
        return self.max_steps is None or self.step < self.max_steps

    def begin_step(self) -> int:
        """开始新的一步。

        步数加 1，刷新预算信号，返回新的步数。

        【为什么需要】agent_loop 每轮循环的步数推进入口，确保预算信号同步更新。

        调用位置: 被 agent_loop_lite.py 循环顶部调用。

        返回:
            新的步数
        """
        self.step += 1
        self._refresh_budget_signals()
        return self.step

    def can_retry_empty_response(self) -> bool:
        """检查是否可以重试空响应。

        当前重试次数小于限制时返回 True。

        【为什么需要】防止模型连续返回空响应耗尽预算。

        调用位置: 被 agent_loop_lite.py Step B/C 调用。

        返回:
            是否允许重试空响应
        """
        return self.empty_response_retry_count < self.empty_response_retry_limit

    def record_empty_response_retry(self) -> None:
        """记录一次空响应重试。

        将空响应重试计数加 1。

        【为什么需要】配合 can_retry_empty_response 维护重试计数状态。

        调用位置: 被 decide_assistant_turn() 内部调用。

        参数:
            （无）
        """
        self.empty_response_retry_count += 1

    def can_retry_recoverable_thinking(self) -> bool:
        """检查是否可以重试可恢复的思考中断。

        当前重试次数小于限制时返回 True。

        【为什么需要】防止模型在 max_tokens/pause_turn 时无限重试。

        调用位置: 被 agent_loop_lite.py Step B 调用。

        返回:
            是否允许重试可恢复思考
        """
        return (
            self.recoverable_thinking_retry_count
            < self.recoverable_thinking_retry_limit
        )

    def record_recoverable_thinking_retry(self) -> None:
        """记录一次可恢复思考重试。

        将可恢复思考重试计数加 1。

        【为什么需要】配合 can_retry_recoverable_thinking 维护重试计数状态。

        调用位置: 被 decide_assistant_turn() 内部调用。
        """
        self.recoverable_thinking_retry_count += 1

    def record_tool_result(self, ok: bool, summary: str | None = None) -> None:
        """记录工具执行结果。

        更新工具观察计数、成功/失败计数、最新工具结果摘要，并刷新预算信号。

        【为什么需要】agent_loop Step D 的工具结果反馈入口，更新所有与工具相关的状态字段。

        调用位置: 被 agent_loop_lite.py Step D 调用。

        参数:
            ok: 工具是否成功执行
            summary: 可选的工具结果摘要，截断至 280 字符；同时更新验证状态的证据摘要
        """
        self.saw_tool_result = True
        self.tool_observation_count += 1
        if ok:
            self.successful_tool_observation_count += 1
        if not ok:
            self.tool_error_count += 1
        if summary:
            normalized = " ".join(summary.split())
            self.latest_tool_result_summary = normalized[:280]
            self.verification_state.evidence_summary = normalized[:200]
            self.verification_state.evidence_ready = True
        self._refresh_budget_signals()

    def set_progress_summary(self, summary: str) -> None:
        """设置进度摘要。

        将摘要存入 progress_state 字典，截断至 280 字符。

        【为什么需要】为 agent_loop 提供每步的进度记录，供 stable task pack 组装。

        调用位置: 被 agent_loop_lite.py Step D 调用。

        参数:
            summary: 进度摘要文本
        """
        self.progress_state["summary"] = summary[:280]

    def set_stop_reason(self, reason: TurnStopReason) -> None:
        """设置停止原因并刷新预算信号。

        【为什么需要】agent_loop 循环退出的触发点，决定最终任务状态。

        调用位置: 被 agent_loop_lite.py 调用。

        参数:
            reason: 轮次停止原因
        """
        self.stop_reason = reason
        self._refresh_budget_signals()

    def has_verification_evidence(self) -> bool:
        """检查是否拥有验证证据。

        当工具观察次数大于 0 且存在最新工具结果摘要时返回 True。

        【为什么需要】判断当前轮次是否具备进入验证阶段所需的证据基础。

        调用位置: 被 derive_turn_step_policy() 内部调用。

        返回:
            是否存在验证证据
        """
        return self.tool_observation_count > 0 and bool(self.latest_tool_result_summary)

    def activate_widening(self, *, extra_steps: int = 0) -> bool:
        """激活拓宽模式。

        如果已激活则返回 False；否则设置拓宽标志，重置重试计数，
        可选增加最大步数，刷新预算信号后返回 True。

        【为什么需要】让 agent_loop 从狭窄路径切换到更宽的搜索策略，避免卡死。

        调用位置: 被 agent_loop_lite.py 调用（widen 切换时）。

        参数:
            extra_steps: 激活拓宽时额外增加的最大步数

        返回:
            是否成功激活拓宽模式（False 表示已处于拓宽模式）
        """
        if self.widening_active:
            return False
        self.widening_active = True
        self.widening_transition_count += 1
        self.empty_response_retry_count = 0
        self.recoverable_thinking_retry_count = 0
        if extra_steps > 0 and self.max_steps is not None:
            self.max_steps += extra_steps
        self._refresh_budget_signals()
        return True

    def final_task_state(self) -> TaskState:
        """根据停止原因和工具错误数推导最终任务状态。

        "done" 且无错误时返回 COMPLETED，否则根据停止原因返回 PAUSED 或 FAILED。

        【为什么需要】Coda 阶段需要统一的最终状态推导逻辑，决定任务结果。

        调用位置: 被 build_turn_coda_summary() 内部调用。

        返回:
            最终任务状态
        """
        if self.stop_reason == "done":
            return (
                TaskState.COMPLETED
                if self.tool_error_count == 0
                else TaskState.FAILED
            )
        if self.stop_reason == "await_user":
            return TaskState.PAUSED
        if self.stop_reason in {
            "max_steps",
            "blocked",
            "verification_failed",
            "widen_needed",
        }:
            return TaskState.FAILED
        return TaskState.COMPLETED if self.tool_error_count == 0 else TaskState.FAILED

    def _refresh_budget_signals(self) -> None:
        """刷新预算信号。

        根据当前步数和上限重新计算剩余步数和是否达到上限，更新到 budget_signals 字段。

        【为什么需要】确保每步和每次状态变更后预算信号保持同步，供策略函数决策。

        调用位置: 被 begin_step()、record_tool_result()、set_stop_reason()、activate_widening() 内部调用。
        """
        remaining_steps = None
        hit_max_steps = False
        if self.max_steps is not None:
            remaining_steps = max(self.max_steps - self.step, 0)
            hit_max_steps = self.step >= self.max_steps
        self.budget_signals = TurnBudgetSignals(
            remaining_steps=remaining_steps,
            hit_max_steps=hit_max_steps,
            tool_error_count=self.tool_error_count,
            saw_tool_result=self.saw_tool_result,
        )


@dataclass(slots=True)
class AssistantTurnDecision:
    """助手轮次决策。

    循环中一次助手响应的结构化决策结果，包括决策类型、助手内容、用户内容、停止原因等。

    【为什么需要】decide_assistant_turn() 的返回类型，统一 agent_loop 的助手步决策接口，
    使调用方无需关心内部分支逻辑。

    调用位置: 被 decide_assistant_turn() 返回，被 agent_loop_lite.py Step C 消费。
    """
    kind: Literal["progress", "retry", "fallback", "final"]  # 决策类型
    assistant_content: str | None = None                     # 要追加到消息的助手内容
    user_content: str | None = None                          # 要追加到消息的用户内容（nudge）
    protect_final_answer: bool = False                       # 是否保护最终答案不被压缩删除
    stop_reason: TurnStopReason | None = None                # 停止原因
    runtime_event_category: RuntimeEventCategory | None = None  # 运行时事件类别


@dataclass(slots=True)
class ToolTurnDecision:
    """工具轮次决策。

    工具执行后的决策结果，包含是否继续循环或等待用户输入。

    【为什么需要】decide_tool_turn() 的返回类型，统一 agent_loop 的工具步决策接口。

    调用位置: 被 decide_tool_turn() 返回，被 agent_loop_lite.py Step D 消费。
    """
    kind: Literal["continue", "await_user"]  # 决策类型: continue=继续, await_user=等用户
    assistant_content: str | None = None     # 要追加的助手内容
    stop_reason: TurnStopReason | None = None  # 停止原因
    progress_summary: str = ""               # 进度摘要


@dataclass(slots=True)
class TurnCodaSummary:
    """轮次尾声摘要。

    轮次结束时的汇总信息，包括步数、工具错误数、成功率、结果摘要、
    错误率、平均延迟、上下文使用率等。

    【为什么需要】标准化轮次结束信息，供 Coda 阶段和审计器使用，
    将散落的状态字段聚合为一份统一的摘要。

    调用位置: 被 build_turn_coda_summary() 构建，被 agent_loop_lite.py Coda 使用。
    """
    step: int                        # 本轮总步数
    tool_error_count: int            # 工具错误总次数
    success: bool                    # 是否成功完成
    result_summary: str              # 结果摘要文本
    error_rate: float                # 错误率（错误数/步数）
    avg_latency: float               # 平均延迟（秒）
    context_usage: float             # 上下文使用率（0~1）
    task_state: TaskState            # 最终任务状态（COMPLETED/PAUSED/FAILED）
    stop_reason: TurnStopReason | None  # 停止原因


def _summarize_task_graph(task_graph: Any | None, task_slot_key: str | None) -> str:
    """汇总任务图的状态信息。

    返回包含进度百分比和槽状态的字符串。

    【为什么需要】为 stable task pack 提供任务图进度可视化的简洁摘要。

    调用位置: 被 build_stable_task_pack() 内部调用。

    参数:
        task_graph: 任务图对象，可为 None
        task_slot_key: 任务槽键名，可为 None

    返回:
        任务图状态摘要字符串，若 task_graph 为 None 则返回空字符串
    """
    if task_graph is None:
        return ""
    progress = 0.0
    try:
        progress = float(task_graph.get_progress_percentage())
    except Exception:
        progress = 0.0
    slot_state = ""
    if task_slot_key:
        try:
            slot = task_graph.slots.get(task_slot_key)
            if slot is not None and getattr(slot, "state", None) is not None:
                slot_state = getattr(slot.state, "value", str(slot.state))
        except Exception:
            slot_state = ""
    parts = [f"progress={progress:.0f}%"]
    if slot_state:
        parts.append(f"slot={slot_state}")
    return ", ".join(parts)


def _derive_widening_signal(
    turn_state: TurnRecurrentState,
    *,
    step: int,
) -> tuple[bool, str, str]:
    """推导是否需要触发拓宽模式。

    根据工具错误数、重试次数、证据状态等判断是否应切换到拓宽路径。
    如果已经处于拓宽模式则直接返回 (False, "", "")。

    【为什么需要】窄路径受阻时决定是否切换到更宽搜索空间，避免死循环。

    调用位置: 被 derive_turn_step_policy() 内部调用。

    ╔══ 流程图 ══╗

    输入: turn_state(TurnRecurrentState), step(int)
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 1. 检查是否已激活拓宽                  │
    │    widening_active → 返回 (False, "", "") │
    └───────────────────────────────────────┘
              │ (未激活)
              ▼
    ┌───────────────────────────────────────┐
    │ 2. 检查是否达到拓宽阈值                │
    │    step < widen_after_step → 返回 (False, "", "") │
    └───────────────────────────────────────┘
              │ (达到阈值)
              ▼
    ┌───────────────────────────────────────┐
    │ 3. 检查工具错误                       │
    │    tool_error_count > 0 → 返回 (True,   │
    │    "tool failures...", evidence)      │
    └───────────────────────────────────────┘
              │ (无工具错误)
              ▼
    ┌───────────────────────────────────────┐
    │ 4. 检查证据+停滞                      │
    │    has_verification_evidence() +     │
    │    (empty_retry > 0 或 thinking_retry > 0) │
    │    → 返回 (True, "narrow path...", evidence) │
    └───────────────────────────────────────┘
              │ (无证据或无停滞)
              ▼
    ┌───────────────────────────────────────┐
    │ 5. 检查无工具结果+重试耗尽             │
    │    !saw_tool_result +                 │
    │    empty_retry >= retry_limit         │
    │    → 返回 (True, "model stalled...", "") │
    └───────────────────────────────────────┘
              │ (其他情况)
              ▼
    ┌───────────────────────────────────────┐
    │ 6. 不触发拓宽                         │
    │    → 返回 (False, "", "")            │
    └───────────────────────────────────────┘
              │
              ▼
    输出: (bool, str, str) 三元组
    ╚═══════════════════════════╝

    参数:
        turn_state: 当前轮次状态
        step: 当前步数

    返回:
        (是否触发拓宽, 触发原因, 触发证据摘要) 的三元组
    """
    if turn_state.widening_active:
        return False, "", ""
    if turn_state.widen_after_step is None or step < turn_state.widen_after_step:
        return False, "", ""

    if turn_state.tool_error_count > 0:
        evidence = (
            turn_state.latest_tool_result_summary
            or f"{turn_state.tool_error_count} tool error(s) observed in this run"
        )
        return True, "tool failures already made the narrow path unstable", evidence[:200]

    if turn_state.has_verification_evidence() and (
        turn_state.empty_response_retry_count > 0
        or turn_state.recoverable_thinking_retry_count > 0
    ):
        evidence = (
            turn_state.latest_tool_result_summary
            or "the narrow path produced evidence but the next step still stalled"
        )
        return True, "the narrow path already produced evidence and then stalled", evidence[:200]

    if (
        not turn_state.saw_tool_result
        and turn_state.empty_response_retry_count >= turn_state.empty_response_retry_limit
    ):
        return (
            True,
            "the model stalled repeatedly before producing new evidence",
            (
                "assistant returned repeated empty responses while the turn stayed on "
                "the same narrow path"
            ),
        )

    if (
        not turn_state.saw_tool_result
        and turn_state.recoverable_thinking_retry_count
        >= turn_state.recoverable_thinking_retry_limit
    ):
        return (
            True,
            "recoverable pauses kept repeating on the same narrow path",
            (
                "the model kept hitting recoverable pause/max-token retries without "
                "producing fresh external evidence"
            ),
        )

    return False, "", ""


def derive_turn_step_policy(turn_state: TurnRecurrentState) -> TurnStepPolicy:
    """根据预算、配置文件和进度推导当前每步策略。

    【为什么需要】agent_loop Step A 的核心策略函数，决定每一步的阶段、引导和拓宽策略，
    是所有下游决策（助手响应、工具执行、验证守卫）的前提。

    调用位置: 被 agent_loop_lite.py Step A 调用。

    ╔══ 流程图 ══╗

    输入: turn_state(TurnRecurrentState)
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 1. 计算基础上下文                    │
    │    step = max(turn_state.step, 1)   │
    │    max_steps = turn_state.max_steps │
    │    remaining_steps = budget_signals │
    │    evidence_ready = has_verification_evidence() │
    └─────────────────────────────────────┘
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 2. 计算阶段切换阈值                  │
    │    verify_after = max(3, 0.7×max_steps)│
    │    strict模式: verify_after = min(4) │
    │    execute_after = 2(single-deep)或1 │
    └─────────────────────────────────────┘
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 3. 确定当前阶段(phase)              │
    │    widening_active + 非最后一步     │
    │    ├─→ phase = "execute"            │
    │    step ≤ execute_after             │
    │    ├─→ phase = "explore"            │
    │    step ≥ verify_after 或剩余≤2步    │
    │    ├─→ phase = "verify"             │
    │    其他                             │
    │    └─→ phase = "execute"            │
    └─────────────────────────────────────┘
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 4. 推导拓宽信号                     │
    │    调用 _derive_widening_signal()   │
    │    返回 (allow_widening, reason, evidence) │
    └─────────────────────────────────────┘
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 5. 设定引导信息与验证焦点            │
    │    widening_active → compare alternatives │
    │    explore → inspect, decompose     │
    │    execute → concrete tool use      │
    │    verify → verify changes          │
    └─────────────────────────────────────┘
              │
              ▼
    ┌─────────────────────────────────────┐
    │ 6. 构建 TurnStepPolicy 并更新状态   │
    │    设置 requires_explicit_final     │
    │    设置 requires_evidence           │
    │    设置 evidence_ready/summary      │
    │    生成 last_verification_note      │
    └─────────────────────────────────────┘
              │
              ▼
    输出: TurnStepPolicy 对象
    ╚═══════════════════════════╝

    参数:
        turn_state: 当前轮次状态

    返回:
        当前步骤的策略对象，同时更新 turn_state 中的策略相关字段
    """
    step = max(turn_state.step, 1)
    max_steps = turn_state.max_steps or 0
    remaining_steps = turn_state.budget_signals.remaining_steps
    evidence_ready = turn_state.has_verification_evidence()

    verify_after = max(3, ceil(max_steps * 0.7)) if max_steps else 6
    if turn_state.verification_state.strict:
        verify_after = min(verify_after, 4 if max_steps else 4)
    execute_after = 2 if turn_state.profile_name == "single-deep" else 1

    if turn_state.widening_active and not (
        remaining_steps is not None and remaining_steps <= 1
    ):
        phase: TurnStepPhase = "execute"
    elif step <= execute_after:
        phase: TurnStepPhase = "explore"
    elif (
        (max_steps and step >= verify_after)
        or (remaining_steps is not None and remaining_steps <= 2)
        or (
            turn_state.verification_state.strict
            and turn_state.saw_tool_result
            and step >= execute_after + 1
        )
    ):
        phase = "verify"
    else:
        phase = "execute"

    allow_widening, widening_reason, widening_evidence_summary = (
        _derive_widening_signal(turn_state, step=step)
    )

    if turn_state.widening_active:
        guidance = (
            "compare alternative approaches, reuse the evidence you already have, "
            "and avoid repeating the same narrow line of attack"
        )
        verification_focus = "normal"
    elif phase == "explore":
        guidance = "inspect, decompose, and anchor the task before committing"
        verification_focus = "light"
    elif phase == "execute":
        guidance = "prefer concrete tool use and incremental edits"
        verification_focus = "normal"
    else:
        guidance = "verify changes, test evidence, and finalize only with support"
        verification_focus = "strict" if turn_state.verification_state.strict else "normal"

    policy = TurnStepPolicy(
        phase=phase,
        phase_index=step,
        remaining_steps=remaining_steps,
        guidance=guidance,
        verification_focus=verification_focus,
        allow_widening=allow_widening,
        widening_active=turn_state.widening_active,
        widening_reason=widening_reason,
        widening_evidence_summary=widening_evidence_summary,
        should_compact_aggressively=(
            phase == "verify" or allow_widening or turn_state.widening_active
        ),
    )
    turn_state.step_policy = policy
    turn_state.widening_trigger_reason = widening_reason
    turn_state.widening_trigger_evidence = widening_evidence_summary
    turn_state.verification_state.requires_explicit_final = phase == "verify"
    turn_state.verification_state.requires_evidence = (
        phase == "verify"
        and turn_state.verification_state.strict
        and turn_state.saw_tool_result
    )
    turn_state.verification_state.evidence_ready = evidence_ready
    if evidence_ready and not turn_state.verification_state.evidence_summary:
        turn_state.verification_state.evidence_summary = turn_state.latest_tool_result_summary[:200]
    turn_state.verification_state.last_verification_note = (
        f"phase={phase}, verification={verification_focus}, "
        f"widening={'active' if turn_state.widening_active else ('ready' if allow_widening else 'hold')}, "
        f"evidence={'ready' if turn_state.verification_state.evidence_ready else 'missing'}"
    )
    if widening_reason:
        turn_state.verification_state.last_verification_note += (
            f", widening_reason={widening_reason}"
        )
    return policy


def render_turn_policy_message(
    *,
    previous_policy: TurnStepPolicy | None,
    current_policy: TurnStepPolicy,
) -> str | None:
    """当阶段发生有意义的变化时，返回紧凑的策略更新消息（终端可见）。

    如果阶段、拓宽状态等关键属性没有变化则返回 None，避免重复输出。

    【为什么需要】避免 agent_loop 每步重复输出相同策略信息，减少上下文噪音。

    调用位置: 被 agent_loop_lite.py Step A 调用。

    参数:
        previous_policy: 上一步的策略对象，可为 None
        current_policy: 当前步骤的策略对象

    返回:
        策略更新消息字符串，无需更新时返回 None
    """
    if previous_policy is not None:
        if (
            previous_policy.phase_index > 0
            and previous_policy.phase == current_policy.phase
            and previous_policy.allow_widening == current_policy.allow_widening
            and previous_policy.widening_active == current_policy.widening_active
        ):
            return None
    message = (
        f"Runtime phase: {current_policy.phase}. {current_policy.guidance} "
        f"(verification={current_policy.verification_focus}, "
        f"remaining_steps="
        f"{'open' if current_policy.remaining_steps is None else current_policy.remaining_steps})."
    )
    if current_policy.allow_widening:
        if current_policy.widening_reason:
            message += (
                " Widening is now available because "
                f"{current_policy.widening_reason}."
            )
        else:
            message += " Widening is now available if the current path keeps stalling."
    if current_policy.widening_active:
        message += " Widened mode is active."
    return message


def _step_aware_followup_nudge(
    *,
    step_policy: TurnStepPolicy | None,
    saw_tool_result: bool,
    nudge_continue: str,
    nudge_after_tool_result: str,
) -> str:
    """根据当前策略阶段生成步骤感知的后续提示文本。

    验证阶段返回验证模式提示，探索阶段且无工具结果时返回探索提示，
    其他情况根据 saw_tool_result 选择默认提示。

    【为什么需要】为 decide_assistant_turn 提供阶段感知的后续引导文本，
    确保模型提示与当前策略阶段匹配。

    调用位置: 被 decide_assistant_turn() 内部调用。

    参数:
        step_policy: 当前步骤策略，可为 None
        saw_tool_result: 本轮是否已观察到工具结果
        nudge_continue: 默认继续提示
        nudge_after_tool_result: 工具结果后的默认提示

    返回:
        步骤感知的提示文本
    """
    if step_policy is None:
        return nudge_after_tool_result if saw_tool_result else nudge_continue
    if step_policy.phase == "verify":
        return (
            "You are in verification mode. Use the current evidence to run the most "
            "relevant validation step, summarize the result, and only then finalize "
            "or explain the remaining blocker."
        )
    if step_policy.phase == "explore" and not saw_tool_result:
        return (
            "You are still in exploration mode. Inspect the most relevant files, "
            "tests, or symbols first so the next step is grounded in evidence."
        )
    return nudge_after_tool_result if saw_tool_result else nudge_continue


def _content_mentions_evidence(content: str, evidence_summary: str) -> bool:
    """检查助手内容是否引用了证据。

    通过关键词匹配和证据令牌重叠度判断助手响应是否基于已有证据。

    【为什么需要】在 verification guard 中判断模型回答是否基于已有证据，防止无依据的结论通过验证。

    调用位置: 被 decide_assistant_turn() 内部调用。

    参数:
        content: 助手响应的内容文本
        evidence_summary: 证据摘要文本

    返回:
        内容是否引用或提及了证据
    """
    normalized_content = " ".join(content.lower().split())
    if not normalized_content:
        return False
    evidence_markers = (
        "verified",
        "verification",
        "validated",
        "test",
        "tests",
        "checked",
        "inspected",
        "confirmed",
        "according to",
        "based on",
        "tool output",
        "output shows",
        "log shows",
        "diff shows",
        "I ran",
        "I checked",
    )
    if any(marker in normalized_content for marker in evidence_markers):
        return True
    evidence_tokens = [
        token.strip(".,:;()[]{}'\"")
        for token in evidence_summary.lower().split()
        if len(token.strip(".,:;()[]{}'\"")) >= 4
    ]
    overlap = sum(1 for token in set(evidence_tokens[:8]) if token and token in normalized_content)
    return overlap >= 2


def build_verification_evidence_nudge(evidence_summary: str) -> str:
    """构建严格验证模式下的证据提示文本。

    引导助手在最终确定前引用具体证据，如果已有证据摘要则附带引用，
    否则要求总结具体证据或说明阻塞原因。

    【为什么需要】在 verification guard 触发时向模型提供结构化的证据引用提示，
    确保最终答案有据可依。

    调用位置: 被 decide_assistant_turn() 内部调用。

    参数:
        evidence_summary: 可用的证据摘要文本

    返回:
        验证证据提示字符串
    """
    evidence_fragment = evidence_summary[:180].strip()
    if evidence_fragment:
        return (
            "You are in strict verification mode. Before finalizing, cite the strongest "
            f"evidence from this run, for example: {evidence_fragment}. If that evidence "
            "is insufficient, run one more validation step or state the exact blocker."
        )
    return (
        "You are in strict verification mode. Before finalizing, summarize the concrete "
        "evidence from this run or state the exact blocker. Do not end with an unsupported conclusion."
    )


def build_widening_transition_nudge(
    latest_tool_result_summary: str,
    *,
    widening_reason: str = "",
    widening_evidence_summary: str = "",
) -> str:
    """构建拓宽模式切换提示文本。

    引导助手从当前狭窄路径切换到更广泛的搜索策略，对比至少两种替代方案。

    【为什么需要】widen 时向模型提供清晰的策略切换指引，避免重复无效路径。

    调用位置: 被 agent_loop_lite.py 调用（widen 切换时）。

    参数:
        latest_tool_result_summary: 最新工具结果摘要
        widening_reason: 拓宽触发原因
        widening_evidence_summary: 拓宽触发证据摘要

    返回:
        拓宽切换提示字符串
    """
    evidence_fragment = (
        widening_evidence_summary[:180].strip()
        or latest_tool_result_summary[:180].strip()
    )
    reason_fragment = widening_reason.strip()
    if evidence_fragment:
        lead = (
            "Switch to widened mode because "
            f"{reason_fragment}. "
            if reason_fragment
            else "Switch to widened mode. "
        )
        return (
            lead
            + "Do not keep pushing the same narrow path. Compare at least two "
            "alternative approaches, reuse the strongest evidence already gathered, "
            f"and choose the next step grounded in this run: {evidence_fragment}"
        )
    if reason_fragment:
        return (
            "Switch to widened mode because "
            f"{reason_fragment}. Do not keep pushing the same narrow path. Compare "
            "at least two alternative approaches, inspect a different source of "
            "evidence, and then choose the most promising next step."
        )
    return (
        "Switch to widened mode. Do not keep pushing the same narrow path. Compare at least "
        "two alternative approaches, inspect a different source of evidence, and then choose "
        "the most promising next step."
    )


def build_stable_task_pack(
    *,
    task: TaskObject | None,
    task_metadata: dict[str, Any] | None,
    protected_context: list[str] | None,
    task_graph: Any | None,
    task_slot_key: str | None,
    latest_tool_result_summary: str,
    progress_state: dict[str, Any] | None,
    verification_state: TurnVerificationState | None,
    budget_signals: TurnBudgetSignals | None,
) -> StableTaskPack | None:
    """构建稳定任务包。

    从任务对象、元数据、受保护上下文、任务图等来源组装 StableTaskPack 实例。
    如果任务对象、受保护上下文和工具结果摘要均为空则返回 None。

    【为什么需要】Step A 中整合散落的任务信息为稳定的上下文包，
    减少跨步骤的上下文不一致性。

    调用位置: 被 agent_loop_lite.py Step A 调用。

    ╔══ 流程图 ══╗

    输入: task, task_metadata, protected_context, task_graph, ...
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 1. 空检查                             │
    │    task=None 且无 protected_context   │
    │    且无 latest_tool_result_summary    │
    │    └─→ 返回 None                     │
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 2. 提取进度摘要                        │
    │    progress_state["summary"]          │
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 3. 构建验证摘要                        │
    │    收集 strict/explicit-final/        │
    │    evidence-required/evidence-ready   │
    │    等状态标签                          │
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 4. 构建预算摘要                        │
    │    remaining_steps / tool_errors /    │
    │    saw_tool_result                    │
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 5. 组装 StableTaskPack 对象            │
    │    整合所有字段，返回完整对象           │
    └───────────────────────────────────────┘
              │
              ▼
    输出: StableTaskPack 对象
    ╚═══════════════════════════╝

    参数:
        task: 任务对象，可为 None
        task_metadata: 任务元数据字典
        protected_context: 受保护上下文列表
        task_graph: 任务图对象
        task_slot_key: 任务槽键名
        latest_tool_result_summary: 最新工具结果摘要
        progress_state: 进度状态字典
        verification_state: 验证状态对象
        budget_signals: 预算信号对象

    返回:
        组装后的 StableTaskPack 实例，无可用数据时返回 None
    """
    if task is None and not protected_context and not latest_tool_result_summary:
        return None

    metadata = task_metadata or {}
    progress_summary = ""
    if progress_state:
        progress_summary = str(progress_state.get("summary", ""))

    verification_summary = ""
    if verification_state:
        verification_parts = []
        if verification_state.strict:
            verification_parts.append("strict")
        if verification_state.requires_explicit_final:
            verification_parts.append("explicit-final")
        if verification_state.requires_evidence:
            verification_parts.append("evidence-required")
        if verification_state.evidence_ready:
            verification_parts.append("evidence-ready")
        if verification_state.evidence_summary:
            verification_parts.append(f"evidence={verification_state.evidence_summary[:120]}")
        if verification_state.last_verification_note:
            verification_parts.append(verification_state.last_verification_note)
        verification_summary = ", ".join(verification_parts)

    budget_summary = ""
    if budget_signals:
        remaining = (
            "open"
            if budget_signals.remaining_steps is None
            else str(budget_signals.remaining_steps)
        )
        budget_summary = (
            f"remaining_steps={remaining}, "
            f"tool_errors={budget_signals.tool_error_count}, "
            f"saw_tool_result={budget_signals.saw_tool_result}"
        )

    return StableTaskPack(
        task_title=str(getattr(task, "title", "") or ""),
        task_goal=str(getattr(task, "goal", "") or ""),
        task_description=str(getattr(task, "description", "") or ""),
        intent_type=str(metadata.get("intent_type", "") or ""),
        action_type=str(metadata.get("action_type", "") or ""),
        task_graph_summary=_summarize_task_graph(task_graph, task_slot_key),
        protected_context=list(protected_context or []),
        latest_tool_result_summary=latest_tool_result_summary,
        progress_summary=progress_summary,
        verification_summary=verification_summary,
        budget_summary=budget_summary,
    )


def build_turn_coda_summary(
    *,
    turn_state: TurnRecurrentState,
    context_usage: float,
) -> TurnCodaSummary:
    """构建标准化的轮次摘要，用于尾声/结束逻辑。

    根据停止原因和步数生成合适的结果摘要文本，并计算错误率等指标。

    【为什么需要】Coda 阶段汇总轮次关键指标，供审计和用户展示。

    调用位置: 被 agent_loop_lite.py Coda 调用。

    ╔══ 流程图 ══╗

    输入: turn_state(TurnRecurrentState), context_usage(float)
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 1. 推导最终任务状态                     │
    │    调用 turn_state.final_task_state() │
    │    返回 COMPLETED / PAUSED / FAILED   │
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 2. 判断任务是否成功                    │
    │    success = (task_state == COMPLETED)│
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 3. 根据停止原因生成结果摘要             │
    │    await_user → "Turn paused..."      │
    │    max_steps → "Turn stopped at max..."│
    │    其他 → "Turn finished with stop_reason=..."│
    └───────────────────────────────────────┘
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 4. 计算指标并组装 TurnCodaSummary      │
    │    error_rate = errors / steps        │
    │    avg_latency = steps × 2.0          │
    │    整合所有字段                        │
    └───────────────────────────────────────┘
              │
              ▼
    输出: TurnCodaSummary 对象
    ╚═══════════════════════════╝

    参数:
        turn_state: 当前轮次状态
        context_usage: 上下文使用率

    返回:
        轮次尾声摘要对象
    """
    task_state = turn_state.final_task_state()
    success = task_state is TaskState.COMPLETED
    if turn_state.stop_reason == "await_user":
        result_summary = (
            f"Turn paused after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    elif turn_state.stop_reason == "max_steps":
        result_summary = (
            f"Turn stopped at the max step budget after {turn_state.step} steps, "
            f"{turn_state.tool_error_count} errors"
        )
    else:
        result_summary = (
            f"Turn finished with stop_reason={turn_state.stop_reason or 'implicit'}, "
            f"{turn_state.step} steps, {turn_state.tool_error_count} errors"
        )
    return TurnCodaSummary(
        step=turn_state.step,
        tool_error_count=turn_state.tool_error_count,
        success=success,
        result_summary=result_summary,
        error_rate=turn_state.tool_error_count / max(turn_state.step, 1),
        avg_latency=turn_state.step * 2.0,
        context_usage=context_usage,
        task_state=task_state,
        stop_reason=turn_state.stop_reason,
    )


def finalize_work_chain_task(
    *,
    task: TaskObject | None,
    auditor: DecisionAuditor | None,
    coda_summary: TurnCodaSummary,
    success_outcome: DecisionOutcome,
    failure_outcome: DecisionOutcome,
) -> None:
    """在尾声阶段应用最终任务状态并完成审计记录。

    设置任务状态和结果摘要，并通过审计器记录成功或失败的决策。

    【为什么需要】Coda 阶段持久化任务状态和审计结果，完成一轮生命周期。

    调用位置: 被 agent_loop_lite.py Coda 调用。

    ╔══ 流程图 ══╗

    输入: task, auditor, coda_summary, success_outcome, failure_outcome
              │
              ▼
    ┌───────────────────────────────────────┐
    │ 1. 空任务检查                          │
    │    task is None → 直接返回             │
    └───────────────────────────────────────┘
          │ (task 不为 None)
          ▼
    ┌───────────────────────────────────────┐
    │ 2. 更新任务状态                        │
    │    task.set_state(coda_summary.task_state) │
    │    task.result_summary = coda_summary.result_summary │
    └───────────────────────────────────────┘
          │
          ▼
    ┌───────────────────────────────────────┐
    │ 3. 审计器检查                          │
    │    auditor is None → 直接返回          │
    └───────────────────────────────────────┘
          │ (auditor 不为 None)
          ▼
    ┌───────────────────────────────────────┐
    │ 4. 记录审计决策                        │
    │    auditor.complete_decision(         │
    │        outcome: success/failure       │
    │        cost: steps × 100              │
    │        summary: result_summary        │
    │        error: error_message           │
    │    )                                  │
    └───────────────────────────────────────┘
          │
          ▼
    输出: None
    ╚═══════════════════════════╝

    参数:
        task: 任务对象，可为 None（此时直接返回）
        auditor: 审计器对象，可为 None（此时只设置任务状态）
        coda_summary: 轮次尾声摘要
        success_outcome: 成功时使用的审计结果对象
        failure_outcome: 失败时使用的审计结果对象
    """
    if task is None:
        return

    task.set_state(coda_summary.task_state)
    task.result_summary = coda_summary.result_summary

    if auditor is None:
        return

    auditor.complete_decision(
        success_outcome if coda_summary.success else failure_outcome,
        coda_summary.step * 100.0,
        task.result_summary,
        task.error_message if not coda_summary.success else "",
    )


def decide_assistant_turn(
    *,
    turn_state: TurnRecurrentState,
    step_content: str,
    step_kind: str | None,
    stop_reason: str | None,
    block_types: list[str] | None,
    ignored_block_types: list[str] | None,
    is_empty: bool,
    treat_as_progress: bool,
    is_recoverable_thinking_stop: bool,
    format_diagnostics: Callable[[str | None, list[str] | None, list[str] | None], str],
    nudge_continue: str,
    nudge_after_tool_result: str,
    resume_after_pause: str,
    resume_after_max_tokens: str,
    nudge_after_empty_response: str,
    nudge_after_empty_no_tools: str,
    step_policy: TurnStepPolicy | None = None,
) -> AssistantTurnDecision:
    """决定循环如何响应纯助手步骤（无工具调用）。

    【为什么需要】agent_loop Step C 的核心决策函数，处理进度/重试/fallback/验证守卫等多种路径，
    是助手响应路由的唯一入口。

    调用位置: 被 agent_loop_lite.py Step C 调用。

    ╔══ 完整执行流程 ══╗

    第1步: 检查是否视为进度（treat_as_progress）
      └─ 是 → 返回 kind="progress"，附带步骤感知的后续提示

    第2步: 检查可恢复思考中断
      ├─ is_recoverable_thinking_stop 且 can_retry_recoverable_thinking()
      │   ├─ 记录可恢复思考重试
      │   ├─ stop_reason == "max_tokens" → progress_content 提示 max_tokens
      │   ├─ stop_reason == "pause_turn" → progress_content 提示 pause_turn
      │   └─ 返回 kind="progress"，runtime_event_category="recovery"
      └─ 否则 → 进入第3步

    第3步: 检查空响应且有重试次数
      ├─ is_empty 且 can_retry_empty_response()
      │   ├─ 记录空响应重试
      │   └─ 返回 kind="retry"，引导文本按策略分支：
      │       ├─ verify 阶段 → 验证模式空响应提示
      │       ├─ allow_widening 就绪 → 拓宽比较提示
      │       ├─ saw_tool_result → nudge_after_empty_response
      │       └─ 无工具结果 → nudge_after_empty_no_tools
      └─ 否则 → 进入第4步

    第4步: 空响应但重试次数已耗尽
      ├─ 确定 late_verify（verify 阶段且 requires_explicit_final）
      ├─ 确定 widen_ready（allow_widening 为 True）
      ├─ 诊断后缀 = format_diagnostics(stop_reason, block_types, ignored_block_types)
      ├─ 构建 fallback 文本：
      │   ├─ saw_tool_result + tool_error_count > 0 → 工具错误 fallback
      │   ├─ saw_tool_result → 空值工具结果 fallback
      │   └─ 无工具结果 → 纯空响应 fallback
      ├─ 确定停止原因 (typed_stop_reason)：
      │   ├─ late_verify + saw_tool_result → "verification_failed"
      │   ├─ widen_ready → "widen_needed"
      │   └─ 其他 → "blocked"
      └─ 返回 kind="fallback"

    第5步: 验证守卫（verify + requires_evidence + 内容未引用证据）
      └─ 是 → 返回 kind="progress"，runtime_event_category="guard"
          └─ 附带 build_verification_evidence_nudge 生成的证据提示

    第6步: 默认结果（非空、非恢复、非验证守卫）
      └─ 返回 kind="final"，protect_final_answer=True，stop_reason="done"
    ╚══════════════════════╝

    参数:
        turn_state: 当前轮次状态
        step_content: 步骤内容文本
        step_kind: 步骤类型
        stop_reason: 停止原因
        block_types: 阻塞类型列表
        ignored_block_types: 被忽略的阻塞类型列表
        is_empty: 是否为空响应
        treat_as_progress: 是否视为进度步骤
        is_recoverable_thinking_stop: 是否为可恢复的思考中断
        format_diagnostics: 格式化诊断信息的回调函数
        nudge_continue: 默认继续提示
        nudge_after_tool_result: 工具结果后的默认提示
        resume_after_pause: 暂停后恢复的提示
        resume_after_max_tokens: 达到最大令牌后恢复的提示
        nudge_after_empty_response: 空响应后的提示（有工具结果时）
        nudge_after_empty_no_tools: 空响应后的提示（无工具结果时）
        step_policy: 当前步骤策略

    返回:
        助手轮次决策对象
    """
    if treat_as_progress:
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=step_content,
            user_content=_step_aware_followup_nudge(
                step_policy=step_policy,
                saw_tool_result=turn_state.saw_tool_result and step_kind != "progress",
                nudge_continue=nudge_continue,
                nudge_after_tool_result=nudge_after_tool_result,
            ),
        )

    if is_recoverable_thinking_stop and turn_state.can_retry_recoverable_thinking():
        turn_state.record_recoverable_thinking_retry()
        progress_content = (
            "Model hit max_tokens during thinking; requesting the next step."
            if stop_reason == "max_tokens"
            else "Model returned pause_turn; requesting the next step."
        )
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=progress_content,
            user_content=(
                resume_after_pause
                if stop_reason == "pause_turn"
                else resume_after_max_tokens
            ),
            runtime_event_category="recovery",
        )

    if is_empty and turn_state.can_retry_empty_response():
        turn_state.record_empty_response_retry()
        retry_nudge = (
            "Your last response was empty during verification mode. Resume with a "
            "single concrete validation step or state the exact blocker."
            if step_policy is not None and step_policy.phase == "verify"
            else (
                "Your last response was empty after the current line of attack stalled. "
                "Resume with one wider search step or explicitly compare the next two options."
                if step_policy is not None and step_policy.allow_widening
                else (
                    nudge_after_empty_response
                    if turn_state.saw_tool_result
                    else nudge_after_empty_no_tools
                )
            )
        )
        return AssistantTurnDecision(
            kind="retry",
            user_content=retry_nudge,
        )

    if is_empty:
        late_verify = bool(
            step_policy is not None
            and step_policy.phase == "verify"
            and turn_state.verification_state.requires_explicit_final
        )
        widen_ready = bool(step_policy is not None and step_policy.allow_widening)
        diagnostics_suffix = format_diagnostics(
            stop_reason,
            block_types,
            ignored_block_types,
        )
        if turn_state.saw_tool_result:
            fallback = (
                "Model returned an empty response after tool execution and the turn "
                "was stopped. There were "
                f"{turn_state.tool_error_count} tool error(s); retry, adjust the "
                f"command, or choose a different approach.{diagnostics_suffix}"
                if turn_state.tool_error_count > 0
                else "Model returned an empty response after tool execution and the "
                "turn was stopped. Retry or ask the model to continue the remaining "
                f"steps.{diagnostics_suffix}"
            )
        else:
            fallback = (
                "Model returned an empty response and the turn was stopped."
                f"{diagnostics_suffix}"
            )
        typed_stop_reason: TurnStopReason = "blocked"
        if late_verify and turn_state.saw_tool_result:
            typed_stop_reason = "verification_failed"
            fallback += (
                " The turn had already shifted into verification mode, so this run "
                "ended as a verification failure rather than an ordinary block."
            )
        elif widen_ready:
            typed_stop_reason = "widen_needed"
            fallback += (
                " Depth stopped paying off after repeated pressure, so a wider search "
                "or handoff is now justified."
            )
        return AssistantTurnDecision(
            kind="fallback",
            assistant_content=fallback,
            stop_reason=typed_stop_reason,
        )

    if (
        step_policy is not None
        and step_policy.phase == "verify"
        and turn_state.verification_state.requires_evidence
        and not _content_mentions_evidence(
            step_content,
            turn_state.verification_state.evidence_summary or turn_state.latest_tool_result_summary,
        )
    ):
        return AssistantTurnDecision(
            kind="progress",
            assistant_content=(
                "Verification guard: final answer withheld until it cites concrete "
                "evidence from this run."
            ),
            user_content=build_verification_evidence_nudge(
                turn_state.verification_state.evidence_summary
                or turn_state.latest_tool_result_summary
            ),
            runtime_event_category="guard",
        )

    return AssistantTurnDecision(
        kind="final",
        assistant_content=step_content,
        protect_final_answer=True,
        stop_reason="done",
    )


def decide_tool_turn(
    *,
    tool_name: str,
    result_output: str,
    await_user: bool,
) -> ToolTurnDecision:
    """在统一的类型化决策接口上处理工具结果及用户询问。

    【为什么需要】agent_loop Step D 的核心决策函数，决定工具结果后继续循环还是等待用户。

    调用位置: 被 agent_loop_lite.py Step D 调用。

    ╔══ 完整执行流程 ══╗

    第1步: 检查是否需要等待用户输入
      ├─ await_user == True
      │   ├─ assistant_content = result_output
      │   ├─ stop_reason = "await_user"
      │   └─ progress_summary = "awaiting user after {tool_name}"
      │   → 返回 kind="await_user"
      └─ await_user == False
          └─ progress_summary = "processed tool result from {tool_name}"
          → 返回 kind="continue"
    ╚══════════════════════╝

    参数:
        tool_name: 工具名称
        result_output: 工具执行结果输出
        await_user: 是否需要等待用户输入

    返回:
        工具轮次决策对象
    """
    if await_user:
        return ToolTurnDecision(
            kind="await_user",
            assistant_content=result_output,
            stop_reason="await_user",
            progress_summary=f"awaiting user after {tool_name}",
        )
    return ToolTurnDecision(
        kind="continue",
        progress_summary=f"processed tool result from {tool_name}",
    )
