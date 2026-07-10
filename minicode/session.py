"""会话持久化与恢复模块。

提供会话数据结构、自动保存机制和恢复能力，
使 SmartCode 能够在重启后保存和恢复对话状态。

使用增量差量保存以减少序列化开销：
- 仅将自上次保存以来新增/变更的消息追加写入
- 定期执行全量保存（每 N 次差量保存后）以保证一致性
- 字段级脏标记避免冗余序列化
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR
from minicode.logging_config import log_session_event

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SESSIONS_DIR = MINI_CODE_DIR / "sessions"
AUTOSAVE_INTERVAL_SECONDS = 30  # 自动保存的最小时间间隔（秒）

# 增量保存配置
DELTA_DIR_NAME = "deltas"        # 差量文件存放子目录
FULL_SAVE_INTERVAL = 10          # 每 N 次差量保存后执行一次全量保存
MAX_DELTA_FILES = 50             # 差量文件数量上限，超过后强制合并


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SessionMetadata:
    """会话列表使用的轻量级元数据。 """

    session_id: str                     # 会话唯一标识 ID
    created_at: float                   # 创建时间（Unix 时间戳）
    updated_at: float                   # 更新时间（Unix 时间戳）
    first_message: str = ""             # 截断后的首条用户消息
    last_message: str = ""              # 截断后的最后一条消息
    message_count: int = 0              # 消息总数
    workspace: str = ""                 # 会话启动时的工作目录
    runtime_summary: str = ""           # 紧凑的运行时时间线
    checkpoint_count: int = 0           # 存储的回退检查点数量
    instruction_summary: str = ""       # 指令层摘要
    hook_summary: str = ""              # 钩子状态摘要
    delegation_summary: str = ""        # 委派状态摘要
    extension_summary: str = ""         # 扩展清单摘要
    readiness_summary: str = ""         # 就绪报告摘要


@dataclass
class FileCheckpoint:
    """在写工具修改磁盘前捕获的持久化文件快照。 """

    checkpoint_id: str                  # 检查点唯一 ID（12 位十六进制）
    created_at: float                   # 创建时间（Unix 时间戳）
    file_path: str                      # 被修改的文件路径
    existed: bool                       # 文件之前是否存在
    previous_content: str               # 修改前的文件内容快照
    kind: str = "edit"                  # 检查点类型（edit/rewind）
    group_id: str = ""                  # 分组 ID（用于原子回退组）


@dataclass
class SessionData:
    """可被持久化和恢复的完整会话状态。 """
    session_id: str                     # 会话唯一标识 ID
    created_at: float                   # 创建时间（Unix 时间戳）
    updated_at: float                   # 最近更新时间（Unix 时间戳）
    workspace: str                      # 会话启动时的工作目录
    messages: list[dict[str, Any]] = field(default_factory=list)           # 对话消息列表
    transcript_entries: list[dict[str, Any]] = field(default_factory=list) # 转录条目列表
    history: list[str] = field(default_factory=list)                       # 提示历史
    permissions_summary: dict[str, Any] = field(default_factory=dict)      # 权限摘要
    skills: list[dict[str, Any]] = field(default_factory=list)             # 可用技能列表
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)        # MCP 服务器列表
    instruction_layers: list[dict[str, Any]] = field(default_factory=list) # 指令层列表
    hook_status: dict[str, Any] = field(default_factory=dict)              # 钩子状态
    delegated_tasks: list[dict[str, Any]] = field(default_factory=list)    # 委派任务列表
    delegation_status: dict[str, Any] = field(default_factory=dict)        # 委派状态
    extension_manifests: list[dict[str, Any]] = field(default_factory=list)# 扩展清单
    readiness_report: dict[str, Any] = field(default_factory=dict)         # 就绪报告
    checkpoints: list[FileCheckpoint] = field(default_factory=list)        # 文件检查点列表
    metadata: SessionMetadata = field(default=None)                        # 会话元数据

    # 增量保存追踪字段（不序列化，不参与 repr）
    _last_saved_msg_count: int = field(default=0, repr=False)        # 上次保存时的消息数量
    _last_saved_transcript_count: int = field(default=0, repr=False) # 上次保存时的转录数量
    _last_saved_checkpoint_count: int = field(default=0, repr=False) # 上次保存时的检查点数量
    _delta_save_count: int = field(default=0, repr=False)            # 增量保存累积次数
    _last_full_save_hash: str = field(default="", repr=False)        # 上次全量保存的内容哈希

    def __post_init__(self):
        """初始化元数据（如果未提供则自动创建）。"""
        if self.metadata is None:
            self.metadata = SessionMetadata(
                session_id=self.session_id,
                created_at=self.created_at,
                updated_at=self.updated_at,
                message_count=len(self.messages),
                workspace=self.workspace,
                checkpoint_count=len(self.checkpoints),
                instruction_summary=_summarize_instruction_layers(self.instruction_layers),
                hook_summary=_summarize_hook_status(self.hook_status),
                delegation_summary=_summarize_delegation_status(self.delegation_status),
                extension_summary=_summarize_extension_manifests(self.extension_manifests),
                readiness_summary=_summarize_readiness_report(self.readiness_report),
            )

    def update_metadata(self) -> None:
        """根据当前状态刷新元数据。"""
        self.updated_at = time.time()
        self.metadata.updated_at = self.updated_at
        self.metadata.message_count = len(self.messages)
        self.metadata.runtime_summary = _runtime_summary_from_transcript_entries(
            self.transcript_entries
        )
        self.metadata.checkpoint_count = len(self.checkpoints)
        self.metadata.instruction_summary = _summarize_instruction_layers(
            self.instruction_layers
        )
        self.metadata.hook_summary = _summarize_hook_status(self.hook_status)
        self.metadata.delegation_summary = _summarize_delegation_status(
            self.delegation_status
        )
        self.metadata.extension_summary = _summarize_extension_manifests(
            self.extension_manifests
        )
        self.metadata.readiness_summary = _summarize_readiness_report(
            self.readiness_report
        )

        # 提取首条用户消息（截断）
        for msg in self.messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = "" if content is None else str(content)
                self.metadata.first_message = content[:100]
                break

        # 提取最后一条消息（截断）— 避免完整反向迭代
        if self.messages:
            for msg in reversed(self.messages):
                if msg.get("role") in ("user", "assistant"):
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = "" if content is None else str(content)
                    self.metadata.last_message = content[:100]
                    break

    @property
    def has_delta(self) -> bool:
        """检查是否存在未保存的变更。"""
        return (
            len(self.messages) != self._last_saved_msg_count
            or len(self.transcript_entries) != self._last_saved_transcript_count
            or len(self.checkpoints) != self._last_saved_checkpoint_count
        )

    def _compute_content_hash(self) -> str:
        """计算消息内容的快速哈希用于变更检测。"""
        h = hashlib.md5(usedforsecurity=False)
        for msg in self.messages[-20:]:  # 为提升速度仅哈希最近 20 条消息
            h.update(msg.get("role", "").encode())
            content = msg.get("content", "")
            if isinstance(content, str):
                h.update(content[:500].encode())
        return h.hexdigest()


def _runtime_trace_token_from_entry(entry: dict[str, Any]) -> str | None:
    """从转录条目中提取运行时追踪令牌。

    参数:
        entry: 转录条目字典，可能包含 runtimeKind、category、runtimeStep 等字段

    返回:
        格式化的运行时追踪令牌字符串；如果无法识别则返回 None
    """
    kind = str(entry.get("runtimeKind") or "").strip().lower()
    category = str(entry.get("category") or "").strip().lower()
    body = str(entry.get("body") or "")

    if category != "runtime" and not kind:
        normalized = " ".join(body.split()).lower()
        if normalized.startswith("runtime phase:"):
            kind = "phase"
        elif normalized.startswith("verification guard:"):
            kind = "guard"
        elif "widened mode is active" in normalized or "widening is now available" in normalized:
            kind = "widening"
        elif normalized.startswith("turn completed") or normalized.startswith("turn complete"):
            kind = "stop"
        else:
            return None

    step = entry.get("runtimeStep")
    step_suffix = f"@{step}" if isinstance(step, int) else ""
    phase = str(entry.get("runtimePhase") or "").strip()
    stop_reason = str(entry.get("runtimeStopReason") or "").strip()
    verify = str(entry.get("runtimeVerificationFocus") or "").strip()

    if kind == "phase":
        return f"phase:{phase or 'unknown'}{step_suffix}"
    if kind == "guard":
        return f"guard:{verify or stop_reason or 'verification'}{step_suffix}"
    if kind == "widening":
        return f"widen:{stop_reason or 'escalation'}{step_suffix}"
    if kind == "stop":
        return f"stop:{stop_reason or 'done'}{step_suffix}"
    if kind == "compaction":
        return f"compact:{phase or 'context'}{step_suffix}"
    if kind == "recovery":
        return f"recover:{stop_reason or 'resume'}{step_suffix}"

    return f"{kind or 'runtime'}{step_suffix}"


def _runtime_summary_from_transcript_entries(entries: list[dict[str, Any]]) -> str:
    """从转录条目构建运行时摘要字符串。

    参数:
        entries: 转录条目列表

    返回:
        用箭头连接的运行时追踪令牌字符串
    """
    tokens: list[str] = []
    for entry in entries:
        token = _runtime_trace_token_from_entry(entry)
        if token and (not tokens or tokens[-1] != token):
            tokens.append(token)
    return " -> ".join(tokens)


def _safe_text(value: Any) -> str:
    """安全地将任意值转换为去除首尾空格的字符串。

    参数:
        value: 任意输入值

    返回:
        去除首尾空格的字符串；None 返回空字符串
    """
    if value is None:
        return ""
    return str(value).strip()


def _named_list(items: list[Any], *, key: str = "name") -> list[str]:
    """从条目列表（字典或字符串）中提取名称列表。

    参数:
        items: 字典或字符串的列表
        key: 字典中用作名称的键，默认为 "name"

    返回:
        提取到的非空名称字符串列表
    """
    names: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            candidate = _safe_text(item.get(key) or item.get("label") or item.get("id"))
            if candidate:
                names.append(candidate)
        else:
            candidate = _safe_text(item)
            if candidate:
                names.append(candidate)
    return names


def _summarize_instruction_layers(layers: list[dict[str, Any]]) -> str:
    """将指令层列表摘要为字符串。

    参数:
        layers: 指令层字典列表

    返回:
        摘要字符串，格式如 "3 layer(s): xxx, yyy, zzz..."；无内容时返回空字符串
    """
    names = _named_list(layers)
    if not names:
        return ""
    return f"{len(names)} layer(s): {', '.join(names[:3])}" + ("..." if len(names) > 3 else "")


def _summarize_hook_status(status: dict[str, Any]) -> str:
    """将钩子状态摘要为字符串。

    参数:
        status: 钩子状态字典

    返回:
        摘要字符串；无内容时返回空字符串
    """
    if not isinstance(status, dict):
        return ""
    summary = _safe_text(status.get("summary"))
    if summary:
        return summary
    total = int(status.get("total_hooks", 0) or 0)
    enabled = int(status.get("enabled_hooks", 0) or 0)
    return f"{enabled}/{total} hook(s) enabled" if total else ""


def _summarize_delegation_status(status: dict[str, Any]) -> str:
    """将委派状态摘要为字符串。

    参数:
        status: 委派状态字典

    返回:
        摘要字符串；无内容时返回空字符串
    """
    if not isinstance(status, dict):
        return ""
    summary = _safe_text(status.get("summary"))
    if summary:
        return summary
    running = int(status.get("running_tasks", 0) or 0)
    available = int(status.get("available_slots", 0) or 0)
    return f"{running} running, {available} slot(s) open"


def _summarize_extension_manifests(manifests: list[dict[str, Any]]) -> str:
    """将扩展清单列表摘要为字符串。

    参数:
        manifests: 扩展清单字典列表

    返回:
        摘要字符串；无内容时返回空字符串
    """
    names = _named_list(manifests)
    if not names:
        return ""
    return f"{len(names)} extension(s): {', '.join(names[:3])}" + ("..." if len(names) > 3 else "")


def _summarize_readiness_report(report: dict[str, Any]) -> str:
    """将就绪报告摘要为字符串。

    参数:
        report: 就绪报告字典

    返回:
        摘要字符串；无内容时返回空字符串
    """
    if not isinstance(report, dict):
        return ""
    summary = _safe_text(report.get("summary"))
    if summary:
        return summary
    status = _safe_text(report.get("status"))
    provider = _safe_text(report.get("provider"))
    if status and provider:
        return f"{status} via {provider}"
    return status or provider


def _format_named_collection(items: list[Any], *, fallback: str = "(none)") -> str:
    """将命名集合格式化为逗号分隔的字符串。

    参数:
        items: 条目列表
        fallback: 列表为空时的回退字符串

    返回:
        逗号分隔的名称字符串，或回退字符串
    """
    names = _named_list(items)
    return ", ".join(names) if names else fallback


def _serialize_checkpoint(checkpoint: FileCheckpoint) -> dict[str, Any]:
    """将 FileCheckpoint 序列化为字典。

    参数:
        checkpoint: 文件检查点对象

    返回:
        可 JSON 序列化的字典
    """
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "created_at": checkpoint.created_at,
        "file_path": checkpoint.file_path,
        "existed": checkpoint.existed,
        "previous_content": checkpoint.previous_content,
        "kind": checkpoint.kind,
        "group_id": checkpoint.group_id,
    }


def _deserialize_checkpoint(data: dict[str, Any]) -> FileCheckpoint:
    """从字典反序列化 FileCheckpoint。

    参数:
        data: 包含检查点数据的字典

    返回:
        还原后的 FileCheckpoint 对象
    """
    return FileCheckpoint(
        checkpoint_id=str(data["checkpoint_id"]),
        created_at=float(data["created_at"]),
        file_path=str(data["file_path"]),
        existed=bool(data["existed"]),
        previous_content=str(data.get("previous_content", "")),
        kind=str(data.get("kind", "edit") or "edit"),
        group_id=str(data.get("group_id", "")),
    )


# ---------------------------------------------------------------------------
# Session file operations
# ---------------------------------------------------------------------------

def _session_file(session_id: str) -> Path:
    """返回会话 JSON 文件的路径。

    参数:
        session_id: 会话 ID

    返回:
        会话文件对应的 Path 对象
    """
    return SESSIONS_DIR / f"{session_id}.json"


def _session_delta_dir(session_id: str) -> Path:
    """返回会话差量目录的路径。

    参数:
        session_id: 会话 ID

    返回:
        差量目录对应的 Path 对象
    """
    return SESSIONS_DIR / DELTA_DIR_NAME / session_id


def _session_index_file() -> Path:
    """返回会话索引文件的路径。

    返回:
        索引文件对应的 Path 对象
    """
    return MINI_CODE_DIR / "sessions_index.json"


def _load_session_index() -> dict[str, SessionMetadata]:
    """加载会话索引（所有会话的轻量级元数据）。

    返回:
        以会话 ID 为键、SessionMetadata 为值的字典；索引不存在时返回空字典
    """
    index_path = _session_index_file()
    if not index_path.exists():
        return {}
    try:
        raw = index_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return {
            sid: SessionMetadata(**meta)
            for sid, meta in data.items()
        }
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def _save_session_index(index: dict[str, SessionMetadata]) -> None:
    """保存会话索引。

    参数:
        index: 以会话 ID 为键、SessionMetadata 为值的字典
    """
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {
        sid: {
            "session_id": meta.session_id,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "first_message": meta.first_message,
            "last_message": meta.last_message,
            "message_count": meta.message_count,
            "workspace": meta.workspace,
            "runtime_summary": meta.runtime_summary,
            "checkpoint_count": meta.checkpoint_count,
            "instruction_summary": meta.instruction_summary,
            "hook_summary": meta.hook_summary,
            "delegation_summary": meta.delegation_summary,
            "extension_summary": meta.extension_summary,
            "readiness_summary": meta.readiness_summary,
        }
        for sid, meta in index.items()
    }
    _session_index_file().write_text(
        json.dumps(serializable, indent=2) + "\n",
        encoding="utf-8",
    )


def _save_delta(session: SessionData) -> None:
    """仅保存自上次全量保存以来的增量变更。

    差量文件包含自上次保存点以来新增的消息和转录条目。
    这比每次自动保存时序列化整个会话要廉价得多。

    调用位置: 被 save_session() 内部调用（当 should_full_save=False 时）。

    参数:
        session: 待保存的会话数据
    """
    delta_dir = _session_delta_dir(session.session_id)
    delta_dir.mkdir(parents=True, exist_ok=True)

    # 收集自上次保存以来的新消息
    new_messages = session.messages[session._last_saved_msg_count:]
    new_transcripts = session.transcript_entries[session._last_saved_transcript_count:]
    new_checkpoints = session.checkpoints[session._last_saved_checkpoint_count:]

    if not new_messages and not new_transcripts and not new_checkpoints:
        return

    # 创建差量条目
    delta_data: dict[str, Any] = {
        "ts": time.time(),
        "msg_offset": session._last_saved_msg_count,
        "transcript_offset": session._last_saved_transcript_count,
    }
    if new_messages:
        delta_data["messages"] = new_messages
    if new_transcripts:
        delta_data["transcripts"] = new_transcripts
    if new_checkpoints:
        delta_data["checkpoint_offset"] = session._last_saved_checkpoint_count
        delta_data["checkpoints"] = [_serialize_checkpoint(cp) for cp in new_checkpoints]

    # 使用顺序编号写入差量文件
    delta_num = session._delta_save_count
    delta_path = delta_dir / f"delta_{delta_num:04d}.json"
    delta_path.write_text(
        json.dumps(delta_data, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 更新追踪
    session._last_saved_msg_count = len(session.messages)
    session._last_saved_transcript_count = len(session.transcript_entries)
    session._last_saved_checkpoint_count = len(session.checkpoints)
    session._delta_save_count += 1


def _consolidate_deltas(session: SessionData) -> None:
    """合并所有差量文件到完整会话文件并清理。

    定期调用以防止差量文件无限制增长，
    并确保完整会话文件保持一致。

    参数:
        session: 待合并的会话数据
    """
    delta_dir = _session_delta_dir(session.session_id)
    if not delta_dir.exists():
        return

    # 差量在 load_session 期间已经应用，因此只需清理
    for delta_file in sorted(delta_dir.glob("delta_*.json")):
        try:
            delta_file.unlink()
        except OSError:
            pass

    # 尝试删除已空的差量目录
    try:
        delta_dir.rmdir()
        # 同时尝试删除父目录（如果已空）
        parent = delta_dir.parent
        if parent.name == DELTA_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass

    session._delta_save_count = 0


def save_session(session: SessionData, force_full: bool = False) -> None:
    """将会话持久化到磁盘，支持增量差量保存。 【为什么需要】
    避免每次微小变更都全量序列化整个会话（可能包含数百条消息）。
    通过增量差量（delta）策略，将常规自动保存的 I/O 开销降低
    到仅写入新增消息/转录，而全量合并仅在关键时机执行。

    调用位置: 被 create_file_checkpoint()、rewind_session_data()、
              AutosaveManager.save_if_needed()、AutosaveManager.force_save() 内部调用；
              被 tui/session_flow.py 和 paper_a_task_completion_eval.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 更新元数据                                      ║
    ║    └─ 调用 session.update_metadata() 刷新时间戳、     ║
    ║       消息计数、运行时摘要等                           ║
    ║                                                       ║
    ║  第2步 创建会话目录（如不存在）                        ║
    ║    └─ SESSIONS_DIR.mkdir(parents=True, exist_ok=True) ║
    ║                                                       ║
    ║  第3步 全量 vs 增量决策                                ║
    ║    ├─ 满足任一条件 → 执行全量保存（第4步）：           ║
    ║    │   A) force_full=True（显式保存命令）              ║
    ║    │   B) _delta_save_count == 0（首次保存）           ║
    ║    │   C) _delta_save_count >= FULL_SAVE_INTERVAL      ║
    ║    │      （每 N 次增量后定期合并）                     ║
    ║    │   D) _delta_save_count >= MAX_DELTA_FILES         ║
    ║    │      （安全上限，防止差量文件过多）               ║
    ║    └─ 均不满足 → 执行增量差量保存（第5步）            ║
    ║                                                       ║
    ║  第4步 全量保存（force_full / 首次 / 定期合并）        ║
    ║    ├─ 序列化整个 SessionData 为 JSON 字典             ║
    ║    │  （消息、转录、历史、权限、技能、MCP、指令、     ║
    ║    │   钩子、委派、扩展、就绪报告、检查点、元数据）   ║
    ║    ├─ 写入 <session_id>.json 文件                      ║
    ║    ├─ 更新追踪计数器（_last_saved_* 对齐当前长度）    ║
    ║    ├─ 计算并保存内容哈希（_last_full_save_hash）       ║
    ║    └─ 调用 _consolidate_deltas() 清理差量文件         ║
    ║                                                       ║
    ║  第5步 增量差量保存                                   ║
    ║    └─ 委托给 _save_delta(session)：                   ║
    ║       ├─ 计算新消息：messages[_last_saved_msg_count:]  ║
    ║       ├─ 计算新转录：transcripts[_last_saved_*:]      ║
    ║       ├─ 计算新检查点：checkpoints[_last_saved_*:]    ║
    ║       ├─ 如无变更则直接返回                           ║
    ║       ├─ 构建 delta_data 字典（含偏移量信息）          ║
    ║       ├─ 写入 deltas/<session_id>/delta_NNNN.json      ║
    ║       └─ 更新追踪计数器 + _delta_save_count++         ║
    ║                                                       ║
    ║  第6步 更新全局会话索引                                ║
    ║    ├─ 加载 sessions_index.json                        ║
    ║    ├─ 将当前 metadata 写入索引                        ║
    ║    └─ 保存索引文件                                     ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session: 待保存的会话数据
        force_full: 强制全量保存（例如在执行显式保存命令时）
    """
    log_session_event("save", details=f"id={session.session_id} force_full={force_full}")
    session.update_metadata()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # 决定执行全量保存还是差量保存
    should_full_save = (
        force_full
        or session._delta_save_count == 0  # 首次保存总是全量
        or session._delta_save_count >= FULL_SAVE_INTERVAL
        or session._delta_save_count >= MAX_DELTA_FILES  # 安全上限
    )

    if should_full_save:
        # 全量保存：序列化所有内容
        session_path = _session_file(session.session_id)
        serializable = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "workspace": session.workspace,
            "messages": session.messages,
            "transcript_entries": session.transcript_entries,
            "history": session.history,
            "permissions_summary": session.permissions_summary,
            "skills": session.skills,
            "mcp_servers": session.mcp_servers,
            "instruction_layers": session.instruction_layers,
            "hook_status": session.hook_status,
            "delegated_tasks": session.delegated_tasks,
            "delegation_status": session.delegation_status,
            "extension_manifests": session.extension_manifests,
            "readiness_report": session.readiness_report,
            "checkpoints": [_serialize_checkpoint(cp) for cp in session.checkpoints],
            "metadata": {
                "session_id": session.metadata.session_id,
                "created_at": session.metadata.created_at,
                "updated_at": session.metadata.updated_at,
                "first_message": session.metadata.first_message,
                "last_message": session.metadata.last_message,
                "message_count": session.metadata.message_count,
                "workspace": session.metadata.workspace,
                "runtime_summary": session.metadata.runtime_summary,
                "checkpoint_count": session.metadata.checkpoint_count,
                "instruction_summary": session.metadata.instruction_summary,
                "hook_summary": session.metadata.hook_summary,
                "delegation_summary": session.metadata.delegation_summary,
                "extension_summary": session.metadata.extension_summary,
                "readiness_summary": session.metadata.readiness_summary,
            },
        }
        session_path.write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # 重置差量追踪
        session._last_saved_msg_count = len(session.messages)
        session._last_saved_transcript_count = len(session.transcript_entries)
        session._last_saved_checkpoint_count = len(session.checkpoints)
        session._last_full_save_hash = session._compute_content_hash()

        # 合并并清理差量文件
        _consolidate_deltas(session)
    else:
        # 差量保存：仅追加新数据
        _save_delta(session)

    # 更新索引（始终保持轻量）
    index = _load_session_index()
    index[session.session_id] = session.metadata
    _save_session_index(index)


def load_session(session_id: str) -> SessionData | None:
    """从磁盘加载会话，并应用所有待处理的差量。 【为什么需要】
    重启或恢复时需将会话文件和可能存在的多个差量 delta 文件
    合并为完整会话状态。差量文件记录了上次全量保存后追加的
    新消息/转录，需按顺序正确拼接并处理可能的偏移重叠。

    调用位置: 被 get_latest_session() 和 rewind_session() 内部调用；
              被 main.py（--session、--resume）、cli_commands.py、
              tui/session_flow.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 检查基础会话文件是否存在                        ║
    ║    ├─ _session_file(session_id) → <id>.json           ║
    ║    └─ 不存在 → 返回 None                              ║
    ║                                                       ║
    ║  第2步 读取并解析会话 JSON                            ║
    ║    ├─ 读取 <id>.json 全部文本                          ║
    ║    ├─ json.loads 反序列化                              ║
    ║    └─ 如 JSON 损坏 → 返回 None                        ║
    ║                                                       ║
    ║  第3步 重建 SessionData 对象                           ║
    ║    ├─ 从 metadata 字段创建 SessionMetadata             ║
    ║    ├─ 还原 messages / transcript_entries / history     ║
    ║    ├─ 还原 permissions / skills / mcp_servers          ║
    ║    ├─ 还原 instruction_layers / hook_status            ║
    ║    ├─ 还原 delegated_tasks / delegation_status         ║
    ║    ├─ 还原 extension_manifests / readiness_report      ║
    ║    ├─ 还原 checkpoints（反序列化每个条目）              ║
    ║    └─ 附加 metadata                                    ║
    ║                                                       ║
    ║  第4步 扫描差量文件目录                                ║
    ║    ├─ 检查 deltas/<session_id>/ 是否存在               ║
    ║    └─ 不存在 → 直接跳至第6步                          ║
    ║                                                       ║
    ║  第5步 按顺序应用所有差量文件                          ║
    ║    ├─ glob("delta_*.json") → 排序                      ║
    ║    └─ 对每个差量文件（损坏则跳过）：                   ║
    ║       ├─ 读取并解析 JSON                               ║
    ║       ├─ 合并 messages：                               ║
    ║       │  利用 msg_offset 判断偏移，处理三种场景：      ║
    ║       │  A) 偏移 >= 当前长度 → 直接 extend            ║
    ║       │  B) 部分重叠 → 只追加新增部分                 ║
    ║       │  C) 偏移 < 当前且无新增 → 跳过               ║
    ║       ├─ 合并 transcripts：同理处理偏移重叠           ║
    ║       ├─ 合并 checkpoints：同理处理偏移重叠            ║
    ║       └─ _delta_save_count += 1                        ║
    ║                                                       ║
    ║  第6步 更新追踪计数器                                  ║
    ║    ├─ _last_saved_msg_count = len(messages)            ║
    ║    ├─ _last_saved_transcript_count = len(transcripts)  ║
    ║    ├─ _last_saved_checkpoint_count = len(checkpoints)  ║
    ║    └─ _last_full_save_hash = _compute_content_hash()   ║
    ║                                                       ║
    ║  第7步 返回重建后的 SessionData                        ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session_id: 要加载的会话 ID

    返回:
        加载后的 SessionData 对象；加载失败时返回 None
    """
    log_session_event("load", details=f"id={session_id}")
    session_path = _session_file(session_id)
    if not session_path.exists():
        return None

    try:
        raw = session_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        metadata = SessionMetadata(**data.get("metadata", {}))
        session = SessionData(
            session_id=data["session_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            workspace=data["workspace"],
            messages=data.get("messages", []),
            transcript_entries=data.get("transcript_entries", []),
            history=data.get("history", []),
            permissions_summary=data.get("permissions_summary", {}),
            skills=data.get("skills", []),
            mcp_servers=data.get("mcp_servers", []),
            instruction_layers=data.get("instruction_layers", []),
            hook_status=data.get("hook_status", {}),
            delegated_tasks=data.get("delegated_tasks", []),
            delegation_status=data.get("delegation_status", {}),
            extension_manifests=data.get("extension_manifests", []),
            readiness_report=data.get("readiness_report", {}),
            checkpoints=[
                _deserialize_checkpoint(item)
                for item in data.get("checkpoints", [])
                if isinstance(item, dict)
            ],
            metadata=metadata,
        )

        # 应用所有待处理的差量
        delta_dir = _session_delta_dir(session_id)
        if delta_dir.exists():
            delta_files = sorted(delta_dir.glob("delta_*.json"))
            for delta_path in delta_files:
                try:
                    delta_raw = delta_path.read_text(encoding="utf-8")
                    delta = json.loads(delta_raw)

                    # 在正确偏移处追加差量消息
                    if "messages" in delta:
                        offset = delta.get("msg_offset", len(session.messages))
                        # 确保不会重复消息
                        if offset >= len(session.messages):
                            session.messages.extend(delta["messages"])
                        elif offset + len(delta["messages"]) > len(session.messages):
                            # 部分重叠 — 仅追加新增部分
                            overlap = len(session.messages) - offset
                            session.messages.extend(delta["messages"][overlap:])

                    # 追加差量转录
                    if "transcripts" in delta:
                        t_offset = delta.get("transcript_offset", len(session.transcript_entries))
                        if t_offset >= len(session.transcript_entries):
                            session.transcript_entries.extend(delta["transcripts"])
                        elif t_offset + len(delta["transcripts"]) > len(session.transcript_entries):
                            overlap = len(session.transcript_entries) - t_offset
                            session.transcript_entries.extend(delta["transcripts"][overlap:])

                    if "checkpoints" in delta:
                        c_offset = delta.get("checkpoint_offset", len(session.checkpoints))
                        parsed = [
                            _deserialize_checkpoint(item)
                            for item in delta["checkpoints"]
                            if isinstance(item, dict)
                        ]
                        if c_offset >= len(session.checkpoints):
                            session.checkpoints.extend(parsed)
                        elif c_offset + len(parsed) > len(session.checkpoints):
                            overlap = len(session.checkpoints) - c_offset
                            session.checkpoints.extend(parsed[overlap:])

                    session._delta_save_count += 1
                except (json.JSONDecodeError, KeyError, TypeError):
                    # 跳过损坏的差量文件
                    continue

        # 更新追踪计数器
        session._last_saved_msg_count = len(session.messages)
        session._last_saved_transcript_count = len(session.transcript_entries)
        session._last_saved_checkpoint_count = len(session.checkpoints)
        session._last_full_save_hash = session._compute_content_hash()

        return session
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def list_sessions() -> list[SessionMetadata]:
    """列出所有可用的会话，按更新时间倒序排列。 【为什么需要】--list-sessions 命令和 get_latest_session 的底层依赖。
    从轻量级索引文件读取，避免加载每个会话的完整 JSON。

    调用位置: 被 get_latest_session() 和 cleanup_old_sessions() 内部调用；
              被 cli_commands.py、tui/session_flow.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 加载会话索引文件                                 ║
    ║    └─ _load_session_index()                            ║
    ║       ├─ 读取 sessions_index.json                     ║
    ║       ├─ 解析 JSON → {id: SessionMetadata, ...}       ║
    ║       └─ 文件不存在或损坏 → 返回 {}                    ║
    ║                                                       ║
    ║  第2步 提取元数据列表                                   ║
    ║    └─ list(index.values())                             ║
    ║                                                       ║
    ║  第3步 按更新时间降序排列                                ║
    ║    └─ sort(key=lambda s: s.updated_at, reverse=True)  ║
    ║                                                       ║
    ║  第4步 返回排序后的列表                                 ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    返回:
        按更新时间降序排列的 SessionMetadata 列表
    """
    index = _load_session_index()
    sessions = list(index.values())
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """从磁盘删除会话。

    参数:
        session_id: 要删除的会话 ID

    返回:
        删除成功返回 True，否则返回 False
    """
    session_path = _session_file(session_id)
    if not session_path.exists():
        return False

    try:
        session_path.unlink()
        # 清理孤立的差量文件
        delta_dir = _session_delta_dir(session_id)
        if delta_dir.exists():
            import shutil
            shutil.rmtree(delta_dir, ignore_errors=True)
        index = _load_session_index()
        index.pop(session_id, None)
        _save_session_index(index)
        return True
    except OSError:
        return False


def cleanup_old_sessions(max_sessions: int = 50) -> int:
    """删除超出 max_sessions 限制的最旧会话。

    参数:
        max_sessions: 保留的最大会话数量，默认为 50

    返回:
        实际删除的会话数量
    """
    sessions = list_sessions()
    if len(sessions) <= max_sessions:
        return 0

    to_delete = sessions[max_sessions:]
    deleted = 0
    for meta in to_delete:
        if delete_session(meta.session_id):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Session creation helpers
# ---------------------------------------------------------------------------

def create_new_session(workspace: str) -> SessionData:
    """创建一个新的空会话。 【为什么需要】每次启动 agent 或用户显式创建新会话时调用。
    生成唯一 ID、记录时间戳，返回一个干净的会话对象。

    调用位置: 被 tui/session_flow.py（会话初始化流程）和
              paper_a_task_completion_eval.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 记录当前时间戳                                   ║
    ║    └─ now = time.time()                               ║
    ║                                                       ║
    ║  第2步 生成 12 位随机会话 ID                            ║
    ║    └─ uuid.uuid4().hex[:12] → 如 "a1b2c3d4e5f6"       ║
    ║                                                       ║
    ║  第3步 记录日志（create 事件）                          ║
    ║    └─ log_session_event("create", ...)                 ║
    ║                                                       ║
    ║  第4步 构造 SessionData 对象                            ║
    ║    ├─ session_id: 生成的随机 ID                       ║
    ║    ├─ created_at / updated_at: 当前时间戳              ║
    ║    ├─ workspace: 传入的工作目录                        ║
    ║    └─ 其余字段（messages/transcripts 等）保持默认空值  ║
    ║                                                       ║
    ║  第5步 返回新会话                                      ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        workspace: 会话启动时的工作目录

    返回:
        新创建的 SessionData 对象
    """
    now = time.time()
    session_id = uuid.uuid4().hex[:12]
    log_session_event("create", details=f"id={session_id} workspace={workspace}")
    return SessionData(
        session_id=session_id,
        created_at=now,
        updated_at=now,
        workspace=workspace,
    )


def get_latest_session(workspace: str | None = None) -> SessionData | None:
    """获取最近的会话，可选择按工作目录过滤。 【为什么需要】--resume latest 和自动恢复场景需要快速找到最近的会话。
    支持按 workspace 过滤，避免跨项目恢复错误的会话。

    调用位置: 被 main.py（--resume latest 参数解析后）、
              cli_commands.py、tui/session_flow.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 获取所有会话列表（按更新时间倒序）                ║
    ║    └─ list_sessions()                                  ║
    ║       └─ 从 sessions_index.json 读取所有会话元数据      ║
    ║       └─ 按 updated_at 降序排列                        ║
    ║                                                       ║
    ║  第2步 遍历查找符合条件的第一个会话                      ║
    ║    ├─ workspace 为 None → 直接返回第一个（最新）        ║
    ║    ├─ workspace 有值 → 找到第一个匹配 workspace 的      ║
    ║    └─ 没有匹配 → 返回 None                             ║
    ║                                                       ║
    ║  第3步 调用 load_session 加载完整数据                    ║
    ║    └─ 返回 SessionData（或 None）                      ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        workspace: 可选的工作目录过滤条件

    返回:
        最近的 SessionData 对象；未找到时返回 None
    """
    sessions = list_sessions()
    for meta in sessions:
        if workspace is None or meta.workspace == workspace:
            return load_session(meta.session_id)
    return None


def create_file_checkpoint(
    session: SessionData | None,
    *,
    file_path: str,
    existed: bool,
    previous_content: str,
) -> FileCheckpoint | None:
    """在文件变更前录制一个持久的回退快照。

    【为什么需要】
    在 AI 修改磁盘文件前将原内容保存到检查点列表，以便
    用户使用回退功能撤销变更。每个检查点包含唯一标识符、
    文件路径、前置内容快照，以及可选的 group_id 用于分组。

    调用位置: 被 file_review.py 和 paper_a_task_completion_eval.py 外部调用。
              具体来说，在 write_file/edit_file 等工具执行文件修改前调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 空值守卫                                       ║
    ║    └─ session 为 None → 返回 None（允许无会话场景）   ║
    ║                                                       ║
    ║  第2步 生成 checkpoint_id                              ║
    ║    └─ uuid.uuid4().hex[:12] → 12 字符随机十六进制串   ║
    ║                                                       ║
    ║  第3步 记录时间戳                                      ║
    ║    └─ time.time() 作为 created_at                     ║
    ║                                                       ║
    ║  第4步 构建 FileCheckpoint 对象                        ║
    ║    ├─ checkpoint_id: 生成的唯一 ID                    ║
    ║    ├─ created_at: 当前 Unix 时间戳                    ║
    ║    ├─ file_path: 被修改的文件路径                     ║
    ║    ├─ existed: 文件之前是否存在（True/False）          ║
    ║    ├─ previous_content: 内容快照（完整前置内容）      ║
    ║    ├─ kind: 默认为 "edit"                             ║
    ║    └─ group_id: 空字符串（可由外部调用者后续设置，    ║
    ║       用于将相关操作归为同一回退组）                   ║
    ║                                                       ║
    ║  第5步 追加到会话检查点列表                            ║
    ║    └─ session.checkpoints.append(checkpoint)           ║
    ║                                                       ║
    ║  第6步 触发持久化                                      ║
    ║    └─ save_session(session, force_full=False)          ║
    ║       （以差量方式写入，避免全量序列化开销）           ║
    ║                                                       ║
    ║  第7步 返回创建的 FileCheckpoint                       ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session: 会话对象（可为 None）
        file_path: 被修改的文件路径
        existed: 文件之前是否存在
        previous_content: 文件的先前内容

    返回:
        创建的 FileCheckpoint；如果 session 为 None 则返回 None
    """
    if session is None:
        return None

    checkpoint = FileCheckpoint(
        checkpoint_id=uuid.uuid4().hex[:12],
        created_at=time.time(),
        file_path=file_path,
        existed=existed,
        previous_content=previous_content,
    )
    session.checkpoints.append(checkpoint)
    save_session(session, force_full=False)
    return checkpoint


def _select_checkpoints_to_rewind(
    session: SessionData,
    *,
    steps: int = 1,
    checkpoint_id: str | None = None,
) -> list[FileCheckpoint]:
    """选择要回退的检查点。

    参数:
        session: 会话数据
        steps: 回退的步数，默认为 1
        checkpoint_id: 可选的目标检查点 ID

    返回:
        选中的 FileCheckpoint 列表
    """
    if not session.checkpoints:
        return []
    if checkpoint_id:
        for index in range(len(session.checkpoints) - 1, -1, -1):
            checkpoint = session.checkpoints[index]
            if checkpoint.checkpoint_id == checkpoint_id:
                group_id = checkpoint.group_id
                if group_id:
                    while index > 0 and session.checkpoints[index - 1].group_id == group_id:
                        index -= 1
                return session.checkpoints[index:]
        return []
    if steps <= 0:
        return []
    start_index = max(len(session.checkpoints) - steps, 0)
    tail_group_id = session.checkpoints[-1].group_id
    if tail_group_id:
        group_start = len(session.checkpoints) - 1
        while group_start > 0 and session.checkpoints[group_start - 1].group_id == tail_group_id:
            group_start -= 1
        start_index = min(start_index, group_start)
    return session.checkpoints[start_index:]


def rewind_session_data(
    session: SessionData,
    *,
    steps: int = 1,
    checkpoint_id: str | None = None,
) -> list[FileCheckpoint]:
    """针对内存中的会话恢复检查点，并持久化结果。 【为什么需要】
    撤销文件修改操作，将磁盘文件恢复到检查点记录的状态。
    支持两种回退路径：按步数（steps）回退 N 个检查点，
    或按 checkpoint_id 回退到指定检查点。回退前先对当前
    文件内容做反向快照，形成可再次撤销的"回退安全网"。

    调用位置: 被 rewind_session() 包装后调用，同时被 cli_commands.py
              （_handle_rewind 内部逻辑）直接调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 选择要回退的检查点                              ║
    ║    └─ 委托 _select_checkpoints_to_rewind()            ║
    ║       ├─ 路径 A：按 steps 回退                        ║
    ║       │  ├─ start_index = max(len - steps, 0)         ║
    ║       │  └─ 如最后一个检查点有 group_id，则向前       ║
    ║       │     扩展到同组首个（保证原子回退）            ║
    ║       ├─ 路径 B：按 checkpoint_id 回退                ║
    ║       │  ├─ 从后向前找到匹配的检查点                  ║
    ║       │  └─ 如有 group_id，向前扩展到同组首个        ║
    ║       └─ 未选中任何检查点 → 返回空列表               ║
    ║                                                       ║
    ║  第2步 已选中 → 生成回退操作元数据                    ║
    ║    ├─ rewind_group_id = uuid 新 ID（标记本轮回退）    ║
    ║    └─ rewind_created_at = time.time()                 ║
    ║                                                       ║
    ║  第3步 创建反向安全快照（逆序遍历选中检查点）          ║
    ║    ├─ 跳过已处理过的 file_path（去重）                ║
    ║    ├─ 对每个唯一文件：                                ║
    ║    │  ├─ 检查文件当前是否存在                         ║
    ║    │  ├─ 存在则读取当前内容作为 previous_content      ║
    ║    │  └─ 生成反向 FileCheckpoint（kind="rewind"，    ║
    ║    │     group_id=rewind_group_id）                    ║
    ║    └─ 加入 reverse_checkpoints 列表                   ║
    ║                                                       ║
    ║  第4步 恢复磁盘文件（逆序遍历选中检查点）              ║
    ║    ├─ 确保目标目录存在（mkdir parents）               ║
    ║    ├─ 原文件存在 → 写入 previous_content 恢复内容     ║
    ║    └─ 原文件不存在且当前存在 → 删除当前文件          ║
    ║                                                       ║
    ║  第5步 替换会话中的检查点列表                          ║
    ║    ├─ 删除被恢复的选中检查点（从末尾删除）            ║
    ║    └─ 追加反向安全快照到列表末尾                      ║
    ║                                                       ║
    ║  第6步 全量持久化                                     ║
    ║    └─ save_session(session, force_full=True)          ║
    ║       （回退操作后强制全量保存以保证一致性）          ║
    ║                                                       ║
    ║  第7步 返回已恢复的原始检查点列表                     ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session: 会话数据
        steps: 回退的步数，默认为 1
        checkpoint_id: 可选的目标检查点 ID

    返回:
        已恢复的 FileCheckpoint 列表
    """
    selected = _select_checkpoints_to_rewind(
        session,
        steps=steps,
        checkpoint_id=checkpoint_id,
    )
    if not selected:
        return []

    rewind_group_id = uuid.uuid4().hex[:12]
    rewind_created_at = time.time()
    reverse_checkpoints: list[FileCheckpoint] = []
    captured_paths: set[str] = set()
    for checkpoint in reversed(selected):
        if checkpoint.file_path in captured_paths:
            continue
        target = Path(checkpoint.file_path)
        existed = target.exists()
        previous_content = target.read_text(encoding="utf-8") if existed else ""
        reverse_checkpoints.append(
            FileCheckpoint(
                checkpoint_id=uuid.uuid4().hex[:12],
                created_at=rewind_created_at,
                file_path=checkpoint.file_path,
                existed=existed,
                previous_content=previous_content,
                kind="rewind",
                group_id=rewind_group_id,
            )
        )
        captured_paths.add(checkpoint.file_path)

    for checkpoint in reversed(selected):
        target = Path(checkpoint.file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint.existed:
            target.write_text(checkpoint.previous_content, encoding="utf-8")
        elif target.exists():
            target.unlink()

    del session.checkpoints[-len(selected):]
    session.checkpoints.extend(reverse_checkpoints)
    save_session(session, force_full=True)
    return selected


def rewind_session(
    session_id: str,
    *,
    steps: int = 1,
    checkpoint_id: str | None = None,
) -> tuple[SessionData | None, list[FileCheckpoint]]:
    """恢复已保存会话的最新检查点文件编辑。 【为什么需要】
    作为 rewind_session_data 的高层入口，先加载已持久化
    的会话，再执行回退操作。它将加载和回退两个阶段串联
    起来，对外提供统一的会话回退接口。

    调用位置: 被 main.py（--rewind 参数处理）和 cli_commands.py 外部调用。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 加载已持久化的会话                              ║
    ║    └─ 调用 load_session(session_id)                   ║
    ║       ├─ 从 <id>.json 读取基础会话                    ║
    ║       ├─ 合并 deltas 目录中的差量文件                 ║
    ║       └─ 加载失败（None）→ 返回 (None, [])           ║
    ║                                                       ║
    ║  第2步 委托给 rewind_session_data                     ║
    ║    ├─ 传入 load 得到的 session 对象                   ║
    ║    ├─ 传入 steps / checkpoint_id 参数                 ║
    ║    └─ 返回选中的已恢复检查点列表                      ║
    ║                                                       ║
    ║  第3步 返回 (session, selected) 元组                  ║
    ║    ├─ session: 回退后的会话对象（含反向快照）         ║
    ║    └─ selected: 被恢复的原始检查点列表                ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session_id: 要回退的会话 ID
        steps: 回退的步数，默认为 1
        checkpoint_id: 可选的目标检查点 ID

    返回:
        (会话数据或 None, 已恢复的检查点列表) 的元组
    """
    session = load_session(session_id)
    if session is None:
        return session, []

    selected = rewind_session_data(
        session,
        steps=steps,
        checkpoint_id=checkpoint_id,
    )
    return session, selected


def format_rewind_preview(
    session: SessionData,
    *,
    steps: int = 1,
    checkpoint_id: str | None = None,
) -> str:
    """格式化为干运行视图，显示回退将恢复哪些检查点。 【为什么需要】--preview-rewind 命令的底层实现。
    在实际执行回退前展示将恢复的文件列表，让用户确认后再操作。

    ╔══ 完整执行流程 ═══════════════════════════════════════╗
    ║                                                       ║
    ║  第1步 获取选中的检查点列表                              ║
    ║    └─ _select_checkpoints_to_rewind(session, steps, id)║
    ║       ├─ 无检查点 → "No checkpoints available"          ║
    ║       └─ 有检查点 → 进入下一步                          ║
    ║                                                       ║
    ║  第2步 统计唯一文件路径（逆序去重）                      ║
    ║    └─ 遍历 selected，用 set 去重                        ║
    ║       ├─ unique_files: 受影响的文件列表                 ║
    ║       └─ count: 检查点总数                             ║
    ║                                                       ║
    ║  第3步 构建预览文本                                     ║
    ║    ├─ 标题行：Rewind preview for session xxx            ║
    ║    ├─ 统计行：N checkpoint(s) across M file(s)         ║
    ║    ├─ 模式行：如果是 rewind 检查点标 "safety"           ║
    ║    └─ 逐条展示每个检查点（ID/时间/路径/类型）           ║
    ║                                                       ║
    ║  第4步 返回格式化文本                                   ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝

    参数:
        session: 会话数据
        steps: 回退的步数，默认为 1
        checkpoint_id: 可选的目标检查点 ID

    返回:
        格式化的人类可读预览字符串
    """
    selected = _select_checkpoints_to_rewind(
        session,
        steps=steps,
        checkpoint_id=checkpoint_id,
    )
    if not selected:
        return f"No checkpoints available to rewind for session {session.session_id[:8]}."

    unique_files: list[str] = []
    seen_paths: set[str] = set()
    for checkpoint in reversed(selected):
        if checkpoint.file_path not in seen_paths:
            unique_files.append(checkpoint.file_path)
            seen_paths.add(checkpoint.file_path)

    lines = [
        f"Rewind preview for session {session.session_id[:8]}:",
        "",
        f"Would restore {len(selected)} checkpoint(s) across {len(unique_files)} file(s).",
    ]
    if checkpoint_id:
        lines.append(f"Target checkpoint: {checkpoint_id[:8]}")

    if any(checkpoint.kind == "rewind" for checkpoint in selected):
        lines.append("Mode: undo prior rewind safety checkpoints.")
    else:
        lines.append("Mode: restore pre-edit file snapshots.")

    lines.extend(["", "Planned restores:"])
    for index, checkpoint in enumerate(reversed(selected), 1):
        created = _fmt_ts(checkpoint.created_at, "%Y-%m-%d %H:%M:%S")
        status = "existing file" if checkpoint.existed else "new file"
        checkpoint_type = _format_checkpoint_type(checkpoint)
        lines.append(
            f"  {index}. [{checkpoint.checkpoint_id[:8]}] {created} - {checkpoint.file_path}"
        )
        lines.append(f"     Restores: {status}")
        lines.append(f"     Type: {checkpoint_type}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Autosave manager
# ---------------------------------------------------------------------------

class AutosaveManager:
    """管理自动会话保存，支持限速和增量差量。 【为什么需要】防止因过于频繁的自动保存而导致 I/O 竞争。
    自动保存使用增量差量保存（快速），显式保存命令使用全量保存（一致）。
    通过脏标记（dirty）+ 时间间隔（interval）双重控制保存频率。
    """

    def __init__(self, session: SessionData, interval: int = AUTOSAVE_INTERVAL_SECONDS):
        """初始化自动保存管理器。 ╔══ 初始化过程 ═══════════════════════════════════════╗
        ║                                                     ║
        ║  第1步 保存会话引用                                   ║
        ║    └─ self.session = session                        ║
        ║                                                     ║
        ║  第2步 设置保存间隔                                   ║
        ║    └─ self.interval = interval（默认 30 秒）        ║
        ║                                                     ║
        ║  第3步 初始化最后保存时间 = 当前时间                   ║
        ║    └─ _last_save_time = time.time()                 ║
        ║                                                     ║
        ║  第4步 初始化脏标记 = False                           ║
        ║    └─ _dirty = False                                ║
        ║                                                     ║
        ║  第5步 全量保存计数器归零                             ║
        ║    └─ _full_save_counter = 0                        ║
        ║                                                     ║
        ╚═════════════════════════════════════════════════════╝

        参数:
            session: 要管理的会话数据
            interval: 自动保存时间间隔（秒），默认为 AUTOSAVE_INTERVAL_SECONDS
        """
        self.session = session
        self.interval = interval
        self._last_save_time = time.time()  # 初始化为当前时间
        self._dirty = False
        self._full_save_counter = 0

    def mark_dirty(self) -> None:
        """将会话标记为需要保存。 【为什么需要】TUI 或其他组件在修改会话后调用此方法，
        标记"有变更待保存"，但不立即触发 I/O 以避免频繁磁盘写入。

        ╔══ 执行流程 ═══════════════════════════════════════╗
        ║                                                     ║
        ║  第1步 设置脏标记为 True                             ║
        ║    └─ self._dirty = True                           ║
        ║                                                     ║
        ╚═════════════════════════════════════════════════════╝
        """
        self._dirty = True

    def should_save(self) -> bool:
        """检查是否应触发自动保存。 【为什么需要】结合脏标记和时间间隔双重判断，避免高频 I/O。
        只有同时满足"有修改"和"距上次保存超过间隔时间"才执行保存。

        ╔══ 执行流程 ═══════════════════════════════════════╗
        ║                                                     ║
        ║  第1步 检查脏标记                                    ║
        ║    ├─ _dirty == False → 无变更 → 返回 False        ║
        ║    └─ _dirty == True → 进入下一步                    ║
        ║                                                     ║
        ║  第2步 计算距上次保存的秒数                           ║
        ║    └─ elapsed = time.time() - _last_save_time       ║
        ║                                                     ║
        ║  第3步 判断是否超过间隔时间                           ║
        ║    ├─ elapsed >= interval → 返回 True（需要保存）    ║
        ║    └─ elapsed < interval → 返回 False（太快了）     ║
        ║                                                     ║
        ╚═════════════════════════════════════════════════════╝

        返回:
            满足保存条件返回 True，否则返回 False
        """
        if not self._dirty:
            return False
        elapsed = time.time() - self._last_save_time
        return elapsed >= self.interval

    def save_if_needed(self) -> bool:
        """如果脏标记已设置且间隔时间已过则执行保存。 【为什么需要】供 TUI 主循环或定时器调用，在满足条件时
        自动触发增量保存，避免用户数据丢失。

        ╔══ 执行流程 ═══════════════════════════════════════╗
        ║                                                     ║
        ║  第1步 调用 should_save() 检查                      ║
        ║    ├─ 返回 False → 跳过，返回 False                ║
        ║    └─ 返回 True → 进入下一步                        ║
        ║                                                     ║
        ║  第2步 执行增量差量保存                               ║
        ║    └─ save_session(session, force_full=False)       ║
        ║       └─ 仅写入新增消息/转录/检查点                  ║
        ║                                                     ║
        ║  第3步 更新最后保存时间                               ║
        ║    └─ _last_save_time = time.time()                ║
        ║                                                     ║
        ║  第4步 清除脏标记                                    ║
        ║    └─ _dirty = False                               ║
        ║                                                     ║
        ║  第5步 递增全量保存计数器                             ║
        ║    └─ _full_save_counter += 1                      ║
        ║                                                     ║
        ║  第6步 返回 True                                    ║
        ║                                                     ║
        ╚═════════════════════════════════════════════════════╝

        返回:
            如果执行了保存则返回 True
        """
        if self.should_save():
            save_session(self.session, force_full=False)
            self._last_save_time = time.time()
            self._dirty = False
            self._full_save_counter += 1
            return True
        return False

    def force_save(self) -> None:
        """忽略间隔时间，强制立即执行全量保存。 【为什么需要】用户执行显式保存命令或退出时调用，
        确保所有数据一定持久化，不依赖脏标记和间隔时间。

        ╔══ 执行流程 ═══════════════════════════════════════╗
        ║                                                     ║
        ║  第1步 强制全量保存                                   ║
        ║    └─ save_session(session, force_full=True)        ║
        ║       └─ 序列化整个 SessionData 到 JSON             ║
        ║                                                     ║
        ║  第2步 更新最后保存时间                               ║
        ║    └─ _last_save_time = time.time()                ║
        ║                                                     ║
        ║  第3步 清除脏标记                                    ║
        ║    └─ _dirty = False                               ║
        ║                                                     ║
        ║  第4步 重置全量保存计数器                             ║
        ║    └─ _full_save_counter = 0                       ║
        ║                                                     ║
        ╚═════════════════════════════════════════════════════╝
        """
        save_session(self.session, force_full=True)
        self._last_save_time = time.time()
        self._dirty = False
        self._full_save_counter = 0


# ---------------------------------------------------------------------------
# Session formatting for display
# ---------------------------------------------------------------------------

def _fmt_ts(ts: float, fmt: str) -> str:
    """使用 datetime 的快速时间戳格式化。

    参数:
        ts: Unix 时间戳（秒）
        fmt: strftime 格式字符串

    返回:
        格式化后的时间字符串
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def format_session_list(sessions: list[SessionMetadata]) -> str:
    """将会话列表格式化为人类可读的文本。

    参数:
        sessions: 会话元数据列表

    返回:
        格式化的会话列表字符串
    """
    if not sessions:
        return "No saved sessions found."

    lines = ["Saved sessions:", ""]
    for i, meta in enumerate(sessions, 1):
        created = _fmt_ts(meta.created_at, "%Y-%m-%d %H:%M")
        workspace = meta.workspace or "unknown"
        first_msg = meta.first_message or "(empty)"
        count = meta.message_count

        lines.append(
            f"  {i}. [{meta.session_id[:8]}] {created} - {workspace}"
        )
        lines.append(f"     Messages: {count} | First: {first_msg}")
        if meta.checkpoint_count:
            lines.append(f"     Checkpoints: {meta.checkpoint_count}")
        if meta.runtime_summary:
            lines.append(f"     Runtime: {meta.runtime_summary}")
        lines.append("")

    lines.append(f"Total: {len(sessions)} session(s)")
    return "\n".join(lines)


def format_session_resume(session: SessionData) -> str:
    """将会话信息格式化为恢复确认的显示文本。

    参数:
        session: 会话数据

    返回:
        格式化的会话恢复摘要字符串
    """
    created = _fmt_ts(session.created_at, "%Y-%m-%d %H:%M:%S")
    updated = _fmt_ts(session.updated_at, "%Y-%m-%d %H:%M:%S")
    return (
        f"Resuming session {session.session_id[:8]}\n"
        f"  Created: {created}\n"
        f"  Updated: {updated}\n"
        f"  Messages: {len(session.messages)}\n"
        f"  Workspace: {session.workspace}"
        + (
            f"\n  Checkpoints: {session.metadata.checkpoint_count}"
            if session.metadata.checkpoint_count
            else ""
        )
        + (
            f"\n  Recent checkpoints: {_format_checkpoint_summary_details(session)}"
            if session.metadata.checkpoint_count
            else ""
        )
        + (
            f"\n  Runtime: {session.metadata.runtime_summary}"
            if session.metadata.runtime_summary
            else ""
        )
        + (
            f"\n  Readiness: {session.metadata.readiness_summary}"
            if session.metadata.readiness_summary
            else ""
        )
        + (
            f"\n  Instructions: {session.metadata.instruction_summary}"
            if session.metadata.instruction_summary
            else ""
        )
        + (
            f"\n  Hooks: {session.metadata.hook_summary}"
            if session.metadata.hook_summary
            else ""
        )
        + (
            f"\n  Delegation: {session.metadata.delegation_summary}"
            if session.metadata.delegation_summary
            else ""
        )
        + (
            f"\n  Extensions: {session.metadata.extension_summary}"
            if session.metadata.extension_summary
            else ""
        )
    )


def _session_entry_preview(text: str, *, limit: int = 96) -> str:
    """生成文本的截断预览。

    参数:
        text: 原始文本
        limit: 最大字符数，默认为 96

    返回:
        截断后的预览字符串，超出部分以 "..." 结尾
    """
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _session_transcript_label(entry: dict[str, Any]) -> str:
    """为转录条目生成本地可读的标签。

    参数:
        entry: 转录条目字典

    返回:
        格式化的标签字符串，如 "runtime:phase"、"tool:Bash/complete" 等
    """
    kind = str(entry.get("kind", "entry") or "entry")
    if entry.get("category") == "runtime":
        runtime_kind = str(entry.get("runtimeKind", "") or "").strip()
        return f"runtime:{runtime_kind}" if runtime_kind else "runtime"
    if kind == "tool":
        tool_name = str(entry.get("toolName", "") or "").strip()
        status = str(entry.get("status", "") or "").strip()
        if tool_name and status:
            return f"tool:{tool_name}/{status}"
        if tool_name:
            return f"tool:{tool_name}"
    return kind


def _format_recent_transcript_lines(
    session: SessionData,
    *,
    limit: int = 8,
) -> list[str]:
    """将最近的转录条目格式化为显示行。

    参数:
        session: 会话数据
        limit: 显示的最大条目数，默认为 8

    返回:
        格式化后的字符串列表
    """
    if not session.transcript_entries:
        return ["  (none)"]

    lines: list[str] = []
    recent_entries = session.transcript_entries[-limit:]
    for entry in recent_entries:
        label = _session_transcript_label(entry)
        preview = _session_entry_preview(str(entry.get("body", "") or "(empty)"))
        lines.append(f"  - [{label}] {preview}")
    return lines


def _format_recent_history_lines(
    session: SessionData,
    *,
    limit: int = 8,
) -> list[str]:
    """将最近的历史条目格式化为显示行。

    参数:
        session: 会话数据
        limit: 显示的最大条目数，默认为 8

    返回:
        格式化后的字符串列表
    """
    if not session.history:
        return ["  (none)"]

    return [
        f"  {index}. {_session_entry_preview(item)}"
        for index, item in enumerate(session.history[-limit:], 1)
    ]


def _format_instruction_layer_lines(
    session: SessionData,
    *,
    limit: int = 6,
) -> list[str]:
    """将指令层格式化为显示行。

    参数:
        session: 会话数据
        limit: 显示的最大层数，默认为 6

    返回:
        格式化后的字符串列表
    """
    if not session.instruction_layers:
        return ["  (none)"]
    lines: list[str] = []
    for layer in session.instruction_layers[:limit]:
        name = _safe_text(layer.get("name")) or "layer"
        scope = _safe_text(layer.get("scope")) or "unknown"
        kind = _safe_text(layer.get("kind")) or "instruction"
        preview = _safe_text(layer.get("preview")) or "(no preview)"
        exists = "present" if layer.get("exists") else "missing"
        lines.append(f"  - {name} [{scope}/{kind}, {exists}] {preview}")
    if len(session.instruction_layers) > limit:
        lines.append(f"  ... {len(session.instruction_layers) - limit} more layer(s)")
    return lines


def _format_hook_status_lines(session: SessionData) -> list[str]:
    """将钩子状态格式化为显示行。

    参数:
        session: 会话数据

    返回:
        格式化后的字符串列表
    """
    if not session.hook_status:
        return ["  (none)"]
    status = session.hook_status
    lines = [
        "  "
        + (
            _safe_text(status.get("summary"))
            or f"{status.get('enabled_hooks', 0)}/{status.get('total_hooks', 0)} hook(s) enabled"
        )
    ]
    hooks = status.get("hooks")
    if isinstance(hooks, list):
        for hook in hooks[:5]:
            lines.append(
                f"  - {hook.get('event', 'hook')} :: {hook.get('last_status', 'idle')}"
                f", calls={hook.get('call_count', 0)}, failures={hook.get('failure_count', 0)}"
            )
    return lines


def _format_delegation_lines(session: SessionData) -> list[str]:
    """将委派状态格式化为显示行。

    参数:
        session: 会话数据

    返回:
        格式化后的字符串列表
    """
    summary = session.metadata.delegation_summary or _summarize_delegation_status(
        session.delegation_status
    )
    lines = [f"  {summary}"] if summary else []
    if not session.delegated_tasks:
        return lines or ["  (none)"]
    for task in session.delegated_tasks[:5]:
        label = _safe_text(task.get("label") or task.get("task_id") or task.get("id")) or "task"
        status = _safe_text(task.get("status")) or "running"
        lines.append(f"  - {label} :: {status}")
    return lines


def _format_extension_lines(
    session: SessionData,
    *,
    limit: int = 6,
) -> list[str]:
    """将扩展清单格式化为显示行。

    参数:
        session: 会话数据
        limit: 显示的最大扩展数，默认为 6

    返回:
        格式化后的字符串列表
    """
    if not session.extension_manifests:
        return ["  (none)"]
    lines: list[str] = []
    for manifest in session.extension_manifests[:limit]:
        name = _safe_text(manifest.get("name")) or "extension"
        scope = _safe_text(manifest.get("scope")) or "unknown"
        version = _safe_text(manifest.get("version")) or "unversioned"
        enabled = "enabled" if manifest.get("enabled", True) else "disabled"
        description = _safe_text(manifest.get("description")) or "(no description)"
        lines.append(f"  - {name} [{scope}] {version}, {enabled} :: {description}")
    if len(session.extension_manifests) > limit:
        lines.append(
            f"  ... {len(session.extension_manifests) - limit} more extension(s)"
        )
    return lines


def _format_readiness_lines(session: SessionData) -> list[str]:
    """将就绪报告格式化为显示行。

    参数:
        session: 会话数据

    返回:
        格式化后的字符串列表
    """
    if not session.readiness_report:
        return ["  (none)"]
    report = session.readiness_report
    provider = _safe_text(report.get("provider")) or "unknown-provider"
    provider_channel = _safe_text(report.get("provider_channel")) or ""
    status = _safe_text(report.get("status")) or "unknown"
    provider_ready = "ready" if report.get("provider_ready") else "not-ready"
    fallback_candidates = list(report.get("fallback_candidates", []) or [])
    viable_fallbacks = set(str(item) for item in list(report.get("viable_fallbacks", []) or []))
    lines = [f"  {status} via {provider} ({provider_ready})"]
    if provider_channel:
        lines.append(f"  channel: {provider_channel}")
    if fallback_candidates:
        lines.append(
            f"  fallback coverage: {len(viable_fallbacks)}/{len(fallback_candidates)} locally ready"
        )
        for candidate in fallback_candidates[:5]:
            label = "ready" if str(candidate) in viable_fallbacks else "not-ready"
            lines.append(f"  - fallback {candidate} [{label}]")
    guidance = report.get("fallback_guidance")
    if isinstance(guidance, list) and guidance:
        for item in guidance[:3]:
            lines.append(f"  - guidance: {item}")
    issues = report.get("issues")
    if isinstance(issues, list) and issues:
        for issue in issues[:5]:
            lines.append(f"  - {issue}")
    return lines


def _format_checkpoint_summary_details(
    session: SessionData,
    *,
    limit: int = 3,
) -> str:
    """将检查点摘要详情格式化为紧凑字符串。

    参数:
        session: 会话数据
        limit: 显示的最新检查点数量，默认为 3

    返回:
        格式化的检查点摘要字符串
    """
    if not session.checkpoints:
        return "none"

    items: list[str] = []
    for checkpoint in reversed(session.checkpoints[-limit:]):
        file_name = Path(checkpoint.file_path).name or checkpoint.file_path
        label = " [rewind]" if getattr(checkpoint, "kind", "edit") == "rewind" else ""
        items.append(f"[{checkpoint.checkpoint_id[:8]}] {file_name}{label}")
    return f"{len(session.checkpoints)} saved; latest " + ", ".join(items)


def _format_checkpoint_type(checkpoint: FileCheckpoint) -> str:
    """将检查点类型格式化为显示字符串。

    参数:
        checkpoint: 文件检查点

    返回:
        类型描述字符串（"rewind safety" 或 "edit"）
    """
    if getattr(checkpoint, "kind", "edit") == "rewind":
        return "rewind safety"
    return "edit"


def format_checkpoint_summary_line(
    session: SessionData | None,
    *,
    limit: int = 3,
) -> str:
    """为 TUI 和转录界面格式化紧凑的检查点摘要。

    参数:
        session: 会话数据（可为 None）
        limit: 显示的最新检查点数量，默认为 3

    返回:
        格式化的检查点摘要字符串；无检查点时返回空字符串
    """
    if not session or not session.checkpoints:
        return ""
    return f"checkpoint-summary: {_format_checkpoint_summary_details(session, limit=limit)}"


def format_session_inspect(
    session: SessionData,
    *,
    transcript_limit: int = 8,
) -> str:
    """为 CLI/会话回放格式化详细的会话检查视图。

    参数:
        session: 会话数据
        transcript_limit: 显示的转录条目数量，默认为 8

    返回:
        格式化的会话检查视图字符串
    """
    created = _fmt_ts(session.created_at, "%Y-%m-%d %H:%M:%S")
    updated = _fmt_ts(session.updated_at, "%Y-%m-%d %H:%M:%S")
    skills = _format_named_collection(session.skills)
    mcp_servers = _format_named_collection(session.mcp_servers)

    lines = [
        f"Session inspect: {session.session_id[:8]}",
        f"  Created: {created}",
        f"  Updated: {updated}",
        f"  Workspace: {session.workspace}",
        f"  Messages: {len(session.messages)}",
        f"  Transcript entries: {len(session.transcript_entries)}",
        f"  History entries: {len(session.history)}",
        f"  Skills: {skills}",
        f"  MCP servers: {mcp_servers}",
        f"  Checkpoints: {session.metadata.checkpoint_count}",
    ]
    if session.metadata.runtime_summary:
        lines.append(f"  Runtime: {session.metadata.runtime_summary}")
    if session.metadata.readiness_summary:
        lines.append(f"  Readiness: {session.metadata.readiness_summary}")
    if session.metadata.instruction_summary:
        lines.append(f"  Instructions: {session.metadata.instruction_summary}")
    if session.metadata.hook_summary:
        lines.append(f"  Hooks: {session.metadata.hook_summary}")
    if session.metadata.delegation_summary:
        lines.append(f"  Delegation: {session.metadata.delegation_summary}")
    if session.metadata.extension_summary:
        lines.append(f"  Extensions: {session.metadata.extension_summary}")

    lines.extend(
        [
            "",
            f"Recent checkpoints: {_format_checkpoint_summary_details(session)}"
            if session.checkpoints
            else "Recent checkpoints: none",
            "",
            "Instruction layers:",
            *_format_instruction_layer_lines(session),
            "",
            "Hook surface:",
            *_format_hook_status_lines(session),
            "",
            "Delegation surface:",
            *_format_delegation_lines(session),
            "",
            "Extensions:",
            *_format_extension_lines(session),
            "",
            "Readiness:",
            *_format_readiness_lines(session),
            "",
            f"Recent transcript ({min(len(session.transcript_entries), transcript_limit)} shown):",
            *_format_recent_transcript_lines(session, limit=transcript_limit),
        ]
    )
    return "\n".join(lines)


def format_session_replay(
    session: SessionData,
    *,
    transcript_limit: int = 16,
    history_limit: int = 8,
    checkpoint_limit: int = 5,
) -> str:
    """为会话格式化面向回放的历史视图。

    参数:
        session: 会话数据
        transcript_limit: 显示的转录条目数量，默认为 16
        history_limit: 显示的历史条目数量，默认为 8
        checkpoint_limit: 显示的检查点数量，默认为 5

    返回:
        格式化的会话回放视图字符串
    """
    created = _fmt_ts(session.created_at, "%Y-%m-%d %H:%M:%S")
    updated = _fmt_ts(session.updated_at, "%Y-%m-%d %H:%M:%S")
    lines = [
        f"Session replay: {session.session_id[:8]}",
        f"  Workspace: {session.workspace}",
        f"  Created: {created}",
        f"  Updated: {updated}",
        f"  Runtime: {session.metadata.runtime_summary or '(none)'}",
        f"  Checkpoints: {session.metadata.checkpoint_count}",
    ]
    if session.metadata.readiness_summary:
        lines.append(f"  Readiness: {session.metadata.readiness_summary}")
        readiness_details = _format_readiness_lines(session)
        if readiness_details and readiness_details != ["  (none)"]:
            lines.extend(readiness_details[1:])
    if session.metadata.delegation_summary:
        lines.append(f"  Delegation: {session.metadata.delegation_summary}")
    lines.extend(
        [
            "",
            f"Checkpoint trail ({min(len(session.checkpoints), checkpoint_limit)} shown):",
        ]
    )
    if session.checkpoints:
        for checkpoint in reversed(session.checkpoints[-checkpoint_limit:]):
            created_at = _fmt_ts(checkpoint.created_at, "%Y-%m-%d %H:%M:%S")
            file_name = Path(checkpoint.file_path).name or checkpoint.file_path
            checkpoint_type = _format_checkpoint_type(checkpoint)
            lines.append(
                f"  - [{checkpoint.checkpoint_id[:8]}] {created_at} :: {file_name} ({checkpoint_type})"
            )
    else:
        lines.append("  (none)")

    lines.extend(
        [
            "",
            "Instruction layers:",
            *_format_instruction_layer_lines(session, limit=4),
            "",
            "Extensions:",
            *_format_extension_lines(session, limit=4),
            "",
            f"Prompt history ({min(len(session.history), history_limit)} shown):",
            *_format_recent_history_lines(session, limit=history_limit),
            "",
            f"Transcript timeline ({min(len(session.transcript_entries), transcript_limit)} shown):",
            *_format_recent_transcript_lines(session, limit=transcript_limit),
        ]
    )
    return "\n".join(lines)


def format_session_checkpoints(session: SessionData) -> str:
    """格式化回退检查点用于查看。

    参数:
        session: 会话数据

    返回:
        格式化后的检查点列表字符串
    """
    if not session.checkpoints:
        return f"No checkpoints saved for session {session.session_id[:8]}."

    lines = [
        f"Checkpoints for session {session.session_id[:8]}:",
        "",
    ]
    for index, checkpoint in enumerate(reversed(session.checkpoints), 1):
        created = _fmt_ts(checkpoint.created_at, "%Y-%m-%d %H:%M:%S")
        status = "existing file" if checkpoint.existed else "new file"
        checkpoint_type = _format_checkpoint_type(checkpoint)
        lines.append(
            f"  {index}. [{checkpoint.checkpoint_id[:8]}] {created} - {checkpoint.file_path}"
        )
        lines.append(f"     Restores: {status}")
        lines.append(f"     Type: {checkpoint_type}")
    lines.append("")
    lines.append(f"Total: {len(session.checkpoints)} checkpoint(s)")
    return "\n".join(lines)
