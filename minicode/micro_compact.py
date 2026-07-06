"""Micro-compaction 上下文管理压缩层。

在调用 API 前进行轻量级上下文压力释放，清除过期的工具结果而不影响
provider 的提示缓存。受到 Claude Code 的 microCompact / 基于时间的
microcompact 管线启发。

设计原则（受 CC 启发）：
  - 不调用任何 API —— 纯本地消息修剪
  - 保持 tool_use / tool_result 的 API 不变性（成对不被拆分）
  - 保留最近的 N 个结果不动
  - 缓存感知：只移除结果内容，不重构消息 ID 结构

使用方式（在 agent 循环内、model.next() 之前调用）：
    mc = MicroCompactor()
    messages = mc.compact(messages)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ── Compressible tool sets ──────────────────────────────────────────────────
# These tools produce content that is safe to discard when stale without
# affecting correctness in subsequent turns (the agent can re-read / re-search
# if it needs the data again).

COMPRESSIBLE_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "web_search",
    "web_fetch",
    "find_symbols",
    "find_references",
    "get_ast_info",
    "code_review",
    "diff_viewer",
    "file_tree",
    "json_parse",
    "csv_parse",
    "test_runner",
})

COMPRESSIBLE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "modify_file",
    "patch_file",
})

# Tool results that should NEVER be compressed (they carry persistent state)
NON_COMPRESSIBLE_TOOLS: frozenset[str] = frozenset({
    "todo_write",
    "task",
    "memory",
})


@dataclass
class MicroCompactionStats:
    """微压缩操作的统计信息，记录压缩前后的消息数和预估释放的 token 数。"""
    messages_before: int = 0
    messages_after: int = 0
    tokens_estimated_freed: int = 0
    reason: str = ""


@dataclass
class MicroCompactorConfig:
    """微压缩管线的配置参数。

    包含基于空闲时间的压缩、基于预算的压缩阈值，以及各自的启用开关。
    """

    # Time-based: if the gap between now and the last main-loop assistant message
    # exceeds this many seconds, clear all older compressible results.
    idle_threshold_seconds: int = 3600  # 60 minutes (matching CC's default)

    # Count-based: keep at most this many recent compressible result groups
    # intact.  Older groups are candidates for trimming.
    keep_recent_groups: int = 5  # matching CC's default

    # Budget: maximum token budget for tool results before triggering.
    # When the estimated total of compressible tool results exceeds this,
    # the micro-compactor trims older groups regardless of time.
    tool_result_budget_tokens: int = 40_000

    # Whether time-based micro-compaction is enabled.
    time_based_enabled: bool = True

    # Whether count-based (budget-aware) micro-compaction is enabled.
    budget_based_enabled: bool = True


@dataclass
class MicroCompactor:
    """轻量级工具结果修剪器，在每次 API 调用前运行。

    不调用任何外部 API。纯粹在内存消息列表上操作，仅移除属于可压缩工具集的
    tool_result 消息的内容，保持 tool_use / tool_result 成对关系的完整性。
    """

    config: MicroCompactorConfig = field(default_factory=MicroCompactorConfig)
    # Timestamp of the last main-loop assistant message seen.
    _last_assistant_ts: float = field(default_factory=time.time)

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        current_time: float | None = None,
    ) -> tuple[list[dict[str, Any]], MicroCompactionStats]:
        """执行微压缩管线并返回（压缩后的消息列表, 统计信息）。

        管线执行顺序（从最便宜的开始）：
          1. 基于时间 —— 如果空闲超过阈值，批量清除旧结果。
          2. 基于预算 —— 如果工具结果超过预算，修剪最旧的。

        参数:
            messages: 待压缩的消息列表。
            current_time: 可选的当前时间戳，用于测试时注入。

        返回:
            (压缩后的消息列表, MicroCompactionStats 统计信息)。
        """
        # if not messages:
            return messages, MicroCompactionStats()

        now = current_time or time.time()
        stats = MicroCompactionStats(messages_before=len(messages))

        # ── 1. Time-based check ──────────────────────────────────────────
        if self.config.time_based_enabled:
            idle_sec = now - self._last_assistant_ts
            if idle_sec >= self.config.idle_threshold_seconds:
                result = self._compact_time_based(messages, now)
                if result is not None:
                    messages = result
                    stats.reason = f"time_based (idle={idle_sec:.0f}s)"
                    stats.messages_after = len(messages)
                    return messages, stats

        # ── 2. Budget-based check ────────────────────────────────────────
        if self.config.budget_based_enabled:
            result = self._compact_budget_based(messages)
            if result is not None:
                messages = result
                stats.reason = "budget_exceeded"
                stats.messages_after = len(messages)
                return messages, stats

        stats.messages_after = len(messages)
        stats.reason = "no_action"
        return messages, stats

    def update_assistant_timestamp(self, ts: float | None = None) -> None:
        """记录主循环刚产生的一条 assistant 消息的时间戳。

        参数:
            ts: 可选的时间戳，不传则取当前时间。
        """
        # self._last_assistant_ts = ts or time.time()

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_compressible(result_msg: dict[str, Any]) -> bool:
        """判断一条 tool_result 消息是否引用了可压缩的工具。

        根据消息 data 中的 tool_name 或 name 字段判断该工具是否属于
        可压缩工具集合，且不属于不可压缩集合。

        参数:
            result_msg: tool_result 消息字典。

        返回:
            该消息是否应被压缩。
        """
        # data = result_msg.get("data", {}) or {}
        tool_name = data.get("tool_name") or data.get("name") or ""
        if not tool_name:
            return False
        return (
            tool_name in COMPRESSIBLE_READ_ONLY_TOOLS
            or tool_name in COMPRESSIBLE_WRITE_TOOLS
        ) and tool_name not in NON_COMPRESSIBLE_TOOLS

    @staticmethod
    def _estimated_tokens(msg: dict[str, Any]) -> int:
        """粗略估算一条消息的 token 数量（字符数 / 4）。

        参数:
            msg: 消息字典。

        返回:
            估算的 token 数，至少为 1。
        """
        # content = msg.get("content", "") or ""
        return max(1, len(str(content)) // 4)

    def _compact_time_based(
        self,
        messages: list[dict[str, Any]],
        now: float,
    ) -> list[dict[str, Any]] | None:
        """清除早于保留边界的可压缩工具结果（基于空闲时间）。

        找到倒数第 keep_recent_groups 条 assistant 消息的位置，
        该位置之前的所有可压缩 tool_result 及其配对的 tool_use 均被丢弃。

        参数:
            messages: 完整消息列表。
            now: 当前时间戳。

        返回:
            修剪后的消息列表，若无需修剪则返回 None。
        """
        # # Find the last N assistant messages (main-loop, not parallel-split).
        assistant_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
        ]
        if len(assistant_indices) <= self.config.keep_recent_groups:
            return None  # Not enough groups to trim

        # The keep_boundary is the index of the keep_recent_groups-th
        # from the end.
        keep_start = assistant_indices[-self.config.keep_recent_groups]
        trimmed_count = 0

        new_messages: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i < keep_start:
                role = msg.get("role", "")
                if role == "tool_result" and self._is_compressible(msg):
                    trimmed_count += 1
                    continue  # drop the result
                if role == "tool_use":
                    # Check if the paired tool_result was just dropped
                    name = (msg.get("data", {}) or {}).get("tool_name") or (msg.get("data", {}) or {}).get("name") or ""
                    if name in COMPRESSIBLE_READ_ONLY_TOOLS | COMPRESSIBLE_WRITE_TOOLS:
                        trimmed_count += 1
                        continue  # drop orphaned tool_use
                # Also drop tool_result messages that are paired with
                # compressible tools even before the assistant boundary —
                # this is done by checking both the role and compressibility.
            new_messages.append(msg)

        if trimmed_count == 0:
            return None
        return new_messages

    def _compact_budget_based(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """当可压缩工具结果的总预估 token 超出预算时，修剪最旧的条目。

        超出预算时，优先丢弃最旧的可压缩结果（价值最低——模型已在其基础上构建，
        不再需要保留），直到剩余结果的总 token 数符合预算。
        受保护的最近组内的可压缩结果不会被触及。

        参数:
            messages: 完整消息列表。

        返回:
            修剪后的消息列表，若无需修剪则返回 None。
        """
        # assistant_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
        ]
        # Never trim compressible results inside the most recent keep_recent_groups.
        keep_from = max(
            0,
            assistant_indices[-self.config.keep_recent_groups]
            if len(assistant_indices) > self.config.keep_recent_groups
            else 0,
        )

        # Candidate compressible tool results before the keep boundary (oldest first).
        candidates = [
            i for i, m in enumerate(messages)
            if i < keep_from and m.get("role") == "tool_result" and self._is_compressible(m)
        ]
        budget = self.config.tool_result_budget_tokens
        total = sum(self._estimated_tokens(messages[i]) for i in candidates)
        if total <= budget:
            return None  # Under budget

        # Drop OLDEST candidates first until the kept total fits the budget.
        trim_indices: set[int] = set()
        running = total
        for i in candidates:  # oldest first
            if running <= budget:
                break
            running -= self._estimated_tokens(messages[i])
            trim_indices.add(i)

        # Drop orphaned tool_use messages whose paired result was trimmed
        # (paired by data.tool_id), so we don't leave dangling tool calls.
        trimmed_tool_ids = {
            (messages[i].get("data", {}) or {}).get("tool_id") for i in trim_indices
        }

        updated: list[dict[str, Any]] = []
        trimmed = 0
        for i, msg in enumerate(messages):
            if i in trim_indices:
                trimmed += 1
                continue
            if i < keep_from and msg.get("role") == "tool_use":
                data = msg.get("data", {}) or {}
                name = data.get("tool_name") or data.get("name") or ""
                if (
                    name in COMPRESSIBLE_READ_ONLY_TOOLS | COMPRESSIBLE_WRITE_TOOLS
                    and data.get("tool_id") in trimmed_tool_ids
                ):
                    trimmed += 1
                    continue
            updated.append(msg)

        return updated if trimmed > 0 else None


# ── Module-level convenience ─────────────────────────────────────────────────

_default_compactor: MicroCompactor | None = None


def get_micro_compactor() -> MicroCompactor:
    """获取或创建模块级别的微压缩器单例。

    返回:
        MicroCompactor 实例（全局唯一）。
    """
    # global _default_compactor
    if _default_compactor is None:
        _default_compactor = MicroCompactor()
    return _default_compactor


def micro_compact(
    messages: list[dict[str, Any]],
    *,
    current_time: float | None = None,
) -> tuple[list[dict[str, Any]], MicroCompactionStats]:
    """便捷函数：对消息列表执行微压缩。

    直接调用模块级别的微压缩器单例的 compact 方法。

    参数:
        messages: 待压缩的消息列表。
        current_time: 可选的当前时间戳，用于测试时注入。

    返回:
        (压缩后的消息列表, MicroCompactionStats 统计信息)。
    """
    # return get_micro_compactor().compact(messages, current_time=current_time)
