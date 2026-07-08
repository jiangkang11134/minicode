"""SmartCode 的 Claude Code 风格上下文管理系统。

实现了三层上下文管理架构：

1. **请求前轻量优化链**：
   - 读取去重（基于哈希的文件内容去重）
   - 工具结果预算（大输出持久化 + 预览替换）
   - 基于时间的微压缩（清理旧工具结果）

2. **自动压缩高水位调度器**：
   - 会话记忆压缩（使用现有记忆条目作为摘要基础）
   - 完整压缩（模型生成的摘要与新的基准线）
   - 断路器（连续 3 次失败后停止）

3. **响应式压缩错误恢复**：
   - 提示过长的恢复路径
   - 媒体大小错误恢复
   - 回退到用户可见的错误

架构参考：compact(5).md（Claude Code 源码分析）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------


class CompactTrigger(str, Enum):
    """压缩触发的来源方式。"""
    MANUAL = "manual"
    AUTO = "auto"
    REACTIVE = "reactive"
    MICROCOMPACT_TIME = "microcompact_time"
    MICROCOMPACT_CACHED = "microcompact_cached"


class CompactStrategy(str, Enum):
    """压缩所使用的策略类型。"""
    SESSION_MEMORY = "session_memory"
    FULL = "full"
    PARTIAL = "partial"
    MICROCOMPACT = "microcompact"
    TOOL_BUDGET = "tool_budget"
    READ_DEDUP = "read_dedup"
    REACTIVE = "reactive"


@dataclass
class CompactBoundary:
    """标记对话历史中的压缩点。

    压缩后，活动上下文视图从最后一个边界开始。
    边界本身是元数据，对模型不可见。
    """
    trigger: CompactTrigger
    strategy: CompactStrategy
    timestamp: float = field(default_factory=time.time)
    tokens_before: int = 0
    tokens_after: int = 0
    messages_removed: int = 0
    logical_parent_id: str | None = None
    preserved_segment: tuple[int, int] | None = None  # 保留的消息索引范围 (start, end)

    def to_dict(self) -> dict[str, Any]:
        """将压缩边界转换为字典格式。

        返回:
            包含所有字段的字典，便于序列化
        """
        return {
            "trigger": self.trigger.value,
            "strategy": self.strategy.value,
            "timestamp": self.timestamp,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "messages_removed": self.messages_removed,
            "logical_parent_id": self.logical_parent_id,
            "preserved_segment": list(self.preserved_segment) if self.preserved_segment else None,
        }


@dataclass
class CompactionResult:
    """压缩操作的结果。"""
    success: bool
    strategy: CompactStrategy
    trigger: CompactTrigger
    messages: list[dict[str, Any]]
    boundary: CompactBoundary | None = None
    tokens_freed: int = 0
    summary_text: str = ""
    error: str = ""

    @property
    def effective(self) -> bool:
        """判断本次压缩是否真正生效。

        返回:
            当压缩成功且释放了至少 1 token 时返回 True
        """
        return self.success and self.tokens_freed > 0


@dataclass
class ToolResultPersisted:
    """已持久化到磁盘的工具结果。"""
    original_size: int
    persisted_path: Path
    preview_text: str
    tool_name: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReadDedupEntry:
    """追踪文件读取信息，用于去重。"""
    file_path: str
    content_hash: str
    timestamp: float
    message_index: int  # 完整内容所在的消息索引


@dataclass
class MicrocompactState:
    """微压缩操作的状态。"""
    last_time_based_compact: float = 0.0
    time_based_interval: float = 3600.0  # 默认 1 小时
    keep_recent_tool_results: int = 5
    total_tokens_cleared: int = 0


@dataclass
class AutoCompactConfig:
    """自动压缩调度器的配置。"""
    enabled: bool = True
    threshold_ratio: float = 0.85  # 上下文窗口的 85%
    circuit_breaker_limit: int = 3
    circuit_breaker_recovery_seconds: float = 300.0  # 在此时间后自动恢复
    session_memory_enabled: bool = True
    min_keep_tokens: int = 10000  # 压缩后至少保留 10k token
    min_keep_messages: int = 5  # 至少保留 5 条文本消息
    max_expand_tokens: int = 40000  # 尾部保留的最大扩展 token 数


# ---------------------------------------------------------------------------
# Phase 2: Tool Result Budget
# ---------------------------------------------------------------------------


class ToolResultBudgetManager:
    """管理工具结果的大小预算，并通过磁盘持久化处理超限内容。

    当工具结果超出单条消息预算时，将其持久化到磁盘，
    并在上下文中替换为预览文本。
    """
    DEFAULT_BUDGET_PER_MESSAGE = 8000  # 每条用户消息的工具结果字符上限
    PERSIST_THRESHOLD = 4000  # 超过此大小则持久化
    PREVIEW_MAX_CHARS = 500

    def __init__(
        self,
        workspace: str | Path | None = None,
        budget_per_message: int = DEFAULT_BUDGET_PER_MESSAGE,
        persist_threshold: int = PERSIST_THRESHOLD,
    ):
        """初始化工具结果预算管理器。

        参数:
            workspace: 工作目录路径，用于存放持久化文件
            budget_per_message: 每条消息的工具结果字符预算
            persist_threshold: 触发持久化的内容大小阈值
        """
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._budget = budget_per_message
        self._persist_threshold = persist_threshold
        self._results_dir = self._workspace / ".mini-code-tool-results"
        self._persisted: dict[str, ToolResultPersisted] = {}

    def check_and_replace(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """检查消息中的工具结果是否超出预算，将超大的持久化并替换为预览。

        参数:
            messages: 待检查的消息列表

        返回:
            包含修改后的消息列表和节省的字节数的元组
        """
        if not self._results_dir.exists():
            self._results_dir.mkdir(parents=True, exist_ok=True)

        modified = list(messages)
        bytes_saved = 0

        for i, msg in enumerate(modified):
            if msg.get("role") != "tool_result":
                continue

            content = msg.get("content")
            # 将 content 统一为字符串——tool_result 的 content 可能为 None
            # （无输出）或非字符串（结构化结果）。若不做此处理，
            # len(None) / len(list) 会崩溃或导致大小计算错误。
            if not isinstance(content, str):
                content = "" if content is None else str(content)
                modified[i] = {**msg, "content": content}
                msg = modified[i]
            content_size = len(content)

            if content_size <= self._persist_threshold:
                continue

            tool_name = msg.get("toolName", "unknown")
            persisted = self._persist_content(content, tool_name, i)

            preview = self._generate_preview(content, tool_name, persisted.persisted_path)
            modified[i] = {**msg, "content": preview, "_persisted_path": str(persisted.persisted_path)}
            self._persisted[f"{i}-{tool_name}"] = persisted
            bytes_saved += content_size - len(preview)

        return modified, bytes_saved

    def _persist_content(
        self, content: str, tool_name: str, index: int
    ) -> ToolResultPersisted:
        """将内容以原子写入方式持久化到磁盘。

        参数:
            content: 待持久化的内容字符串
            tool_name: 工具名称
            index: 消息在列表中的索引

        返回:
            持久化记录的 ToolResultPersisted 对象
        """
        safe_name = f"{tool_name}_{index}_{int(time.time() * 1000)}.txt"
        path = self._results_dir / safe_name

        meta = {
            "tool_name": tool_name,
            "message_index": index,
            "original_size": len(content),
            "timestamp": time.time(),
        }
        header = json.dumps(meta, ensure_ascii=False) + "\n---CONTENT---\n"

        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._results_dir), prefix=".tool_result_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(content)
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return ToolResultPersisted(
            original_size=len(content),
            persisted_path=path,
            preview_text="",
            tool_name=tool_name,
        )

    def _generate_preview(
        self, content: str, tool_name: str, path: Path
    ) -> str:
        """为已持久化的内容生成预览文本。

        参数:
            content: 原始内容字符串
            tool_name: 工具名称
            path: 持久化文件的路径

        返回:
            预览文本字符串，长度不超过 PREVIEW_MAX_CHARS
        """
        lines = content.splitlines()
        head_lines = lines[:8]
        tail_lines = lines[-3:] if len(lines) > 12 else []

        parts = [
            f"[Tool result persisted to disk — {len(content)} chars]",
            f"Tool: {tool_name}",
            f"Path: {path.name}",
            "",
            "--- Preview (first/last lines) ---",
        ]
        parts.extend(head_lines)
        if tail_lines:
            parts.append(f"... ({len(lines) - len(head_lines) - len(tail_lines)} lines omitted) ...")
            parts.extend(tail_lines)

        preview = "\n".join(parts)
        return preview[:self.PREVIEW_MAX_CHARS]

    def get_persisted_count(self) -> int:
        """获取已持久化的工具结果数量。

        返回:
            持久化记录的总数
        """
        return len(self._persisted)

    def get_total_saved_bytes(self) -> int:
        """获取通过持久化总共节省的字节数。

        返回:
            所有持久化记录原始大小之和
        """
        return sum(r.original_size for r in self._persisted.values())


# ---------------------------------------------------------------------------
# Phase 3: Read Deduplication
# ---------------------------------------------------------------------------


class ReadDedupManager:
    """基于哈希的文件读取去重管理器。

    当同一文件（相同路径 + 相同内容哈希）被再次读取时，
    返回存根而非重新将完整内容注入上下文。
    """
    def __init__(self):
        """初始化读取去重管理器。"""
        self._entries: dict[str, ReadDedupEntry] = {}  # file_path -> entry
        self._stub_template = (
            "File unchanged since last read. "
            "The content from the earlier Read tool_result "
            "in this conversation is still current — refer to that instead."
        )

    def register_read(
        self, file_path: str, content: str, message_index: int
    ) -> bool:
        """注册一次文件读取操作。

        参数:
            file_path: 文件路径
            content: 文件内容
            message_index: 当前消息在列表中的索引

        返回:
            如果是新的或内容变化的读取则返回 True，重复读取则返回 False
        """
        content_hash = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()

        existing = self._entries.get(file_path)
        if existing and existing.content_hash == content_hash:
            return False  # 重复读取

        self._entries[file_path] = ReadDedupEntry(
            file_path=file_path,
            content_hash=content_hash,
            timestamp=time.time(),
            message_index=message_index,
        )
        return True  # 新文件或内容已变化

    def should_dedup(self, file_path: str, content: str) -> bool:
        """检查本次读取是否可以被去重。

        参数:
            file_path: 文件路径
            content: 文件内容

        返回:
            如果内容与上次读取一致则返回 True
        """
        content_hash = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()
        existing = self._entries.get(file_path)
        return existing is not None and existing.content_hash == content_hash

    def get_stub(self, file_path: str) -> str:
        """获取已读取文件的去重存根文本。

        参数:
            file_path: 文件路径

        返回:
            去重存根文本，如果从未读取过该文件则返回空字符串
        """
        entry = self._entries.get(file_path)
        if not entry:
            return ""
        return (
            f"[Read deduplicated: {file_path}]\n"
            f"{self._stub_template}\n"
            f"(Original content at message index {entry.message_index})"
        )

    def invalidate(self, file_path: str) -> None:
        """使指定文件的缓存失效（例如写入后调用）。

        参数:
            file_path: 要失效的文件路径
        """
        self._entries.pop(file_path, None)

    def clear(self) -> None:
        """清空所有读取去重缓存。"""
        self._entries.clear()


# ---------------------------------------------------------------------------
# Phase 4: Time-based Microcompact
# ---------------------------------------------------------------------------


class MicrocompactEngine:
    """轻量级预压缩优化引擎。

    清除不太可能仍在提示缓存中的旧工具结果（基于时间），
    从而降低下一次 API 调用的重写成本。
    """
    def __init__(self, config: MicrocompactState | None = None):
        """初始化微压缩引擎。

        参数:
            config: 微压缩状态配置，如果为 None 则使用默认配置
        """
        self._state = config or MicrocompactState()

    def run_time_based_microcompact(
        self,
        messages: list[dict[str, Any]],
        now: float | None = None,
    ) -> CompactionResult:
        """根据上次助手响应以来的时间，清除旧的工具结果。

        不会生成摘要，仅将旧的 tool_result 内容替换为固定标记文本。

        参数:
            messages: 待处理的消息列表
            now: 当前时间戳，如果为 None 则使用 time.time()

        返回:
            包含处理结果的 CompactionResult 对象
        """
        now = now or time.time()
        elapsed = now - self._state.last_time_based_compact

        if elapsed < self._state.time_based_interval:
            return CompactionResult(
                success=False,
                strategy=CompactStrategy.MICROCOMPACT,
                trigger=CompactTrigger.MICROCOMPACT_TIME,
                messages=messages,
            )

        tool_results = [
            (i, m) for i, m in enumerate(messages)
            if m.get("role") == "tool_result"
            and not m.get("content", "").startswith("[Tool result persisted")
            and not m.get("content", "").startswith("[Old tool result")
        ]

        if len(tool_results) <= self._state.keep_recent_tool_results:
            return CompactionResult(
                success=False,
                strategy=CompactStrategy.MICROCOMPACT,
                trigger=CompactTrigger.MICROCOMPACT_TIME,
                messages=messages,
            )

        modified = list(messages)
        cleared_count = 0
        tokens_cleared = 0

        # 保留最近的 N 条，清除更早的
        keep_indices = {idx for idx, _ in tool_results[-self._state.keep_recent_tool_results:]}

        for idx, msg in tool_results:
            if idx in keep_indices:
                continue

            old_content = msg.get("content", "")
            old_size = len(old_content)
            modified[idx] = {
                **msg,
                "content": "[Old tool result content cleared by time-based microcompact]",
                "_microcompacted": True,
            }
            cleared_count += 1
            tokens_cleared += old_size // 4  # 粗略的 token 估算

        self._state.last_time_based_compact = now
        self._state.total_tokens_cleared += tokens_cleared

        logger.info(
            "Time-based microcompact: cleared %d old tool results (~%d tokens)",
            cleared_count,
            tokens_cleared,
        )

        return CompactionResult(
            success=True,
            strategy=CompactStrategy.MICROCOMPACT,
            trigger=CompactTrigger.MICROCOMPACT_TIME,
            messages=modified,
            tokens_freed=tokens_cleared,
        )


# ---------------------------------------------------------------------------
# Phase 5: Session Memory Compact
# ---------------------------------------------------------------------------


class SessionMemoryCompactEngine:
    """使用现有的 MemoryManager 条目作为压缩摘要基础。

    不调用模型生成摘要，而是利用已维护的记忆条目（项目决策、约定、
    模式）来构建压缩摘要，同时保留最近的消息作为尾部。
    """
    TAIL_MIN_TOKENS = 10000
    TAIL_MIN_MESSAGES = 5
    TAIL_MAX_TOKENS = 40000

    def __init__(self, memory_manager=None):
        """初始化会话记忆压缩引擎。

        参数:
            memory_manager: 记忆管理器实例，用于获取上下文摘要
        """
        self._memory = memory_manager

    def try_session_memory_compact(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        estimate_fn=None,
        config: AutoCompactConfig | None = None,
    ) -> CompactionResult | None:
        """尝试执行会话记忆压缩。如果不适用则返回 None。

        参数:
            messages: 消息列表
            context_window: 上下文窗口大小（token 数）
            estimate_fn: 估算单个消息 token 数的函数
            config: 自动压缩配置

        返回:
            压缩结果对象，如果不适用则返回 None
        """
        config = config or AutoCompactConfig()

        if not config.session_memory_enabled:
            return None

        if self._memory is None:
            return None

        # 获取记忆上下文作为摘要基础
        memory_context = self._memory.get_relevant_context(max_tokens=6000)
        if not memory_context.strip():
            return None  # 没有可用记忆，回退到完整压缩

        # 确定截断位置：保留最近的尾部
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 从末尾开始计算尾部
        tail_tokens = 0
        tail_start = len(non_system)
        estimate = estimate_fn or (lambda m: len(str(m)) // 4)

        for i in range(len(non_system) - 1, -1, -1):
            msg_tokens = estimate(non_system[i])
            if tail_tokens + msg_tokens > config.max_expand_tokens and \
               (len(non_system) - i) >= config.min_keep_messages:
                tail_start = i + 1
                break
            tail_tokens += msg_tokens

        if tail_tokens < self.TAIL_MIN_TOKENS:
            tail_start = max(0, len(non_system) - config.min_keep_messages)

        # 确保不破坏 tool_use/tool_result 配对
        tail_start = self._adjust_for_tool_pair(non_system, tail_start)

        # 构建压缩后的消息
        boundary = CompactBoundary(
            trigger=CompactTrigger.AUTO,
            strategy=CompactStrategy.SESSION_MEMORY,
            tokens_before=sum(estimate(m) for m in messages),
        )

        compacted = []
        compacted.append({
            "role": "system",
            "content": (
                f"[Context compacted at {time.strftime('%H:%M:%S')} via Session Memory]\n"
                f"Messages removed: {tail_start}. Tokens before: ~{boundary.tokens_before}\n\n"
                f"## Project Memory & Context\n\n{memory_context}\n\n"
                "--- Recent conversation continues below ---"
            ),
            "_compact_boundary": True,
        })

        # 添加保留的尾部
        tail = non_system[tail_start:]
        compacted.extend(tail)

        # 在前面重新添加系统消息
        final = system_msgs + compacted

        boundary.tokens_after = sum(estimate(m) for m in final)
        boundary.messages_removed = len(messages) - len(final)
        boundary.preserved_segment = (tail_start + len(system_msgs), len(final) - 1)

        # 检查压缩是否真正有效
        if boundary.tokens_after >= boundary.tokens_before * 0.95:
            return None  # 节省不够

        logger.info(
            "Session Memory Compact: %d → %d tokens (%d freed)",
            boundary.tokens_before,
            boundary.tokens_after,
            boundary.tokens_before - boundary.tokens_after,
        )

        return CompactionResult(
            success=True,
            strategy=CompactStrategy.SESSION_MEMORY,
            trigger=CompactTrigger.AUTO,
            messages=final,
            boundary=boundary,
            tokens_freed=boundary.tokens_before - boundary.tokens_after,
            summary_text=memory_context,
        )

    @staticmethod
    def _adjust_for_tool_pair(messages: list[dict], cut_point: int) -> int:
        """调整截断点以避免破坏 tool_use/tool_result 配对。

        参数:
            messages: 消息列表
            cut_point: 原始截断点

        返回:
            调整后的截断点
        """
        adjusted = cut_point

        # 从截断点向前扫描，处理孤立的 tool_result
        for i in range(adjusted, len(messages)):
            if messages[i].get("role") == "tool_result":
                # 检查对应的 tool_use 是否在截断点之前
                found_match = False
                for j in range(max(0, adjusted - 10), adjusted):
                    if (messages[j].get("role") == "assistant" and
                        isinstance(messages[j].get("content"), list) and
                        any(b.get("type") == "tool_use" for b in messages[j]["content"] if isinstance(b, dict))):
                        found_match = True
                        break
                if not found_match:
                    adjusted = i + 1

        # 从截断点向后扫描，处理孤立的 tool_use
        for i in range(adjusted - 1, max(0, adjusted - 10), -1):
            msg = messages[i]
            if (msg.get("role") == "assistant" and
                isinstance(msg.get("content"), list) and
                any(b.get("type") == "tool_use" for b in msg["content"] if isinstance(b, dict))):
                # 检查截断点之后是否有对应的 tool_result
                has_result = any(
                    m.get("role") == "tool_result"
                    for m in messages[adjusted:]
                )
                if has_result:
                    adjusted = min(adjusted, i)
                    break

        return max(0, adjusted)


# ---------------------------------------------------------------------------
# Phase 6: Auto Compact High-Water Dispatcher
# ---------------------------------------------------------------------------


class AutoCompactDispatcher:
    """高水位线自动压缩调度器。

    不是多级百分比选择器。而是：
    - 监控 token 使用量是否超过阈值
    - 优先尝试会话记忆压缩
    - 回退到完整压缩
    - 包含连续失败时的断路器机制
    """
    def __init__(
        self,
        context_window: int = 200000,
        config: AutoCompactConfig | None = None,
        memory_manager=None,
        estimate_fn=None,
    ):
        """初始化自动压缩调度器。

        参数:
            context_window: 上下文窗口大小（token 数）
            config: 自动压缩配置，为 None 时使用默认配置
            memory_manager: 记忆管理器实例
            estimate_fn: 估算消息 token 数的函数
        """
        self._context_window = context_window
        self._config = config or AutoCompactConfig()
        self._memory = memory_manager
        self._estimate = estimate_fn or (lambda m: len(str(m)) // 4)
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._boundaries: list[CompactBoundary] = []
        self._suppressed_until: float = 0.0  # 压缩后的警告抑制时间
        self._session_memory_engine = SessionMemoryCompactEngine(memory_manager)
        self._microcompact = MicrocompactEngine()

    @property
    def threshold_tokens(self) -> int:
        """获取触发自动压缩的阈值 token 数。

        返回:
            基于上下文窗口和阈值比例计算的触发 token 数
        """
        return int(self._context_window * self._config.threshold_ratio)

    @property
    def blocking_limit(self) -> int:
        """获取阻塞限制的 token 数（接近上下文窗口上限）。

        返回:
            上下文窗口的 97% 作为阻塞限制
        """
        return int(self._context_window * 0.97)

    @property
    def is_tripped(self) -> bool:
        """纯谓词：判断是否已达到断路器触发阈值。无副作用（调用方如 ReactiveCompactEngine 仅做读取检查）。

        返回:
            连续失败次数达到断路器限制时返回 True
        """
        return self._consecutive_failures >= self._config.circuit_breaker_limit

    def _maybe_auto_recover(self) -> bool:
        """如果断路器已触发但已过恢复超时时间，重置为半开状态。

        返回:
            如果刚刚执行了恢复重置则返回 True。若没有此机制，
            一旦触发，should_trigger 始终返回 False，_on_success 永远不会执行，
            断路器将在整个会话期间保持打开状态。
        """
        if self._consecutive_failures < self._config.circuit_breaker_limit:
            return False
        recovery = self._config.circuit_breaker_recovery_seconds
        if (
            recovery > 0
            and self._last_failure_time > 0
            and time.time() - self._last_failure_time >= recovery
        ):
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            logger.info(
                "Auto Compact circuit breaker auto-recovered after %.0fs", recovery
            )
            return True
        return False

    def should_trigger(
        self,
        messages: list[dict[str, Any]],
        token_usage: int | None = None,
    ) -> bool:
        """检查是否应触发自动压缩。

        参数:
            messages: 消息列表
            token_usage: 当前 token 使用量，为 None 时自动估算

        返回:
            如果 token 使用量达到或超过阈值且断路器未阻断则返回 True
        """
        if not self._config.enabled:
            return False
        if self.is_tripped:
            # 半开自动恢复：如果已过恢复超时，允许重试；否则保持阻断
            if not self._maybe_auto_recover():
                return False

        usage = token_usage or sum(self._estimate(m) for m in messages)
        return usage >= self.threshold_tokens

    def dispatch(
        self,
        messages: list[dict[str, Any]],
        token_usage: int | None = None,
        force_full: bool = False,
    ) -> CompactionResult:
        """执行自动压缩调度：优先尝试会话记忆压缩，失败则执行完整压缩。

        参数:
            messages: 消息列表
            token_usage: 当前 token 使用量
            force_full: 是否强制使用完整压缩（跳过会话记忆压缩）

        返回:
            压缩结果对象
        """
        if not self.should_trigger(messages, token_usage) and not force_full:
            return CompactionResult(
                success=False,
                strategy=CompactStrategy.FULL,
                trigger=CompactTrigger.AUTO,
                messages=messages,
            )

        usage = token_usage or sum(self._estimate(m) for m in messages)
        logger.info(
            "Auto Compact dispatch: usage=%d, threshold=%d, circuit_breaker=%s",
            usage,
            self.threshold_tokens,
            "TRIPPED" if self.is_tripped else "OK",
        )

        # 优先尝试会话记忆压缩（除非强制完整压缩）
        if not force_full:
            sm_result = self._session_memory_engine.try_session_memory_compact(
                messages,
                self._context_window,
                self._estimate,
                self._config,
            )
            if sm_result and sm_result.effective:
                self._on_success(sm_result.boundary)
                self._suppress_warnings()
                return sm_result

        # 回退到完整压缩
        return self._run_full_compact(messages, usage)

    def _run_full_compact(
        self, messages: list[dict[str, Any]], usage: int
    ) -> CompactionResult:
        """执行完整压缩：生成摘要并创建新的基准线。

        参数:
            messages: 消息列表
            usage: 当前 token 使用量

        返回:
            包含压缩结果和摘要的 CompactionResult 对象
        """
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= self._config.min_keep_messages:
            self._on_failure()
            return CompactionResult(
                success=False,
                strategy=CompactStrategy.FULL,
                trigger=CompactTrigger.AUTO,
                messages=messages,
                error="Too few messages to compact",
            )

        # 从对话结构中生成摘要
        summary = self._generate_structured_summary(non_system)

        boundary = CompactBoundary(
            trigger=CompactTrigger.AUTO,
            strategy=CompactStrategy.FULL,
            tokens_before=usage,
        )

        # 构建压缩后的消息：系统消息 + 边界 + 摘要 + 保留的尾部
        compacted = list(system_msgs)
        compacted.append({
            "role": "system",
            "content": (
                f"[Context compacted at {time.strftime('%H:%M:%S')} — Full Compact]\n"
                f"Original: ~{usage} tokens, {len(messages)} messages\n\n"
                f"## Conversation Summary\n\n{summary}"
            ),
            "_compact_boundary": True,
        })

        # 保留最近的尾部
        tail_size = min(len(non_system) // 3, self._config.min_keep_messages)
        tail = non_system[-tail_size:] if tail_size > 0 else []
        compacted.extend(tail)

        boundary.tokens_after = sum(self._estimate(m) for m in compacted)
        boundary.messages_removed = len(messages) - len(compacted)

        self._on_success(boundary)
        self._suppress_warnings()

        logger.info(
            "Full Compact: %d → %d tokens (%d removed)",
            boundary.tokens_before,
            boundary.tokens_after,
            boundary.messages_removed,
        )

        return CompactionResult(
            success=True,
            strategy=CompactStrategy.FULL,
            trigger=CompactTrigger.AUTO,
            messages=compacted,
            boundary=boundary,
            tokens_freed=boundary.tokens_before - boundary.tokens_after,
            summary_text=summary,
        )

    def _generate_structured_summary(self, messages: list[dict]) -> str:
        """无需调用 LLM，从消息历史中生成结构化摘要。

        参数:
            messages: 非系统消息列表

        返回:
            格式化的结构化摘要字符串
        """
        parts = ["### Summary of conversation so far:\n"]

        # 提取关键信息模式
        user_topics = []
        tool_calls_made = set()
        files_mentioned = set()
        errors_seen = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user" and isinstance(content, str) and len(content) > 10:
                topic = content[:100].replace("\n", " ")
                user_topics.append(topic)

            if role == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls_made.add(block.get("name", "unknown"))
                        input_data = block.get("input", {})
                        if "file_path" in input_data:
                            files_mentioned.add(input_data["file_path"])

            if role == "tool_result":
                err = msg.get("isError")
                if err:
                    errors_seen.append(content[:80] if isinstance(content, str) else str(content)[:80])

        if user_topics:
            parts.append("**Topics discussed:**\n")
            for t in user_topics[:8]:
                parts.append(f"- {t}")
            parts.append("")

        if tool_calls_made:
            parts.append(f"**Tools used:** {', '.join(sorted(tool_calls_made))}\n")

        if files_mentioned:
            parts.append(f"**Files touched:** {', '.join(sorted(files_mentioned)[:10])}\n")

        if errors_seen:
            parts.append("**Errors encountered:**\n")
            for e in errors_seen[:3]:
                parts.append(f"- {e}")
            parts.append("")

        parts.append("\n*Continue from where we left off.*")
        return "\n".join(parts)

    def _on_success(self, boundary: CompactBoundary | None) -> None:
        """处理压缩成功的回调：重置连续失败计数。

        参数:
            boundary: 本次压缩的边界对象
        """
        self._consecutive_failures = 0
        if boundary:
            self._boundaries.append(boundary)

    def _on_failure(self) -> None:
        """处理压缩失败的回调：增加连续失败计数并记录日志。"""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        logger.warning(
            "Auto Compact failure #%d/%d (circuit breaker)",
            self._consecutive_failures,
            self._config.circuit_breaker_limit,
        )

    def _suppress_warnings(self, duration: float = 30.0) -> None:
        """在指定时间内抑制警告，避免频繁提示用户。

        参数:
            duration: 抑制持续时间（秒），默认 30 秒
        """
        self._suppressed_until = time.time() + duration

    def is_warning_suppressed(self) -> bool:
        """检查当前是否处于警告抑制状态。

        返回:
            如果在抑制时间内则返回 True
        """
        return time.time() < self._suppressed_until

    def reset_circuit_breaker(self) -> None:
        """手动重置断路器，将连续失败计数归零。"""
        self._consecutive_failures = 0

    def get_history(self) -> list[CompactBoundary]:
        """获取所有压缩边界的列表。

        返回:
            包含每次成功压缩的 CompactBoundary 对象列表
        """
        return list(self._boundaries)

    def get_last_boundary(self) -> CompactBoundary | None:
        """获取最近一次压缩的边界。

        返回:
            最近的 CompactBoundary 对象，如果没有则返回 None
        """
        return self._boundaries[-1] if self._boundaries else None


# ---------------------------------------------------------------------------
# Phase 7: Reactive Compact (Error Recovery)
# ---------------------------------------------------------------------------


class ReactiveCompactEngine:
    """API 调用失败后的错误恢复压缩。

    当模型 API 因以下原因拒绝请求时触发：
    - 提示过长 (prompt too long)
    - 媒体大小超限 (media size exceeded)
    - 其他可恢复的错误
    """
    MAX_RETRIES = 3

    def __init__(
        self,
        auto_compact: AutoCompactDispatcher | None = None,
        estimate_fn=None,
    ):
        """初始化响应式压缩引擎。

        参数:
            auto_compact: 自动压缩调度器实例
            estimate_fn: 估算消息 token 数的函数
        """
        self._auto_compact = auto_compact
        self._estimate = estimate_fn or (lambda m: len(str(m)) // 4)
        self._recovery_attempts = 0

    def try_recover_from_overflow(
        self,
        messages: list[dict[str, Any]],
        error_message: str = "",
    ) -> CompactionResult | None:
        """尝试从提示过长错误中恢复。

        策略：
        1. 使用激进截断的强制完整压缩
        2. 如果仍然过长，丢弃最旧的 API 轮次组
        3. 最多尝试 MAX_RETRIES 次

        参数:
            messages: 消息列表
            error_message: 原始错误信息

        返回:
            恢复成功时返回 CompactionResult，失败时返回 None
        """
        self._recovery_attempts += 1
        if self._recovery_attempts > self.MAX_RETRIES:
            logger.error("Reactive Compact: max retries (%d) exceeded", self.MAX_RETRIES)
            return None

        logger.info(
            "Reactive Compact attempt %d/%d: recovering from overflow",
            self._recovery_attempts,
            self.MAX_RETRIES,
        )

        # 使用自动压缩的强制完整模式
        if self._auto_compact:
            # 临时重置断路器以允许恢复
            original_tripped = self._auto_compact.is_tripped
            if original_tripped:
                self._auto_compact.reset_circuit_breaker()

            result = self._auto_compact.dispatch(messages, force_full=True)

            # 检查结果是否足够小
            result_usage = sum(self._estimate(m) for m in result.messages)
            if result_usage < self._auto_compact.blocking_limit * 0.9:
                self._recovery_attempts = 0  # 成功时重置
                return CompactionResult(
                    success=True,
                    strategy=CompactStrategy.REACTIVE,
                    trigger=CompactTrigger.REACTIVE,
                    messages=result.messages,
                    boundary=result.boundary,
                    tokens_freed=result.tokens_freed,
                )

        # 激进回退：直接截断最旧的消息
        # 仅在仍在重试预算内时执行
        if self._recovery_attempts > self.MAX_RETRIES:
            logger.error("Reactive Compact: max retries (%d) exceeded in fallback", self.MAX_RETRIES)
            return None
        return self._aggressive_truncate(messages)

    def _aggressive_truncate(
        self, messages: list[dict[str, Any]]
    ) -> CompactionResult:
        """以激进方式截断消息，使其符合限制范围。

        参数:
            messages: 消息列表

        返回:
            截断后的 CompactionResult 对象
        """
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 仅保留最近的部分，逐步降低保留比例
        keep_ratio = 0.4 - (self._recovery_attempts * 0.1)  # 渐进式截断
        keep_count = max(3, int(len(non_system) * max(keep_ratio, 0.15)))

        truncated = list(system_msgs)
        truncated.append({
            "role": "system",
            "content": (
                f"[Context aggressively truncated for recovery — attempt {self._recovery_attempts}]\n"
                f"Earlier conversation was removed to fit context limits."
            ),
            "_reactive_compact": True,
        })
        truncated.extend(non_system[-keep_count:])

        boundary = CompactBoundary(
            trigger=CompactTrigger.REACTIVE,
            strategy=CompactStrategy.REACTIVE,
            tokens_before=sum(self._estimate(m) for m in messages),
            tokens_after=sum(self._estimate(m) for m in truncated),
            messages_removed=len(messages) - len(truncated),
        )

        return CompactionResult(
            success=True,
            strategy=CompactStrategy.REACTIVE,
            trigger=CompactTrigger.REACTIVE,
            messages=truncated,
            boundary=boundary,
            tokens_freed=boundary.tokens_before - boundary.tokens_after,
        )


# ---------------------------------------------------------------------------
# Unified Context Manager (Orchestrates all phases)
# ---------------------------------------------------------------------------


class ContextCompactor:
    """统一的上下文管理编排器。

    实现完整的 Claude Code 风格流水线：

    第 1 步：构建活动上下文（从最后一个边界开始）
    第 2 步：应用工具结果预算
    第 3 步：读取去重
    第 4 步：微压缩
    第 5 步：自动压缩高水位检查
    第 6 步：调度（会话记忆 → 完整压缩）
    第 7 步：响应式恢复（如果需要）
    """
    def __init__(
        self,
        context_window: int = 200000,
        workspace: str | Path | None = None,
        memory_manager=None,
        estimate_fn=None,
        config: AutoCompactConfig | None = None,
    ):
        """初始化统一上下文压缩器。

        参数:
            context_window: 上下文窗口大小（token 数）
            workspace: 工作目录路径
            memory_manager: 记忆管理器实例
            estimate_fn: 估算消息 token 数的函数
            config: 自动压缩配置
        """
        self._context_window = context_window
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._config = config or AutoCompactConfig()

        self._tool_budget = ToolResultBudgetManager(workspace)
        self._read_dedup = ReadDedupManager()
        self._microcompact = MicrocompactEngine()
        self._auto_compact = AutoCompactDispatcher(
            context_window=context_window,
            config=config,
            memory_manager=memory_manager,
            estimate_fn=estimate_fn,
        )
        self._reactive = ReactiveCompactEngine(self._auto_compact, estimate_fn)
        self._estimate = estimate_fn or (lambda m: len(str(m)) // 4)

        self._last_compact_result: CompactionResult | None = None
        self._total_optimization_passes = 0

    def process_request(
        self,
        messages: list[dict[str, Any]],
        *,
        enable_tool_budget: bool = True,
        enable_read_dedup: bool = True,
        enable_microcompact: bool = True,
        enable_auto_compact: bool = True,
    ) -> CompactionResult:
        """运行完整的请求前优化流水线。

        这是每次 API 请求前调用的主入口点。

        参数:
            messages: 消息列表
            enable_tool_budget: 是否启用工具结果预算
            enable_read_dedup: 是否启用读取去重
            enable_microcompact: 是否启用微压缩
            enable_auto_compact: 是否启用自动压缩

        返回:
            包含优化结果的 CompactionResult 对象
        """
        self._total_optimization_passes += 1
        current = list(messages)
        total_freed = 0
        steps_taken = []

        # Step 2: 工具结果预算
        if enable_tool_budget:
            current, budget_saved = self._tool_budget.check_and_replace(current)
            if budget_saved > 0:
                total_freed += budget_saved
                steps_taken.append(f"tool_budget({budget_saved})")

        # Step 3: 读取去重（在工具级别处理，此处仅跟踪状态）
        # 读取去重主要在处理工具结果时使用

        # Step 4: 微压缩
        if enable_microcompact:
            mc_result = self._microcompact.run_time_based_microcompact(current)
            if mc_result.effective:
                current = mc_result.messages
                total_freed += mc_result.tokens_freed
                steps_taken.append(f"microcompact({mc_result.tokens_freed})")

        # Step 5+6: 自动压缩高水位调度
        if enable_auto_compact and self._auto_compact.should_trigger(current):
            ac_result = self._auto_compact.dispatch(current)
            if ac_result.effective:
                current = ac_result.messages
                total_freed += ac_result.tokens_freed
                steps_taken.append(f"auto_compact({ac_result.strategy.value},{ac_result.tokens_freed})")
                self._last_compact_result = ac_result

        result = CompactionResult(
            success=total_freed > 0,
            strategy=CompactStrategy.FULL,
            trigger=CompactTrigger.AUTO,
            messages=current,
            tokens_freed=total_freed,
            summary_text=f"Optimization steps: {' + '.join(steps_taken)}" if steps_taken else "",
        )

        logger.info(
            "ContextCompactor pass #%d: %d tokens freed across [%s]",
            self._total_optimization_passes,
            total_freed,
            ", ".join(steps_taken) if steps_taken else "none",
        )

        return result

    def reactive_recover(
        self, messages: list[dict[str, Any]], error: str = ""
    ) -> CompactionResult | None:
        """在 API 错误后尝试响应式恢复。

        参数:
            messages: 消息列表
            error: 原始错误信息

        返回:
            恢复成功时返回 CompactionResult，失败时返回 None
        """
        return self._reactive.try_recover_from_overflow(messages, error)

    @property
    def tool_budget(self) -> ToolResultBudgetManager:
        """获取工具结果预算管理器实例。"""
        return self._tool_budget

    @property
    def read_dedup(self) -> ReadDedupManager:
        """获取读取去重管理器实例。"""
        return self._read_dedup

    @property
    def auto_compact(self) -> AutoCompactDispatcher:
        """获取自动压缩调度器实例。"""
        return self._auto_compact

    @property
    def reactive(self) -> ReactiveCompactEngine:
        """获取响应式压缩引擎实例。"""
        return self._reactive

    @property
    def last_result(self) -> CompactionResult | None:
        """获取最近一次压缩的结果。"""
        return self._last_compact_result

    def get_stats(self) -> dict[str, Any]:
        """获取上下文压缩器的运行统计信息。

        返回:
            包含各组件统计数据的字典
        """
        return {
            "total_passes": self._total_optimization_passes,
            "tool_results_persisted": self._tool_budget.get_persisted_count(),
            "tool_bytes_saved": self._tool_budget.get_total_saved_bytes(),
            "read_dedup_entries": len(self._read_dedup._entries),
            "microcompact_tokens_cleared": self._microcompact._state.total_tokens_cleared,
            "auto_compact_boundaries": len(self._auto_compact.get_history()),
            "circuit_breaker_tripped": self._auto_compact.is_tripped,
            "reactive_recovery_attempts": self._reactive._recovery_attempts,
            "context_window": self._context_window,
            "auto_compact_threshold": self._auto_compact.threshold_tokens,
        }

    def format_pipeline_status(self) -> str:
        """将流水线状态格式化为可读的字符串。

        返回:
            格式化的流水线状态信息
        """
        stats = self.get_stats()
        lines = [
            "Context Management Pipeline Status",
            "=" * 40,
            f"Optimization passes: {stats['total_passes']}",
            f"Tool results persisted: {stats['tool_results_persisted']} ({stats['tool_bytes_saved']} bytes saved)",
            f"Read dedup cache: {stats['read_dedup_entries']} files",
            f"Microcompact cleared: ~{stats['microcompact_tokens_cleared']} tokens",
            f"Compact boundaries: {stats['auto_compact_boundaries']}",
            f"Circuit breaker: {'TRIPPED' if stats['circuit_breaker_tripped'] else 'OK'}",
            f"Reactive recoveries: {stats['reactive_recovery_attempts']}",
            "",
            f"Context window: {stats['context_window']:,} tokens",
            f"Auto compact threshold: {stats['auto_compact_threshold']:,} tokens ({self._config.threshold_ratio:.0%})",
        ]
        return "\n".join(lines)
