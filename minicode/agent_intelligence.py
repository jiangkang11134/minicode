"""Agent 智能调度与错误处理模块。

提供工具调用调度、错误分类与恢复策略建议、
以及智能提示生成等核心智能功能。

模块包含：
1. ToolSchedulerController - 工具并发与重试压力的反馈控制器
2. ErrorClassifier - 错误分类与恢复策略推荐
3. NudgeGenerator - 智能提示消息生成
4. ToolScheduler - 基于历史性能的智能工具调度
"""
from enum import Enum
from dataclasses import dataclass
from typing import Any


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minicode.agent_metrics import AgentMetricsCollector


class ErrorCategory(Enum):
    """错误类别枚举。

    定义了六种错误类型：NETWORK（网络错误）、PERMISSION（权限错误）、
    RESOURCE（资源不足）、LOGIC（逻辑/参数错误）、TIMEOUT（超时）、
    UNKNOWN（未分类错误）。
    """
    NETWORK = "network"
    PERMISSION = "permission"
    RESOURCE = "resource"
    LOGIC = "logic"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    """恢复策略枚举。

    定义了七种错误恢复策略：
    - RETRY_EXPONENTIAL_BACKOFF: 指数退避重试
    - RETRY_IMMEDIATE: 立即重试
    - FALLBACK_ALTERNATIVE: 使用备用方案
    - REQUEST_PERMISSION: 请求权限
    - WAIT_AND_RETRY: 等待后重试
    - SKIP_AND_CONTINUE: 跳过并继续
    - ABORT: 中止操作
    """
    RETRY_EXPONENTIAL_BACKOFF = "retry_exponential_backoff"
    RETRY_IMMEDIATE = "retry_immediate"
    FALLBACK_ALTERNATIVE = "fallback_alternative"
    REQUEST_PERMISSION = "request_permission"
    WAIT_AND_RETRY = "wait_and_retry"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ABORT = "abort"


@dataclass
class ClassifiedError:
    """分类后的错误信息。

    包含错误类别、推荐的恢复策略、置信度评分以及附加上下文信息。
    """
    category: ErrorCategory
    strategy: RecoveryStrategy
    confidence: float  # 0.0 - 1.0
    context: dict[str, Any]


@dataclass
class ToolSchedulingSignal:
    """反馈控制器使用的观测状态信号。

    包含工具调用数量、写入操作数量、命令执行数量、
    错误率、平均延迟、冲突次数和最近失败次数等运行时压力信号。
    """
    call_count: int = 0
    write_count: int = 0
    command_count: int = 0
    error_rate: float = 0.0
    avg_latency: float = 0.0
    conflict_count: int = 0
    recent_failures: int = 0


@dataclass
class ToolSchedulingDecision:
    """工具执行调度的控制器输出。

    包含最大并发数、并发倍率、冷却时间、重试退避倍率以及决策原因。
    """
    max_workers: int
    concurrency_multiplier: float
    cooldown_seconds: float = 0.0
    retry_backoff_multiplier: float = 1.0
    reasons: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """将调度决策转换为字典格式。

        返回:
            包含所有决策字段的字典，数值字段保留 3 位小数
        """
        return {
            "max_workers": self.max_workers,
            "concurrency_multiplier": round(self.concurrency_multiplier, 3),
            "cooldown_seconds": round(self.cooldown_seconds, 3),
            "retry_backoff_multiplier": round(self.retry_backoff_multiplier, 3),
            "reasons": list(self.reasons or []),
        }


class ToolSchedulerController:
    """工具并发与重试压力的反馈控制器。

    根据运行时压力信号（错误率、延迟、冲突、失败次数）
    动态调节工具执行的并发度、冷却时间和重试参数。
    """

    def decide(self, signal: ToolSchedulingSignal) -> ToolSchedulingDecision:
        """根据输入的压力信号做出调度决策。

        综合考虑写入操作、命令执行、错误率、延迟、
        冲突次数和最近失败数等因素，逐步降低并发倍率、
        增加冷却时间和重试退避倍率，最终输出合理的并发数。

        参数:
            signal: 包含当前运行时压力信号的 ToolSchedulingSignal

        返回:
            包含最大并发数、并发倍率、冷却时间、重试退避倍率和决策原因的决策对象
        """
        if signal.call_count <= 0:
            return ToolSchedulingDecision(
                max_workers=1,
                concurrency_multiplier=0.0,
                reasons=["no tool calls"],
            )

        multiplier = 1.0
        cooldown = 0.0
        backoff = 1.0
        reasons: list[str] = []

        if signal.write_count > 0:
            multiplier *= 0.65
            reasons.append("write tools present")

        if signal.command_count > 0:
            multiplier *= 0.55
            backoff *= 1.5
            reasons.append("command tools present")

        if signal.error_rate >= 0.5:
            multiplier *= 0.35
            cooldown += 1.0
            backoff *= 2.0
            reasons.append("high tool error rate")
        elif signal.error_rate >= 0.2:
            multiplier *= 0.65
            cooldown += 0.25
            backoff *= 1.3
            reasons.append("elevated tool error rate")

        if signal.avg_latency >= 30.0:
            multiplier *= 0.50
            cooldown += 0.5
            reasons.append("high tool latency")
        elif signal.avg_latency >= 10.0:
            multiplier *= 0.75
            reasons.append("elevated tool latency")

        if signal.conflict_count > 0:
            multiplier *= max(0.35, 1.0 - 0.15 * signal.conflict_count)
            reasons.append("known tool conflicts")

        if signal.recent_failures > 0:
            multiplier *= max(0.40, 1.0 - 0.10 * signal.recent_failures)
            cooldown += min(2.0, 0.25 * signal.recent_failures)
            backoff *= min(3.0, 1.0 + 0.25 * signal.recent_failures)
            reasons.append("recent tool failures")

        multiplier = max(0.15, min(1.0, multiplier))
        max_workers = max(1, min(signal.call_count, int(round(signal.call_count * multiplier))))

        return ToolSchedulingDecision(
            max_workers=max_workers,
            concurrency_multiplier=multiplier,
            cooldown_seconds=cooldown,
            retry_backoff_multiplier=backoff,
            reasons=reasons or ["healthy scheduling pressure"],
        )


class ErrorClassifier:
    """错误分类器，根据错误消息内容分类错误并推荐恢复策略。

    基于关键词模式匹配将错误分为网络、权限、资源、超时、逻辑等类别，
    并为每种类别映射默认的恢复策略。
    """

    # Keyword patterns for each error category
    PATTERNS = {
        ErrorCategory.NETWORK: [
            "connection", "timeout", "network", "refused", "unreachable",
            "reset", "closed", "dns", "ssl", "certificate",
        ],
        ErrorCategory.PERMISSION: [
            "permission", "access denied", "unauthorized", "forbidden",
            "privilege", "not allowed", "restricted", "admin",
        ],
        ErrorCategory.RESOURCE: [
            "memory", "disk", "space", "resource", "quota", "limit",
            "exceeded", "out of", "no space", "too large",
        ],
        ErrorCategory.TIMEOUT: [
            "timeout", "timed out", "deadline", "expired", "took too long",
        ],
        ErrorCategory.LOGIC: [
            "invalid", "not found", "does not exist", "already exists",
            "bad request", "syntax", "parse", "format", "type error",
        ],
    }

    # Strategy mapping based on category
    STRATEGY_MAP = {
        ErrorCategory.NETWORK: RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
        ErrorCategory.TIMEOUT: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.PERMISSION: RecoveryStrategy.REQUEST_PERMISSION,
        ErrorCategory.RESOURCE: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.LOGIC: RecoveryStrategy.FALLBACK_ALTERNATIVE,
        ErrorCategory.UNKNOWN: RecoveryStrategy.RETRY_IMMEDIATE,
    }

    @classmethod
    def classify(cls, error_message: str, tool_name: str = "") -> ClassifiedError:
        """对错误消息进行分类并推荐恢复策略。

        通过关键词匹配计算各类别的得分，选择得分最高的类别。
        置信度基于匹配到的关键词数量计算（0.3-0.95）。
        对于只读工具（read_file、list_files、grep_files）的逻辑错误，
        将策略调整为 SKIP_AND_CONTINUE。

        参数:
            error_message: 原始错误消息文本
            tool_name: 产生错误的工具名称，用于辅助策略调整

        返回:
            包含分类结果和恢复策略的 ClassifiedError
        """
        error_lower = error_message.lower()

        scores: dict[ErrorCategory, int] = {}
        for category, patterns in cls.PATTERNS.items():
            score = sum(1 for p in patterns if p in error_lower)
            if score > 0:
                scores[category] = score

        if scores:
            best_category = max(scores, key=scores.get)
            confidence = min(0.95, 0.5 + max(scores.values()) * 0.15)
        else:
            best_category = ErrorCategory.UNKNOWN
            confidence = 0.3

        strategy = cls.STRATEGY_MAP.get(best_category, RecoveryStrategy.RETRY_IMMEDIATE)

        # Adjust strategy based on tool name
        if tool_name in ["read_file", "list_files", "grep_files"] and best_category == ErrorCategory.LOGIC:
            strategy = RecoveryStrategy.SKIP_AND_CONTINUE

        return ClassifiedError(
            category=best_category,
            strategy=strategy,
            confidence=confidence,
            context={"tool_name": tool_name, "error_snippet": error_message[:200]},
        )


class NudgeGenerator:
    """智能提示生成器，基于失败上下文生成指导性提示消息。

    维护各类错误类别和恢复策略对应的模板消息，
    支持按重试次数和具体工具名称添加上下文相关提示。
    """

    TEMPLATES = {
        ErrorCategory.NETWORK: {
            RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF: (
                "Network error detected. The previous attempt failed due to connectivity issues. "
                "Please retry the same operation. If it fails again, consider checking your "
                "network connection or trying an alternative approach."
            ),
            RecoveryStrategy.RETRY_IMMEDIATE: (
                "A transient network issue occurred. Please retry the operation immediately."
            ),
        },
        ErrorCategory.PERMISSION: {
            RecoveryStrategy.REQUEST_PERMISSION: (
                "Permission denied. You don't have sufficient privileges for this operation. "
                "Consider: (1) running with elevated permissions if appropriate, "
                "(2) using a different approach that doesn't require elevated access, or "
                "(3) asking the user for permission to proceed."
            ),
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "Access was denied. Try an alternative approach that works with current permissions."
            ),
        },
        ErrorCategory.RESOURCE: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "Resource limit reached (memory/disk/quota). Consider: "
                "(1) freeing up resources before retrying, "
                "(2) processing in smaller batches, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.TIMEOUT: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "The operation timed out. This may be due to heavy load or a long-running process. "
                "Consider: (1) retrying after a brief wait, "
                "(2) breaking the task into smaller steps, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.LOGIC: {
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "The previous approach encountered an error. Consider using a different strategy: "
                "try alternative tools, adjust parameters, or break the task into smaller steps."
            ),
            RecoveryStrategy.SKIP_AND_CONTINUE: (
                "This step encountered an issue but it's not critical. "
                "You can skip this and continue with the remaining tasks."
            ),
        },
        ErrorCategory.UNKNOWN: {
            RecoveryStrategy.RETRY_IMMEDIATE: (
                "An unexpected error occurred. Please retry the operation. "
                "If the error persists, try a different approach."
            ),
        },
    }

    @classmethod
    def generate(cls, classified_error: ClassifiedError, retry_count: int = 0) -> str:
        """根据分类后的错误生成提示消息。

        首先获取基础模板消息，然后根据重试次数添加重试上下文，
        最后根据具体工具名称和错误类别添加上下文相关的额外建议。

        参数:
            classified_error: 已分类的错误信息
            retry_count: 当前已重试次数

        返回:
            生成的提示消息字符串
        """
        category = classified_error.category
        strategy = classified_error.strategy

        # Get base template
        category_templates = cls.TEMPLATES.get(category, cls.TEMPLATES[ErrorCategory.UNKNOWN])
        base_message = category_templates.get(
            strategy,
            category_templates.get(RecoveryStrategy.RETRY_IMMEDIATE, "Please retry."),
        )

        # Add retry context
        if retry_count > 0:
            base_message += f" (This is retry attempt {retry_count + 1})"

        # Add tool-specific hints
        tool_name = classified_error.context.get("tool_name", "")
        if tool_name == "run_command" and category == ErrorCategory.PERMISSION:
            base_message += " For command execution, consider using 'sudo' only if explicitly approved by the user."
        elif tool_name in ["write_file", "edit_file"] and category == ErrorCategory.LOGIC:
            base_message += " For file operations, verify the path exists and you have write permissions."
        elif tool_name == "grep_files" and category == ErrorCategory.LOGIC:
            base_message += " Try a broader pattern, or use list_files first to understand the directory structure."
        elif tool_name == "read_file" and category in (ErrorCategory.LOGIC, ErrorCategory.RESOURCE):
            base_message += " Verify the file path is correct. Use list_files or file_tree to confirm the file exists."
        elif tool_name == "edit_file" and category == ErrorCategory.LOGIC:
            base_message += " The search string may not match. Use grep_files to find the exact text you want to edit, then copy it verbatim."
        elif category == ErrorCategory.TIMEOUT:
            base_message += " Try breaking this into smaller steps or reducing the scope."

        return base_message

    @classmethod
    def generate_progress_nudge(cls, tool_results: list[tuple[str, bool]]) -> str | None:
        """根据工具执行结果生成进度提示。

        统计工具执行的成功和失败数量，根据情况生成不同的提示：
        - 全部成功：鼓励继续下一步
        - 全部失败：建议检查错误并调整方法
        - 部分成功部分失败：建议先处理失败项

        参数:
            tool_results: 工具名称和成功状态的元组列表

        返回:
            生成的进度提示，如果结果为空则返回 None
        """
        if not tool_results:
            return None

        success_count = sum(1 for _, ok in tool_results if ok)
        failure_count = len(tool_results) - success_count

        if failure_count == 0:
            return (
                f"All {success_count} tool(s) executed successfully. "
                "Continue with the next concrete step or provide a <final> answer if complete."
            )
        elif failure_count == len(tool_results):
            return (
                f"All {failure_count} tool(s) failed. "
                "Review the errors, adjust your approach, and try again with corrected parameters."
            )
        else:
            return (
                f"{success_count} tool(s) succeeded, {failure_count} failed. "
                "Address the failures first, then continue with remaining tasks."
            )


class ToolScheduler:
    """基于历史性能的工具智能调度器。

    根据工具的历史成功率、并发安全性以及冲突记录，
    将工具调用合理划分为可并发批次和串行批次。
    """

    def __init__(
        self,
        metrics_collector: "AgentMetricsCollector | None" = None,
        controller: ToolSchedulerController | None = None,
    ):
        """初始化工具调度器。

        参数:
            metrics_collector: 可选的指标收集器，用于获取工具历史成功率和统计
            controller: 可选的调度控制器，用于决定并发参数
        """
        self._metrics = metrics_collector
        self._controller = controller or ToolSchedulerController()
        self._conflict_history: dict[frozenset[str], int] = {}  # Track tool pair conflicts
        self._last_decision: ToolSchedulingDecision | None = None

    def schedule_calls(self, calls: list[dict], tools: Any) -> tuple[list[dict], list[dict]]:
        """将工具调用划分为并发批次和串行批次。

        根据工具的可靠性评分和并发安全性进行排序和分组：
        - 并发安全性未知或不安全的工具放入串行批次
        - 存在已知冲突的工具对放入串行批次
        - 其余工具放入并发批次

        参数:
            calls: 待调度的工具调用列表，每个元素包含 "toolName" 等信息
            tools: 工具注册表，用于查询工具的并发安全性

        返回:
            (concurrent_calls, serial_calls) 元组，分别代表可并发和需串行的调用
        """
        if len(calls) <= 1:
            return calls, []

        # Score each call based on historical success rate
        scored_calls: list[tuple[float, dict]] = []
        for call in calls:
            tool_name = call["toolName"]
            score = self._get_tool_score(tool_name)
            scored_calls.append((score, call))

        # Sort by score (highest first = most reliable)
        scored_calls.sort(key=lambda x: x[0], reverse=True)

        # Identify conflicting tool pairs
        concurrent_calls: list[dict] = []
        serial_calls: list[dict] = []

        for score, call in scored_calls:
            tool_name = call["toolName"]
            tool_def = tools.find(tool_name)

            if not tool_def or not tool_def.is_concurrency_safe:
                serial_calls.append(call)
                continue

            # Check if this tool conflicts with already-selected concurrent tools
            conflicts = self._has_conflicts(tool_name, concurrent_calls)
            if conflicts:
                serial_calls.append(call)
            else:
                concurrent_calls.append(call)

        return concurrent_calls, serial_calls

    def _get_tool_score(self, tool_name: str) -> float:
        """获取工具的可靠性评分（0.0 - 1.0）。

        如果有指标收集器则使用其历史成功率，否则返回 1.0 表示默认可靠。

        参数:
            tool_name: 工具名称

        返回:
            可靠性评分
        """
        if self._metrics is None:
            return 1.0
        stats = self._metrics.get_tool_stats(tool_name)
        return stats.success_rate

    def _has_conflicts(self, tool_name: str, concurrent_calls: list[dict]) -> bool:
        """检查工具与已选并发工具之间是否存在已知冲突。

        遍历已选入并发批次的工具，检查与该工具是否存在冲突历史。
        当冲突记录达到 2 次及以上时视为存在冲突。

        参数:
            tool_name: 待检查的工具名称
            concurrent_calls: 已选入并发批次的工具调用列表

        返回:
            是否存在已知冲突
        """
        for other_call in concurrent_calls:
            other_name = other_call["toolName"]
            pair = frozenset({tool_name, other_name})
            conflict_count = self._conflict_history.get(pair, 0)
            if conflict_count >= 2:  # Known conflict threshold
                return True
        return False

    def record_conflict(self, tool1: str, tool2: str) -> None:
        """记录两个工具在并发执行时发生了冲突。

        将冲突计数加一，用于后续调度时避免将这两个工具同时并发执行。

        参数:
            tool1: 第一个工具名称
            tool2: 第二个工具名称
        """
        pair = frozenset({tool1, tool2})
        self._conflict_history[pair] = self._conflict_history.get(pair, 0) + 1

    def get_recommended_max_workers(
        self,
        concurrent_calls: list[dict],
        *,
        error_rate: float = 0.0,
        avg_latency: float = 0.0,
        recent_failures: int = 0,
    ) -> int:
        """根据调用特征推荐最大并发工作数。

        从调用数量和工具类型（写入/命令）确定基准值，
        然后通过调度控制器综合考虑错误率、延迟和失败次数做出最终决策。

        参数:
            concurrent_calls: 可并发的工具调用列表
            error_rate: 当前错误率（0.0-1.0）
            avg_latency: 平均延迟（秒）
            recent_failures: 最近失败次数

        返回:
            推荐的最大并发工作数
        """
        if not concurrent_calls:
            self._last_decision = ToolSchedulingDecision(
                max_workers=1,
                concurrency_multiplier=0.0,
                reasons=["no concurrent calls"],
            )
            return 1

        base = min(len(concurrent_calls), 8)

        # Reduce workers if we have file write operations
        write_tools = {"write_file", "edit_file", "patch_file", "modify_file"}
        write_count = sum(1 for c in concurrent_calls if c["toolName"] in write_tools)
        if write_count > 0:
            base = min(base, 4)

        # Reduce further if we have command executions
        command_tools = {"run_command", "execute_command", "bash"}
        cmd_count = sum(1 for c in concurrent_calls if c["toolName"] in command_tools)
        if cmd_count > 0:
            base = min(base, 3)

        signal = ToolSchedulingSignal(
            call_count=base,
            write_count=write_count,
            command_count=cmd_count,
            error_rate=error_rate,
            avg_latency=avg_latency,
            conflict_count=self._count_conflicts(concurrent_calls),
            recent_failures=recent_failures,
        )
        self._last_decision = self._controller.decide(signal)
        return max(1, min(base, self._last_decision.max_workers))

    @property
    def last_decision(self) -> ToolSchedulingDecision | None:
        """获取最近一次的调度决策。"""
        return self._last_decision

    def _count_conflicts(self, calls: list[dict]) -> int:
        """统计调用列表中存在已知冲突的工具对数量。

        参数:
            calls: 工具调用列表

        返回:
            存在已知冲突的工具对数量
        """
        count = 0
        for i, call in enumerate(calls):
            for other in calls[i + 1:]:
                pair = frozenset({call["toolName"], other["toolName"]})
                if self._conflict_history.get(pair, 0) >= 2:
                    count += 1
        return count
