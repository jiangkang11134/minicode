"""Task Object - 稳定的任务表示层。

深化工作链：
  原始输入 -> 意图解析 -> 任务对象 -> 管道 -> 执行 -> 结果

该模块定义了核心的任务数据结构（TaskObject）及其构建器（TaskBuilder），
以及约束条件（Constraint）和预期输出（ExpectedOutput）等辅助类型。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.intent_parser import ParsedIntent
from minicode.logging_config import get_logger

logger = get_logger("task_object")


class TaskState(str, Enum):
    """任务生命周期状态枚举。"""
    DRAFT = "draft"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConstraintType(str, Enum):
    """约束条件类型枚举，定义任务执行时必须遵守的规则。"""
    MUST_INCLUDE = "must_include"
    MUST_NOT_MODIFY = "must_not_modify"
    MAX_TOKENS = "max_tokens"
    TIMEOUT = "timeout"
    REQUIRES_REVIEW = "requires_review"
    TEST_REQUIRED = "test_required"
    BACKUP_REQUIRED = "backup_required"


@dataclass
class Constraint:
    """约束条件，定义任务执行时需要遵守的规则。

    例如：必须包含某些内容、不能修改某些文件、需要测试等。
    """

    type: ConstraintType
    target: str = ""
    value: Any = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """将约束条件序列化为字典。

        返回:
            包含 type、target、value、reason 的字典
        """
        return {"type": self.type.value, "target": self.target,
                "value": self.value, "reason": self.reason}


@dataclass
class ExpectedOutput:
    """任务的预期输出描述。

    定义任务完成后应该产出的内容类型、路径、格式和验证方式。
    """

    type: str = ""
    path: str = ""
    format: str = ""
    validation: str = ""
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将预期输出序列化为字典。

        返回:
            包含 type、path、format、validation、examples 的字典
        """
        return {"type": self.type, "path": self.path, "format": self.format,
                "validation": self.validation, "examples": self.examples}


@dataclass
class TaskObject:
    """核心任务对象，表示一个从意图解析得到的可执行任务。

    包含任务的完整信息：原始输入、解析后的意图、标题、描述、
    目标、相关文件/代码、约束条件、预期输出、状态等。
    是任务执行管道中的核心数据载体。
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    raw_input: str = ""
    parsed_intent: ParsedIntent | None = None
    title: str = ""
    description: str = ""
    goal: str = ""
    relevant_files: list[str] = field(default_factory=list)
    relevant_code: list[str] = field(default_factory=list)
    context_notes: list[str] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    expected_outputs: list[ExpectedOutput] = field(default_factory=list)
    state: TaskState = TaskState.DRAFT
    plan_id: str = ""
    result_summary: str = ""
    error_message: str = ""
    tags: list[str] = field(default_factory=list)
    priority: int = 0
    estimated_effort: str = "moderate"

    def to_dict(self) -> dict[str, Any]:
        """将任务对象序列化为字典。

        返回:
            包含所有字段的字典，嵌套对象也会递归序列化
        """
        return {
            "id": self.id, "created_at": self.created_at, "updated_at": self.updated_at,
            "raw_input": self.raw_input,
            "parsed_intent": self.parsed_intent.to_dict() if self.parsed_intent else None,
            "title": self.title, "description": self.description, "goal": self.goal,
            "relevant_files": self.relevant_files, "relevant_code": self.relevant_code,
            "context_notes": self.context_notes,
            "constraints": [c.to_dict() for c in self.constraints],
            "expected_outputs": [o.to_dict() for o in self.expected_outputs],
            "state": self.state.value, "plan_id": self.plan_id,
            "result_summary": self.result_summary, "error_message": self.error_message,
            "tags": self.tags, "priority": self.priority,
            "estimated_effort": self.estimated_effort,
        }

    def add_constraint(self, type: ConstraintType, target: str = "", value: Any = None, reason: str = "") -> None:
        """添加一条约束条件。

        参数:
            type: 约束类型
            target: 约束的目标对象（如文件路径、函数名）
            value: 约束的值
            reason: 添加约束的原因说明
        """
        self.constraints.append(Constraint(type=type, target=target, value=value, reason=reason))
        self.updated_at = time.time()

    def add_expected_output(self, type: str, path: str = "", format: str = "", validation: str = "") -> None:
        """添加一条预期输出定义。

        参数:
            type: 输出类型（如 "code_block"、"explanation"）
            path: 输出文件路径
            format: 输出格式描述
            validation: 验证方式描述
        """
        self.expected_outputs.append(ExpectedOutput(type=type, path=path, format=format, validation=validation))
        self.updated_at = time.time()

    def set_state(self, state: TaskState) -> None:
        """更新任务状态。

        参数:
            state: 新的任务状态
        """
        self.state = state
        self.updated_at = time.time()

    def is_read_only(self) -> bool:
        """判断任务是否为只读操作（不需要修改代码）。

        返回:
            如果任务仅为查看/解释等只读操作返回 True
        """
        return self.parsed_intent.is_read_only() if self.parsed_intent else False

    def requires_write(self) -> bool:
        """判断任务是否需要写操作（修改代码）。

        返回:
            如果需要修改代码返回 True
        """
        return not self.is_read_only()


class TaskBuilder:
    """任务构建器，将 ParsedIntent 转换为完整的 TaskObject。

    负责从解析后的意图自动生成标题、描述、目标、优先级、
    标签、默认约束和预期输出。
    """

    def build(self, intent: ParsedIntent, raw_input: str = "") -> TaskObject:
        """从解析后的意图构建一个完整的 TaskObject。

        参数:
            intent: 解析后的用户意图
            raw_input: 原始用户输入文本，如果为空则使用 intent 中的 raw_input

        返回:
            构建完成的 TaskObject 实例
        """
        task = TaskObject(raw_input=raw_input or intent.raw_input, parsed_intent=intent)
        task.title = self._generate_title(intent)
        task.goal = self._generate_goal(intent)
        task.description = self._generate_description(intent)
        task.relevant_files = intent.entities.get("files", [])
        task.estimated_effort = intent.complexity_hint
        task.priority = self._calculate_priority(intent)
        task.tags = [intent.intent_type.value, intent.action_type.value] + intent.keywords[:3]
        self._add_default_constraints(task, intent)
        self._add_expected_outputs(task, intent)
        logger.debug("Built TaskObject %s: %s", task.id, task.title)
        return task

    def _generate_title(self, intent: ParsedIntent) -> str:
        """根据意图生成任务标题。

        格式: "动作类型 意图类型: 前3个关键词"

        参数:
            intent: 解析后的意图

        返回:
            生成的标题字符串
        """
        return f"{intent.action_type.value} {intent.intent_type.value}: {' '.join(intent.keywords[:3])}".strip()

    def _generate_goal(self, intent: ParsedIntent) -> str:
        """根据意图生成任务目标，取原始输入的前120个字符。

        参数:
            intent: 解析后的意图

        返回:
            目标描述字符串
        """
        return intent.raw_input[:120]

    def _generate_description(self, intent: ParsedIntent) -> str:
        """根据意图生成任务描述，包含意图类型、置信度和相关实体。

        参数:
            intent: 解析后的意图

        返回:
            多行描述字符串
        """
        lines = [f"Intent: {intent.intent_type.value} / {intent.action_type.value}",
                 f"Confidence: {intent.confidence:.2f}"]
        for key in ("files", "functions", "classes"):
            if intent.entities.get(key):
                lines.append(f"{key.capitalize()}: {', '.join(intent.entities[key])}")
        return "\n".join(lines)

    def _calculate_priority(self, intent: ParsedIntent) -> int:
        """根据意图计算数值优先级（0-100）。

        基础分为 50。调试/系统类型加 20，复杂任务加 10，
        低置信度减 10，最终值限制在 0-100 之间。

        参数:
            intent: 解析后的意图

        返回:
            0-100 的优先级分数
        """
        base = 50
        if intent.intent_type.value in ("debug", "system"):
            base += 20
        if intent.complexity_hint == "complex":
            base += 10
        if intent.confidence < 0.5:
            base -= 10
        return max(0, min(100, base))

    def _add_default_constraints(self, task: TaskObject, intent: ParsedIntent) -> None:
        """根据意图类型添加默认的约束条件。

        - 只读操作：添加 MUST_NOT_MODIFY 约束
        - 创建/更新代码：添加 TEST_REQUIRED 约束
        - 删除/更新操作：添加 BACKUP_REQUIRED 约束

        参数:
            task: 要添加约束的任务对象
            intent: 解析后的意图
        """
        if intent.is_read_only():
            task.add_constraint(ConstraintType.MUST_NOT_MODIFY, reason="Read-only intent")
        if intent.is_code_related() and intent.action_type.value in ("create", "update"):
            task.add_constraint(ConstraintType.TEST_REQUIRED, reason="Code modification requires tests")
        if intent.action_type.value in ("delete", "update"):
            task.add_constraint(ConstraintType.BACKUP_REQUIRED, reason="Destructive action")

    def _add_expected_outputs(self, task: TaskObject, intent: ParsedIntent) -> None:
        """根据意图类型添加默认的预期输出定义。

        根据 intent_type 和 action_type 自动推断期望的输出类型：
        - create code -> code_block
        - debug -> explanation
        - explain -> explanation
        - search -> file_list
        - review -> review_comments

        参数:
            task: 要添加预期输出的任务对象
            intent: 解析后的意图
        """
        itype, action = intent.intent_type.value, intent.action_type.value
        if itype == "code" and action == "create":
            task.add_expected_output(type="code_block", validation="Valid runnable code")
        elif itype == "debug":
            task.add_expected_output(type="explanation", validation="Identify root cause")
        elif itype == "explain":
            task.add_expected_output(type="explanation", validation="Clear and accurate")
        elif itype == "search":
            task.add_expected_output(type="file_list", validation="Relevant files with context")
        elif itype == "review":
            task.add_expected_output(type="review_comments", validation="Issues with severity")


_builder: TaskBuilder | None = None


def get_task_builder() -> TaskBuilder:
    """获取全局单例的 TaskBuilder 实例。

    使用懒加载模式，仅在首次调用时创建实例。

    返回:
        TaskBuilder 单例
    """
    global _builder
    if _builder is None:
        _builder = TaskBuilder()
    return _builder


def build_task(intent: ParsedIntent, raw_input: str = "") -> TaskObject:
    """快捷函数：从意图构建 TaskObject。

    这是使用 TaskBuilder 构建任务的便捷入口。

    参数:
        intent: 解析后的用户意图
        raw_input: 原始用户输入文本

    返回:
        构建完成的 TaskObject 实例
    """
    return get_task_builder().build(intent, raw_input)
