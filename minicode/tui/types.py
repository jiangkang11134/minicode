"""MiniCode TUI 数据类型定义。

提供 TranscriptEntry 数据类及其对象池管理函数，用于在 TUI 中
表示和复用对话转录条目。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class TranscriptEntry:
    """对话转录条目，表示 TUI 中的一条消息记录。

    包含用户消息、助手回复、进度通知和工具调用等类型的条目，
    以及运行时状态、折叠展示等元数据。
    """

    id: int
    kind: Literal["user", "assistant", "progress", "tool"]
    body: str
    category: str | None = None
    runtimeKind: str | None = None
    runtimeStep: int | None = None
    runtimePhase: str | None = None
    runtimeStopReason: str | None = None
    runtimeVerificationFocus: str | None = None
    toolName: str | None = None
    status: Literal["running", "success", "error"] | None = None
    collapsed: bool = False
    collapsedSummary: str | None = None
    collapsePhase: Literal[1, 2, 3] | None = None


# TranscriptEntry 对象池，减少频繁创建和 GC 压力
# Placed after the class definition so that runtime references resolve correctly.
_entry_pool: list[TranscriptEntry] = []
_POOL_MAX_SIZE = 100


def _create_transcript_entry(
    id: int,
    kind: Literal["user", "assistant", "progress", "tool"],
    body: str,
    category: str | None = None,
    runtimeKind: str | None = None,
    runtimeStep: int | None = None,
    runtimePhase: str | None = None,
    runtimeStopReason: str | None = None,
    runtimeVerificationFocus: str | None = None,
    toolName: str | None = None,
    status: Literal["running", "success", "error"] | None = None,
    collapsed: bool = False,
    collapsedSummary: str | None = None,
    collapsePhase: Literal[1, 2, 3] | None = None,
) -> TranscriptEntry:
    """创建 TranscriptEntry，使用对象池减少 GC 压力"""  # if _entry_pool:
        entry = _entry_pool.pop()
        entry.id = id
        entry.kind = kind
        entry.body = body
        entry.category = category
        entry.runtimeKind = runtimeKind
        entry.runtimeStep = runtimeStep
        entry.runtimePhase = runtimePhase
        entry.runtimeStopReason = runtimeStopReason
        entry.runtimeVerificationFocus = runtimeVerificationFocus
        entry.toolName = toolName
        entry.status = status
        entry.collapsed = collapsed
        entry.collapsedSummary = collapsedSummary
        entry.collapsePhase = collapsePhase
        return entry
    else:
        return TranscriptEntry(
            id=id,
            kind=kind,
            body=body,
            category=category,
            runtimeKind=runtimeKind,
            runtimeStep=runtimeStep,
            runtimePhase=runtimePhase,
            runtimeStopReason=runtimeStopReason,
            runtimeVerificationFocus=runtimeVerificationFocus,
            toolName=toolName,
            status=status,
            collapsed=collapsed,
            collapsedSummary=collapsedSummary,
            collapsePhase=collapsePhase,
        )


def _recycle_transcript_entry(entry: TranscriptEntry) -> None:
    """回收 TranscriptEntry 到对象池"""  # if len(_entry_pool) < _POOL_MAX_SIZE:
        _entry_pool.append(entry)
