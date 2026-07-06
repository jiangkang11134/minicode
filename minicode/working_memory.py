"""上下文压缩过程中的工作记忆保护模块。

受 Learn Claude Code 最佳实践启发：
- 在上下文压缩期间保留关键连续性信息
- 保护活跃任务上下文不被摘要掉
- 维持跨压缩边界的对话流连续性

提供：
- WorkingMemoryTracker: 追踪并保护关键上下文
- ContinuityMarker: 标记重要的对话流节点
- MemoryBudgetAllocator: 为工作记忆分配令牌预算
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from minicode.context_manager import estimate_tokens


@dataclass
class WorkingMemoryEntry:
    """单个工作记忆条目，在上下文压缩期间应受保护。

    Attributes:
        content: 需保护的内容
        entry_type: 条目类型（"active_task", "user_intent", "key_decision", "error_context"）
        created_at: 创建时间戳
        expires_at: 过期时间戳，None 表示永不过期
        importance: 重要性评分 0.0-1.0，越高越受保护
    """

    content: str
    entry_type: str  # "active_task", "user_intent", "key_decision", "error_context"
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = no expiry
    importance: float = 1.0  # 0.0 - 1.0, higher = more protected

    def is_expired(self) -> bool:
        """检查此条目是否已过期。

        返回:
            如果 expires_at 不为 None 且当前时间超过 expires_at 则返回 True
        """
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def token_count(self) -> int:
        """估算此条目的令牌（token）数量。

        使用 context_manager.estimate_tokens 进行估算。

        返回:
            估算的令牌数
        """
        return estimate_tokens(self.content)


class WorkingMemoryTracker:
    """在上下文压缩期间追踪并保护关键上下文。

    实现 Learn Claude Code 最佳实践中的"工作记忆保护"模式。
    在上下文压缩期间，此追踪器中的条目会被保留，以维持
    对话连续性和任务连贯性。
    """

    def __init__(
        self,
        max_entries: int = 15,
        max_tokens: int = 4000,
    ) -> None:
        """初始化工作记忆追踪器。

        参数:
            max_entries: 最大条目数量，默认 15
            max_tokens: 最大令牌数预算，默认 4000
        """
        self._entries: list[WorkingMemoryEntry] = []
        self.max_entries = max_entries
        self.max_tokens = max_tokens

    def add(
        self,
        content: str,
        entry_type: str = "active_task",
        ttl_seconds: float | None = None,
        importance: float = 1.0,
    ) -> WorkingMemoryEntry:
        """添加一个需保护的工作记忆条目。

        创建并存储一个 WorkingMemoryEntry，然后强制执行容量限制。

        参数:
            content: 要保护的内容
            entry_type: 条目类型（active_task, user_intent 等）
            ttl_seconds: 生存时间（秒），None 表示永不过期
            importance: 重要性评分 0.0-1.0，越高越受保护

        返回:
            新创建的 WorkingMemoryEntry 实例
        """
        expires_at = None
        if ttl_seconds is not None:
            expires_at = time.time() + ttl_seconds

        entry = WorkingMemoryEntry(
            content=content,
            entry_type=entry_type,
            expires_at=expires_at,
            importance=importance,
        )

        self._entries.append(entry)
        self._enforce_limits()
        return entry

    def remove(self, entry: WorkingMemoryEntry) -> None:
        """移除一个工作记忆条目。

        参数:
            entry: 要移除的 WorkingMemoryEntry 实例
        """
        if entry in self._entries:
            self._entries.remove(entry)

    def clear_expired(self) -> int:
        """移除所有已过期的条目。

        返回:
            被移除的条目数量
        """
        before = len(self._entries)
        self._entries = [e for e in self._entries if not e.is_expired()]
        return before - len(self._entries)

    def get_protected_content(self) -> list[str]:
        """获取所有未过期条目的受保护内容。

        自动先清理过期条目。

        返回:
            条目内容字符串列表
        """
        self.clear_expired()
        return [e.content for e in self._entries]

    def get_protected_tokens(self) -> int:
        """获取受保护内容的总令牌数。

        自动跳过已过期条目。

        返回:
            总令牌数
        """
        return sum(e.token_count() for e in self._entries if not e.is_expired())

    def get_stats(self) -> dict[str, Any]:
        """获取工作记忆的统计信息。

        包括条目数、最大条目数、受保护令牌数、最大令牌数和利用率。

        返回:
            包含统计信息的字典
        """
        self.clear_expired()
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "protected_tokens": self.get_protected_tokens(),
            "max_tokens": self.max_tokens,
            "utilization": self.get_protected_tokens() / self.max_tokens
            if self.max_tokens > 0
            else 0,
        }

    def _enforce_limits(self) -> None:
        """强制容量和令牌数限制。

        依次执行：
        1. 移除所有过期条目
        2. 若令牌数超出预算，按重要性从低到高移除条目
        3. 若条目数超出上限，按重要性从低到高移除条目
        """
        # Remove expired first
        self.clear_expired()

        # Remove by token budget
        while self.get_protected_tokens() > self.max_tokens and self._entries:
            # Remove lowest importance entry
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

        # Remove by entry count
        while len(self._entries) > self.max_entries and self._entries:
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

    def format_status(self) -> str:
        """格式化工作记忆状态用于展示。

        生成包含条目数、令牌使用情况和受保护内容预览的多行字符串。

        返回:
            格式化的状态字符串
        """
        stats = self.get_stats()
        lines = [
            "Working Memory",
            "=" * 50,
            f"Entries: {stats['entries']}/{stats['max_entries']}",
            f"Protected tokens: {stats['protected_tokens']:,}/{stats['max_tokens']:,} ({stats['utilization']*100:.0f}%)",
            "",
        ]

        if self._entries:
            lines.append("Protected Content:")
            for entry in self._entries:
                expires = ""
                if entry.expires_at:
                    remaining = entry.expires_at - time.time()
                    if remaining > 0:
                        expires = f" (expires in {remaining/60:.0f}m)"
                    else:
                        expires = " (EXPIRED)"
                preview = entry.content[:60].replace("\n", " ")
                lines.append(f"  • [{entry.entry_type}] {preview}...{expires}")

        return "\n".join(lines)


@dataclass
class ContinuityMarker:
    """标记重要的对话流节点。

    在上下文压缩期间，这些标记有助于在消息被摘要后
    重建对话脉络。

    Attributes:
        marker_type: 标记类型（"task_start", "decision_point", "error_recovered", "user_redirect"）
        description: 标记描述
        timestamp: 创建时间戳
        metadata: 附加元数据
    """

    marker_type: str  # "task_start", "decision_point", "error_recovered", "user_redirect"
    description: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationContinuityManager:
    """管理跨压缩边界的对话连续性。

    当上下文被压缩时，此管理器通过保留关键转换节点
    来帮助重建对话流。
    """

    def __init__(self, max_markers: int = 20) -> None:
        """初始化对话连续性管理器。

        参数:
            max_markers: 最大保留标记数，默认 20
        """
        self._markers: list[ContinuityMarker] = []
        self.max_markers = max_markers

    def add_marker(
        self,
        marker_type: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> ContinuityMarker:
        """添加一个连续性标记。

        创建标记并追加到列表，超出上限时只保留最新的 max_markers 个。

        参数:
            marker_type: 标记类型
            description: 标记描述
            metadata: 附加元数据

        返回:
            新创建的 ContinuityMarker 实例
        """
        marker = ContinuityMarker(
            marker_type=marker_type,
            description=description,
            metadata=metadata or {},
        )
        self._markers.append(marker)

        # Enforce limit
        if len(self._markers) > self.max_markers:
            self._markers = self._markers[-self.max_markers:]

        return marker

    def get_recent_markers(self, limit: int = 10) -> list[ContinuityMarker]:
        """获取最近的连续性标记。

        参数:
            limit: 获取数量，默认 10

        返回:
            最新的 limit 个标记列表
        """
        return self._markers[-limit:]

    def get_markers_since(self, timestamp: float) -> list[ContinuityMarker]:
        """获取指定时间戳之后添加的所有标记。

        参数:
            timestamp: 起始时间戳

        返回:
            该时间之后的所有标记列表
        """
        return [m for m in self._markers if m.timestamp > timestamp]

    def format_continuity_summary(self) -> str:
        """格式化对话连续性信息用于展示。

        输出最近 10 个标记的时间、类型和描述。

        返回:
            格式化的连续性摘要字符串
        """
        if not self._markers:
            return "No continuity markers."

        lines = ["Conversation Continuity", "=" * 50, ""]
        for marker in self._markers[-10:]:  # Last 10 markers
            time_str = time.strftime("%H:%M:%S", time.localtime(marker.timestamp))
            lines.append(f"  [{time_str}] [{marker.marker_type}] {marker.description}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_working_memory = WorkingMemoryTracker()
_continuity_manager = ConversationContinuityManager()


def get_working_memory() -> WorkingMemoryTracker:
    """获取全局工作记忆追踪器单例。

    返回:
        WorkingMemoryTracker 实例
    """
    return _working_memory


def get_continuity_manager() -> ConversationContinuityManager:
    """获取全局对话连续性管理器单例。

    返回:
        ConversationContinuityManager 实例
    """
    return _continuity_manager


def protect_context(
    content: str,
    entry_type: str = "active_task",
    ttl_seconds: float | None = None,
    importance: float = 1.0,
) -> WorkingMemoryEntry:
    """便利函数，用于在上下文压缩期间保护关键内容。

    直接调用全局工作记忆追踪器的 add 方法。

    参数:
        content: 要保护的内容
        entry_type: 条目类型
        ttl_seconds: 生存时间（秒）
        importance: 重要性评分

    返回:
        创建的 WorkingMemoryEntry 实例
    """
    return _working_memory.add(
        content,
        entry_type,
        ttl_seconds,
        importance=importance,
    )


def mark_continuity(
    marker_type: str,
    description: str,
    metadata: dict[str, Any] | None = None,
) -> ContinuityMarker:
    """便利函数，用于添加对话连续性标记。

    直接调用全局连续性管理器的 add_marker 方法。

    参数:
        marker_type: 标记类型
        description: 标记描述
        metadata: 附加元数据

    返回:
        创建的 ContinuityMarker 实例
    """
    return _continuity_manager.add_marker(marker_type, description, metadata)
