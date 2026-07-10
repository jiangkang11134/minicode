"""Agent 执行指标收集与持久化模块。

提供工具执行记录、Agent 回合指标、工具历史统计等数据结构的定义，
以及 AgentMetricsCollector 用于在运行时收集、存储和持久化这些指标。

适用场景：
- 工具性能监控
- 成功率统计
- 运行时诊断
"""
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ErrorCategory(Enum):
    """工具执行错误类别枚举。

    定义了五类工具执行错误：
    - NETWORK: 连接错误、超时等网络相关问题
    - PERMISSION: 权限不足、认证失败等访问控制问题
    - RESOURCE: 内存不足、磁盘已满等资源受限问题
    - LOGIC: 工具逻辑错误、输入参数无效等
    - UNKNOWN: 无法归类的错误
    """
    NETWORK = "network"          # Connection errors, timeouts
    PERMISSION = "permission"    # Access denied, auth errors
    RESOURCE = "resource"        # Out of memory, disk full
    LOGIC = "logic"              # Tool logic errors, invalid input
    UNKNOWN = "unknown"          # Unclassified errors


@dataclass
class ToolExecutionRecord:
    """单次工具执行的记录。

    包含工具名称、起止时间、执行是否成功、错误类别、
    错误消息和消耗的 Token 数量等信息。
    """
    tool_name: str
    start_time: float
    end_time: float = 0.0
    success: bool = False
    error_category: ErrorCategory = ErrorCategory.UNKNOWN
    error_message: str = ""
    tokens_consumed: int = 0

    @property
    def duration_ms(self) -> float:
        """获取工具执行的持续时间（毫秒）。"""
        return (self.end_time - self.start_time) * 1000


@dataclass
class AgentTurnMetrics:
    """单次 Agent 回合的指标。

    包含回合 ID、起止时间、工具执行记录列表、
    模型调用次数和消耗的总 Token 数。
    """
    turn_id: int
    start_time: float
    end_time: float = 0.0
    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    model_calls: int = 0
    total_tokens: int = 0

    @property
    def duration_ms(self) -> float:
        """获取回合持续时长（毫秒）。"""
        return (self.end_time - self.start_time) * 1000

    @property
    def tool_success_rate(self) -> float:
        """获取本回合工具执行的成功率。

        如果没有工具执行记录则返回 1.0。
        """
        if not self.tool_records:
            return 1.0
        successful = sum(1 for r in self.tool_records if r.success)
        return successful / len(self.tool_records)


@dataclass
class ToolHistoricalStats:
    """特定工具的历史统计信息。

    包含执行总次数、成功次数、总耗时和各类错误的计数。
    """
    tool_name: str
    total_executions: int = 0
    successful_executions: int = 0
    total_duration_ms: float = 0.0
    error_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """获取工具的历史成功率。

        如果没有执行记录则返回 1.0。
        """
        if self.total_executions == 0:
            return 1.0
        return self.successful_executions / self.total_executions

    @property
    def avg_duration_ms(self) -> float:
        """获取工具的平均执行时间（毫秒）。"""
        if self.total_executions == 0:
            return 0.0
        return self.total_duration_ms / self.total_executions


class AgentMetricsCollector:
    """Agent 执行指标收集器。

    负责收集、存储和持久化 Agent 执行过程中的各项指标，
    包括工具执行记录、回合级别统计和工具级别历史统计。
    支持按路径持久化到磁盘并从磁盘恢复。
    """

    def __init__(self, storage_path: Path | None = None):
        """初始化指标收集器。

        参数:
            storage_path: 可选的持久化文件路径。如果提供且文件已存在，则自动加载历史数据
        """
        self._turns: list[AgentTurnMetrics] = []
        self._tool_stats: dict[str, ToolHistoricalStats] = {}
        self._current_turn: AgentTurnMetrics | None = None
        self._current_tool: ToolExecutionRecord | None = None
        self._storage_path = storage_path
        if storage_path and storage_path.exists():
            self._load()

    def start_turn(self, turn_id: int) -> None:
        """开始记录一个新的 Agent 回合。

        创建新的 AgentTurnMetrics 实例并记录起始时间。

        参数:
            turn_id: 回合的唯一标识符
        """
        self._current_turn = AgentTurnMetrics(turn_id=turn_id, start_time=time.time())

    def end_turn(self, total_tokens: int = 0) -> AgentTurnMetrics:
        """结束当前回合并返回其指标。

        记录结束时间、更新总 Token 数、更新工具历史统计，
        然后将回合加入历史列表并持久化到磁盘。

        参数:
            total_tokens: 本回合消耗的总 Token 数

        返回:
            已完成的 AgentTurnMetrics 对象

        抛出:
            RuntimeError: 如果没有正在进行的回合
        """
        if self._current_turn is None:
            raise RuntimeError("No turn in progress")
        self._current_turn.end_time = time.time()
        self._current_turn.total_tokens = total_tokens
        self._turns.append(self._current_turn)

        # Update historical stats
        for record in self._current_turn.tool_records:
            self._update_tool_stats(record)

        result = self._current_turn
        self._current_turn = None
        self._save()
        return result

    def start_tool(self, tool_name: str) -> None:
        """开始记录一次工具执行。

        创建新的 ToolExecutionRecord 并记录起始时间。

        参数:
            tool_name: 被执行的工具名称
        """
        self._current_tool = ToolExecutionRecord(
            tool_name=tool_name,
            start_time=time.time(),
        )

    def end_tool(self, success: bool, error: str = "", tokens: int = 0) -> ToolExecutionRecord:
        """结束当前工具执行并返回其记录。

        记录结束时间、成功状态、错误消息和 Token 消耗，
        并对错误进行分类。如果有正在进行的回合，将记录追加到回合中。

        参数:
            success: 工具是否执行成功
            error: 错误消息（如果有）
            tokens: 本次工具执行消耗的 Token 数

        返回:
            完整的 ToolExecutionRecord 对象

        抛出:
            RuntimeError: 如果没有正在执行的工具
        """
        if self._current_tool is None:
            raise RuntimeError("No tool execution in progress")
        self._current_tool.end_time = time.time()
        self._current_tool.success = success
        self._current_tool.error_message = error
        self._current_tool.tokens_consumed = tokens
        self._current_tool.error_category = self._classify_error(error)

        if self._current_turn:
            self._current_turn.tool_records.append(self._current_tool)

        result = self._current_tool
        self._current_tool = None
        return result

    def get_tool_stats(self, tool_name: str) -> ToolHistoricalStats:
        """获取指定工具的历史统计信息。

        如果该工具尚无统计数据，返回一个初始值全为 0 的 ToolHistoricalStats。

        参数:
            tool_name: 工具名称

        返回:
            该工具的历史统计
        """
        return self._tool_stats.get(tool_name, ToolHistoricalStats(tool_name=tool_name))

    def get_all_tool_stats(self) -> dict[str, ToolHistoricalStats]:
        """获取所有工具的历史统计信息。"""
        return dict(self._tool_stats)

    def get_recent_turns(self, count: int = 10) -> list[AgentTurnMetrics]:
        """获取最近 N 个回合的指标。

        参数:
            count: 需要获取的最近回合数

        返回:
            最近 count 个回合的指标列表
        """
        return self._turns[-count:]

    def _classify_error(self, error_message: str) -> ErrorCategory:
        """根据错误消息内容对错误进行分类。

        通过关键词匹配判断错误类型（网络/权限/资源/逻辑/未知）。

        参数:
            error_message: 原始错误消息

        返回:
            匹配到的 ErrorCategory，无法匹配时返回 UNKNOWN
        """
        error_lower = error_message.lower()
        if any(kw in error_lower for kw in ["connection", "timeout", "network", "refused", "unreachable"]):
            return ErrorCategory.NETWORK
        if any(kw in error_lower for kw in ["permission", "access denied", "unauthorized", "forbidden"]):
            return ErrorCategory.PERMISSION
        if any(kw in error_lower for kw in ["memory", "disk", "space", "resource", "quota"]):
            return ErrorCategory.RESOURCE
        if error_message:
            return ErrorCategory.LOGIC
        return ErrorCategory.UNKNOWN

    def _update_tool_stats(self, record: ToolExecutionRecord) -> None:
        """用一条新记录更新对应工具的历史统计。

        参数:
            record: 新的工具执行记录
        """
        name = record.tool_name
        if name not in self._tool_stats:
            self._tool_stats[name] = ToolHistoricalStats(tool_name=name)

        stats = self._tool_stats[name]
        stats.total_executions += 1
        if record.success:
            stats.successful_executions += 1
        stats.total_duration_ms += record.duration_ms

        cat = record.error_category.value
        stats.error_counts[cat] = stats.error_counts.get(cat, 0) + 1

    def _save(self) -> None:
        """将指标数据持久化到磁盘。

        保存工具统计和最近 50 个回合的摘要信息。
        如果写入失败则静默忽略（尽力而为策略）。
        """
        if self._storage_path is None:
            return
        try:
            data = {
                "tool_stats": {
                    name: {
                        "tool_name": s.tool_name,
                        "total_executions": s.total_executions,
                        "successful_executions": s.successful_executions,
                        "total_duration_ms": s.total_duration_ms,
                        "error_counts": s.error_counts,
                    }
                    for name, s in self._tool_stats.items()
                },
                "recent_turns": [
                    {
                        "turn_id": t.turn_id,
                        "duration_ms": t.duration_ms,
                        "tool_success_rate": t.tool_success_rate,
                        "total_tokens": t.total_tokens,
                        "tool_count": len(t.tool_records),
                    }
                    for t in self._turns[-50:]  # Keep last 50 turns
                ],
            }
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass  # Metrics persistence is best-effort

    def _load(self) -> None:
        """从磁盘加载之前持久化的指标数据。

        恢复工具统计信息，如果读取或解析失败则静默忽略。
        """
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            for name, s in data.get("tool_stats", {}).items():
                self._tool_stats[name] = ToolHistoricalStats(
                    tool_name=s["tool_name"],
                    total_executions=s["total_executions"],
                    successful_executions=s["successful_executions"],
                    total_duration_ms=s["total_duration_ms"],
                    error_counts=s.get("error_counts", {}),
                )
        except Exception:
            pass
