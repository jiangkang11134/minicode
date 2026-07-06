"""决策审计模块 -- Agent 决策审计日志记录。

受显式决策记录原则启发:
- 所有 Agent 决策均被记录
- 决策链可追溯、可审计
- 支持决策回放和分析
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("decision_audit")


class DecisionType(str, Enum):
    """决策类型枚举。

    定义了系统中所有可能的决策类型，包括路由、工具选择、模型选择等。
    """
    ROUTING = "routing"
    TOOL_SELECTION = "tool_selection"
    MODEL_SELECTION = "model_selection"
    PERMISSION = "permission"
    MEMORY = "memory"
    CONTEXT = "context"
    RETRY = "retry"
    FALLBACK = "fallback"
    CUSTOM = "custom"


class DecisionOutcome(str, Enum):
    """决策结果枚举。

    表示决策执行后的结果状态：成功、失败、部分成功、跳过、被覆盖。
    """
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    OVERRIDDEN = "overridden"


@dataclass
class DecisionRecord:
    """单条决策记录数据类。

    包含决策的唯一标识、时间戳、类型、Agent ID、会话 ID、
    输入上下文、可选方案列表、推理过程、选择结果、置信度、
    执行结果、执行时长、错误信息等完整信息。
    支持通过 parent_decision_id 和 child_decisions 构建父子决策链。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    decision_type: DecisionType = DecisionType.CUSTOM
    agent_id: str = ""
    session_id: str = ""
    input_context: dict[str, Any] = field(default_factory=dict)
    available_options: list[str] = field(default_factory=list)
    reasoning: str = ""
    selected_option: str = ""
    confidence: float = 0.0
    outcome: DecisionOutcome = DecisionOutcome.SUCCESS
    execution_time_ms: float = 0.0
    result_summary: str = ""
    error_message: str = ""
    parent_decision_id: str = ""
    child_decisions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将决策记录转换为字典格式，用于 JSON 序列化。 """
        return {
            "id": self.id, "timestamp": self.timestamp,
            "decision_type": self.decision_type.value, "agent_id": self.agent_id,
            "session_id": self.session_id, "input_context": self.input_context,
            "available_options": self.available_options, "reasoning": self.reasoning,
            "selected_option": self.selected_option, "confidence": self.confidence,
            "outcome": self.outcome.value, "execution_time_ms": self.execution_time_ms,
            "result_summary": self.result_summary, "error_message": self.error_message,
            "parent_decision_id": self.parent_decision_id,
            "child_decisions": self.child_decisions,
        }


class DecisionAuditor:
    """决策审计器，管理所有决策记录的创建、更新、查询和持久化。

    维护全局记录列表、按会话分组的记录字典以及当前决策栈，
    支持决策链追踪、审计报告生成和会话级导出。
    """

    def __init__(self, log_dir: str | Path | None = None):
        """初始化决策审计器。

        参数:
            log_dir: 审计日志目录路径（可选）。默认为 ~/.mini-code/audit。
        """
        self.log_dir = Path(log_dir) if log_dir else Path.home() / ".mini-code" / "audit"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._records: list[DecisionRecord] = []
        self._session_records: dict[str, list[DecisionRecord]] = {}
        self._current_session: str = ""
        self._decision_stack: list[str] = []

    def start_session(self, session_id: str) -> None:
        """开始一个新的审计会话。清空当前决策栈。

        参数:
            session_id: 会话唯一标识符。
        """
        self._current_session = session_id
        self._decision_stack.clear()

    def record(self, decision_type: DecisionType, reasoning: str, selected_option: str,
               available_options: list[str] | None = None,
               input_context: dict[str, Any] | None = None,
               confidence: float = 0.0, parent_id: str | None = None) -> DecisionRecord:
        """创建并记录一个新的决策。

        自动处理父决策关联：如果提供了 parent_id 或当前决策栈非空，
        则新记录会被关联到对应的父记录上。记录创建后推入决策栈。

        参数:
            decision_type: 决策类型。
            reasoning: 决策推理过程描述。
            selected_option: 最终选择的选项。
            available_options: 决策时可供选择的选项列表（可选）。
            input_context: 决策时的输入上下文（可选）。
            confidence: 决策置信度 (0.0 ~ 1.0)，默认为 0.0。
            parent_id: 父决策 ID（可选），默认从决策栈顶部获取。

        返回:
            新创建的 DecisionRecord 实例。
        """
        record = DecisionRecord(
            decision_type=decision_type, agent_id="minicode",
            session_id=self._current_session,
            input_context=input_context or {},
            available_options=available_options or [],
            reasoning=reasoning, selected_option=selected_option,
            confidence=confidence,
            parent_decision_id=parent_id or (self._decision_stack[-1] if self._decision_stack else ""),
        )
        self._records.append(record)
        if self._current_session:
            if self._current_session not in self._session_records:
                self._session_records[self._current_session] = []
            self._session_records[self._current_session].append(record)
        if record.parent_decision_id:
            for r in self._records:
                if r.id == record.parent_decision_id:
                    r.child_decisions.append(record.id)
                    break
        self._decision_stack.append(record.id)
        return record

    def update_outcome(self, record_id: str, outcome: DecisionOutcome,
                       execution_time_ms: float = 0.0,
                       result_summary: str = "", error_message: str = "") -> bool:
        """更新指定决策记录的执行结果。

        记录完成后，其 ID 会从决策栈中移除。

        参数:
            record_id: 要更新的决策记录 ID。
            outcome: 决策结果枚举值。
            execution_time_ms: 执行时长（毫秒），默认为 0.0。
            result_summary: 结果摘要（可选）。
            error_message: 错误信息（可选），失败时填写。

        返回:
            如果找到并成功更新记录返回 True，否则返回 False。
        """
        for record in self._records:
            if record.id == record_id:
                record.outcome = outcome
                record.execution_time_ms = execution_time_ms
                record.result_summary = result_summary
                record.error_message = error_message
                if record_id in self._decision_stack:
                    self._decision_stack.remove(record_id)
                return True
        return False

    def complete_decision(self, outcome: DecisionOutcome = DecisionOutcome.SUCCESS,
                          execution_time_ms: float = 0.0,
                          result_summary: str = "", error_message: str = "") -> bool:
        """完成当前栈顶的决策。

        便捷方法，自动使用决策栈顶部的记录 ID 调用 update_outcome()。

        参数:
            outcome: 决策结果，默认为 SUCCESS。
            execution_time_ms: 执行时长（毫秒）。
            result_summary: 结果摘要。
            error_message: 错误信息。

        返回:
            如果成功更新返回 True，决策栈为空时返回 False。
        """
        if not self._decision_stack:
            return False
        record_id = self._decision_stack[-1]
        return self.update_outcome(record_id, outcome, execution_time_ms, result_summary, error_message)

    def get_session_decisions(self, session_id: str | None = None) -> list[DecisionRecord]:
        """获取指定会话（或当前会话）的所有决策记录。

        参数:
            session_id: 会话 ID（可选），默认为当前会话。

        返回:
            决策记录列表。
        """
        sid = session_id or self._current_session
        return list(self._session_records.get(sid, []))

    def get_decision_chain(self, record_id: str) -> list[DecisionRecord]:
        """获取指定决策的完整父链。

        通过 parent_decision_id 回溯至根决策，按时间正序排列。
        包含循环引用检测以防止无限循环。

        参数:
            record_id: 要追踪的决策记录 ID。

        返回:
            从根决策到当前决策的顺序记录列表。
        """
        chain: list[DecisionRecord] = []
        current_id = record_id
        visited: set[str] = set()
        while current_id:
            if current_id in visited:
                break
            visited.add(current_id)
            for record in self._records:
                if record.id == current_id:
                    chain.append(record)
                    current_id = record.parent_decision_id
                    break
            else:
                break
        chain.reverse()
        return chain

    def get_stats(self) -> dict[str, Any]:
        """计算所有决策记录的汇总统计信息。

        包括决策总数、会话数、按结果和类型分组的计数、
        平均执行时间和成功率。

        返回:
            统计信息字典。
        """
        if not self._records:
            return {"total_decisions": 0}
        outcomes = {}
        types = {}
        total_time = 0.0
        for record in self._records:
            outcomes[record.outcome.value] = outcomes.get(record.outcome.value, 0) + 1
            types[record.decision_type.value] = types.get(record.decision_type.value, 0) + 1
            total_time += record.execution_time_ms
        return {
            "total_decisions": len(self._records),
            "sessions": len(self._session_records),
            "outcomes": outcomes,
            "types": types,
            "avg_execution_time_ms": round(total_time / len(self._records), 2),
            "success_rate": round(outcomes.get("success", 0) / len(self._records) * 100, 1),
        }

    def save_session(self, session_id: str | None = None) -> Path:
        """将会话的决策记录导出并保存到 JSON 文件。

        文件命名格式: audit_{session_id}_{timestamp}.json

        参数:
            session_id: 会话 ID（可选），默认为当前会话。

        返回:
            保存的文件路径。如果无记录则返回空 Path。
        """
        sid = session_id or self._current_session
        if not sid:
            raise ValueError("No session ID provided")
        records = self._session_records.get(sid, [])
        if not records:
            return Path()
        filename = f"audit_{sid}_{int(time.time())}.json"
        filepath = self.log_dir / filename
        data = {
            "session_id": sid, "saved_at": time.time(),
            "stats": self.get_stats(),
            "records": [r.to_dict() for r in records],
        }
        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return filepath

    def export_report(self, session_id: str | None = None) -> str:
        """导出人类可读的审计报告文本。

        包含整体结果统计（按结果类型分组）和决策链摘要
        （仅展示前 5 个根决策及其子链）。

        参数:
            session_id: 会话 ID（可选），默认为当前会话。

        返回:
            格式化的报告字符串。
        """
        sid = session_id or self._current_session
        records = self._session_records.get(sid, self._records)
        if not records:
            return "No decisions recorded."
        lines = ["# Decision Audit Report", f"Session: {sid or 'all'}", f"Total Decisions: {len(records)}", "", "## Outcomes"]
        outcomes: dict[str, int] = {}
        for r in records:
            outcomes[r.outcome.value] = outcomes.get(r.outcome.value, 0) + 1
        for outcome, count in sorted(outcomes.items(), key=lambda x: -x[1]):
            lines.append(f"- {outcome}: {count}")
        lines.extend(["", "## Decision Chain"])
        root_records = [r for r in records if not r.parent_decision_id]
        for root in root_records[:5]:
            lines.append(f"\n### Decision {root.id}")
            lines.append(f"- Type: {root.decision_type.value}")
            lines.append(f"- Selected: {root.selected_option}")
            lines.append(f"- Reasoning: {root.reasoning[:100]}...")
            lines.append(f"- Outcome: {root.outcome.value}")
            if root.child_decisions:
                lines.append(f"- Sub-decisions: {len(root.child_decisions)}")
        return "\n".join(lines)

    def clear(self) -> None:
        """清除所有审计记录、会话分组和决策栈。 """
        self._records.clear()
        self._session_records.clear()
        self._decision_stack.clear()


_auditor: DecisionAuditor | None = None


def get_auditor() -> DecisionAuditor:
    """获取全局单例 DecisionAuditor 实例。

    采用延迟初始化，首次调用时创建。

    返回:
        全局唯一的 DecisionAuditor 实例。
    """
    global _auditor
    if _auditor is None:
        _auditor = DecisionAuditor()
    return _auditor


def audited(decision_type: DecisionType, option_extractor: str | None = None):
    """决策审计装饰器。

    自动包装函数执行过程，在函数调用前后创建和更新决策记录。
    被装饰函数每次调用都会创建一个决策记录，并在执行完成后
    根据是否抛出异常更新结果为 SUCCESS 或 FAILURE。

    支持通过 option_extractor 参数从 kwargs 中提取可选方案列表。

    参数:
        decision_type: 决策类型，用于分类审计记录。
        option_extractor: kwargs 中提取可选方案列表的参数名（可选）。

    返回:
        装饰器函数。
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            auditor = get_auditor()
            available = []
            if option_extractor and kwargs:
                available = kwargs.get(option_extractor, [])
            record = auditor.record(
                decision_type=decision_type,
                reasoning=f"Function: {func.__name__}",
                selected_option="pending",
                available_options=available,
                input_context={"args": str(args), "kwargs": str(kwargs)},
            )
            start = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = (time.time() - start) * 1000
                auditor.update_outcome(record.id, DecisionOutcome.SUCCESS, elapsed, str(result)[:200])
                return result
            except Exception as e:
                elapsed = (time.time() - start) * 1000
                auditor.update_outcome(record.id, DecisionOutcome.FAILURE, elapsed, error_message=str(e))
                raise
        return wrapper
    return decorator
