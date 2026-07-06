"""持久化任务图，用于跨步骤工作流管理。

灵感来自 Learn Claude Code 最佳实践：
- 区分会话级计划与持久化任务协调
- 分离任务定义（做什么）与执行槽（谁在运行/进度）
- 后台任务槽管理与定时调度
- 针对高风险操作的工作树隔离执行

提供：
- TaskGraph: 带有依赖关系的有向无环图（DAG）
- TaskSlot: 带状态跟踪的命名执行槽
- WorktreeIsolator: 用于高风险操作的临时工作树
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR


# ---------------------------------------------------------------------------
# Task Graph
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    """任务执行状态枚举。"""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskPriority(str, Enum):
    """任务优先级级别枚举。"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class TaskDefinition:
    """任务定义，描述需要完成的工作（持久化、跨会话）。

    包含任务的名称、描述、依赖关系、优先级、超时时间等元信息。
    该类的生命周期跨越多个会话，保存在磁盘上。
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    description: str = ""
    dependencies: list[str] = field(default_factory=list)
    priority: TaskPriority = TaskPriority.NORMAL
    timeout_seconds: int = 300
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSlot:
    """任务执行槽，记录谁在执行以及当前进度（会话级）。

    用于跟踪单个任务在某个槽中的运行状态、进度百分比、
    开始/结束时间、错误信息和执行结果。
    """

    task_id: str
    slot_name: str = "default"
    state: TaskState = TaskState.PENDING
    progress: float = 0.0  # 0.0 - 1.0
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    result: str | None = None


@dataclass
class TaskGraph:
    """持久化任务图，包含定义和执行槽。

    管理一组任务定义（TaskDefinition）和对应的执行槽（TaskSlot），
    提供添加任务、分配槽位、状态流转、依赖检测等核心功能，
    并支持序列化到磁盘。
    """

    name: str = ""
    definitions: dict[str, TaskDefinition] = field(default_factory=dict)
    slots: dict[str, TaskSlot] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- Definition API ---
    def add_task(
        self,
        name: str,
        description: str = "",
        dependencies: list[str] | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout_seconds: int = 300,
    ) -> TaskDefinition:
        """向图中添加一个任务定义。

        创建新的 TaskDefinition 并将其注册到图的 definitions 字典中。
        自动更新图的 updated_at 时间戳。

        参数:
            name: 任务名称
            description: 任务描述
            dependencies: 依赖的任务 ID 列表，这些任务完成后当前任务才能执行
            priority: 任务优先级，默认为 NORMAL
            timeout_seconds: 任务超时时间（秒），默认 300 秒

        返回:
            新创建的 TaskDefinition 实例
        """
        task_def = TaskDefinition(
            name=name,
            description=description,
            dependencies=dependencies or [],
            priority=priority,
            timeout_seconds=timeout_seconds,
        )
        self.definitions[task_def.id] = task_def
        self.updated_at = time.time()
        return task_def

    # --- Slot API ---
    def assign_slot(self, task_id: str, slot_name: str = "default") -> TaskSlot:
        """将任务分配到一个执行槽。

        参数:
            task_id: 要分配的任务 ID
            slot_name: 槽名称，默认为 "default"

        返回:
            新创建的 TaskSlot 实例

        抛出:
            ValueError: 如果 task_id 对应的任务定义不存在
        """
        if task_id not in self.definitions:
            raise ValueError(f"Task {task_id} not found")

        slot = TaskSlot(task_id=task_id, slot_name=slot_name)
        slot_key = f"{slot_name}:{task_id}"
        self.slots[slot_key] = slot
        self.updated_at = time.time()
        return slot

    def start_task(self, slot_key: str) -> TaskSlot:
        """将指定槽中的任务标记为运行中。

        参数:
            slot_key: 槽的键名，格式为 "slot_name:task_id"

        返回:
            更新后的 TaskSlot 实例

        抛出:
            ValueError: 如果 slot_key 对应的槽不存在
        """
        slot = self.slots.get(slot_key)
        if not slot:
            raise ValueError(f"Slot {slot_key} not found")
        slot.state = TaskState.RUNNING
        slot.started_at = time.time()
        slot.progress = 0.0
        self.updated_at = time.time()
        return slot

    def complete_task(self, slot_key: str, result: str = "") -> TaskSlot:
        """将指定槽中的任务标记为已完成。

        参数:
            slot_key: 槽的键名
            result: 任务的执行结果描述

        返回:
            更新后的 TaskSlot 实例

        抛出:
            ValueError: 如果 slot_key 对应的槽不存在
        """
        slot = self.slots.get(slot_key)
        if not slot:
            raise ValueError(f"Slot {slot_key} not found")
        slot.state = TaskState.COMPLETED
        slot.completed_at = time.time()
        slot.progress = 1.0
        slot.result = result
        self.updated_at = time.time()
        return slot

    def fail_task(self, slot_key: str, error: str) -> TaskSlot:
        """将指定槽中的任务标记为失败。

        参数:
            slot_key: 槽的键名
            error: 错误描述信息

        返回:
            更新后的 TaskSlot 实例

        抛出:
            ValueError: 如果 slot_key 对应的槽不存在
        """
        slot = self.slots.get(slot_key)
        if not slot:
            raise ValueError(f"Slot {slot_key} not found")
        slot.state = TaskState.FAILED
        slot.completed_at = time.time()
        slot.error = error
        self.updated_at = time.time()
        return slot

    # --- Graph Logic ---
    def get_ready_tasks(self) -> list[TaskDefinition]:
        """获取所有依赖已满足、可执行的任务。

        检查每个任务定义：
        1. 是否已经完成
        2. 是否正在运行
        3. 所有依赖是否都已完成

        返回的列表按优先级排序（CRITICAL -> HIGH -> NORMAL -> LOW）。

        返回:
            准备就绪的 TaskDefinition 列表
        """
        completed_task_ids = {
            slot.task_id for slot in self.slots.values()
            if slot.state == TaskState.COMPLETED
        }

        ready = []
        for task_def in self.definitions.values():
            if task_def.id in completed_task_ids:
                continue
            # Check if already running
            if any(
                s.task_id == task_def.id and s.state == TaskState.RUNNING
                for s in self.slots.values()
            ):
                continue
            # Check dependencies
            if all(dep in completed_task_ids for dep in task_def.dependencies):
                ready.append(task_def)

        # Sort by priority
        priority_order = {
            TaskPriority.CRITICAL: 0,
            TaskPriority.HIGH: 1,
            TaskPriority.NORMAL: 2,
            TaskPriority.LOW: 3,
        }
        ready.sort(key=lambda t: priority_order.get(t.priority, 2))
        return ready

    def is_graph_complete(self) -> bool:
        """检查图中所有任务是否都已完成。

        如果没有定义任何任务，视为已完成。

        返回:
            如果所有任务均已完成后返回 True，否则返回 False
        """
        if not self.definitions:
            return True
        completed_ids = {
            slot.task_id for slot in self.slots.values()
            if slot.state == TaskState.COMPLETED
        }
        return all(tid in completed_ids for tid in self.definitions)

    def get_progress_percentage(self) -> float:
        """计算整张图的总体进度百分比。

        返回:
            已完成任务数占总任务数的百分比（0.0 - 100.0）
        """
        if not self.definitions:
            return 0.0
        completed = sum(
            1 for slot in self.slots.values()
            if slot.state == TaskState.COMPLETED
        )
        return (completed / len(self.definitions)) * 100

    # --- Persistence ---
    def to_dict(self) -> dict[str, Any]:
        """将 TaskGraph 序列化为字典格式，用于 JSON 持久化。

        返回:
            包含图所有字段的字典，优先级以字符串值存储
        """
        return {
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "definitions": {
                tid: {
                    "id": td.id,
                    "name": td.name,
                    "description": td.description,
                    "dependencies": td.dependencies,
                    "priority": td.priority.value,
                    "timeout_seconds": td.timeout_seconds,
                    "created_at": td.created_at,
                    "metadata": td.metadata,
                }
                for tid, td in self.definitions.items()
            },
            "slots": {
                sk: {
                    "task_id": s.task_id,
                    "slot_name": s.slot_name,
                    "state": s.state.value,
                    "progress": s.progress,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "error": s.error,
                    "result": s.result,
                }
                for sk, s in self.slots.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraph:
        """从字典反序列化创建 TaskGraph 实例。

        参数:
            data: 由 to_dict() 生成的字典数据

        返回:
            反序列化后的 TaskGraph 实例
        """
        graph = cls(
            name=data.get("name", ""),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )
        for tid, td_data in data.get("definitions", {}).items():
            graph.definitions[tid] = TaskDefinition(
                id=td_data["id"],
                name=td_data["name"],
                description=td_data.get("description", ""),
                dependencies=td_data.get("dependencies", []),
                priority=TaskPriority(td_data.get("priority", "normal")),
                timeout_seconds=td_data.get("timeout_seconds", 300),
                created_at=td_data.get("created_at", time.time()),
                metadata=td_data.get("metadata", {}),
            )
        for sk, s_data in data.get("slots", {}).items():
            graph.slots[sk] = TaskSlot(
                task_id=s_data["task_id"],
                slot_name=s_data.get("slot_name", "default"),
                state=TaskState(s_data.get("state", "pending")),
                progress=s_data.get("progress", 0.0),
                started_at=s_data.get("started_at"),
                completed_at=s_data.get("completed_at"),
                error=s_data.get("error"),
                result=s_data.get("result"),
            )
        return graph


# ---------------------------------------------------------------------------
# Worktree Isolator (for risky operations)
# ---------------------------------------------------------------------------

class WorktreeIsolator:
    """创建临时 git worktree 用于执行高风险任务。

    提供隔离机制，使探索性或破坏性操作不影响主工作目录。
    每个隔离任务在独立的 git worktree 中运行，完成后可清理。
    """

    def __init__(self, base_path: Path, prefix: str = "isolated_task") -> None:
        """初始化 WorktreeIsolator。

        参数:
            base_path: 用于创建工作树的基路径
            prefix: 工作树目录和分支名称的前缀，默认为 "isolated_task"
        """
        self.base_path = base_path
        self.prefix = prefix
        self.active_worktrees: list[Path] = []

    def create_worktree(self, task_id: str) -> Path:
        """为指定任务创建一个新的 worktree。

        在 base_path 下创建以 prefix_task_id 命名的目录，
        并在该目录上创建一个同名的孤儿分支。

        参数:
            task_id: 任务 ID，用于命名 worktree 目录和分支

        返回:
            创建的 worktree 目录的 Path 对象
        """
        import subprocess

        worktree_path = self.base_path / f"{self.prefix}_{task_id}"
        worktree_path.mkdir(parents=True, exist_ok=True)

        # Create a new orphan branch for isolation
        branch_name = f"{self.prefix}_{task_id}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "--detach"],
            capture_output=True,
            text=True,
        )

        self.active_worktrees.append(worktree_path)
        return worktree_path

    def cleanup_worktree(self, worktree_path: Path) -> None:
        """移除指定的 worktree 及其目录。

        首先尝试通过 git worktree remove 命令移除，
        如果目录仍然存在，则强制删除。清除 active_worktrees 列表中的记录。

        参数:
            worktree_path: 要清理的 worktree 目录路径
        """
        import subprocess

        try:
            subprocess.run(
                ["git", "worktree", "remove", "-f", str(worktree_path)],
                capture_output=True,
                text=True,
            )
        except Exception:
            pass  # Best effort cleanup

        # Remove directory if it still exists
        if worktree_path.exists():
            import shutil
            try:
                shutil.rmtree(worktree_path)
            except Exception:
                pass

        if worktree_path in self.active_worktrees:
            self.active_worktrees.remove(worktree_path)

    def cleanup_all(self) -> None:
        """移除所有活跃的 worktree。"""
        for wt in list(self.active_worktrees):
            self.cleanup_worktree(wt)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_TASK_GRAPH_DIR = MINI_CODE_DIR / "task_graphs"


def save_task_graph(graph: TaskGraph, graph_id: str) -> Path:
    """将任务图保存到磁盘。

    在 _TASK_GRAPH_DIR 目录下创建以 graph_id 命名的 JSON 文件。

    参数:
        graph: 要保存的 TaskGraph 实例
        graph_id: 图的唯一标识符，用作文件名

    返回:
        保存文件的 Path 对象
    """
    _TASK_GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    graph_file = _TASK_GRAPH_DIR / f"{graph_id}.json"
    graph_file.write_text(
        json.dumps(graph.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return graph_file


def load_task_graph(graph_id: str) -> TaskGraph | None:
    """从磁盘加载任务图。

    参数:
        graph_id: 图的唯一标识符

    返回:
        反序列化后的 TaskGraph 实例，如果文件不存在或格式错误则返回 None
    """
    graph_file = _TASK_GRAPH_DIR / f"{graph_id}.json"
    if not graph_file.exists():
        return None
    try:
        data = json.loads(graph_file.read_text(encoding="utf-8"))
        return TaskGraph.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return None


def list_task_graphs() -> list[str]:
    """列出所有已保存的任务图 ID。

    返回:
        任务图 ID 的字符串列表（不带 .json 后缀）
    """
    if not _TASK_GRAPH_DIR.exists():
        return []
    return [f.stem for f in _TASK_GRAPH_DIR.glob("*.json")]


def delete_task_graph(graph_id: str) -> bool:
    """删除已保存的任务图文件。

    参数:
        graph_id: 要删除的图 ID

    返回:
        如果文件存在并成功删除返回 True，否则返回 False
    """
    graph_file = _TASK_GRAPH_DIR / f"{graph_id}.json"
    if graph_file.exists():
        graph_file.unlink()
        return True
    return False
