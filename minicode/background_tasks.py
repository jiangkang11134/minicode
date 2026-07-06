"""后台任务管理模块，支持跨平台的后台进程注册、状态监控与槽位管理。

提供后台 shell 任务的注册与状态查询功能，支持通过 PID 检测进程
存活状态（兼容 Windows 和 Unix），同时维护任务执行槽位限制，
并可注册完成回调函数以响应任务结束事件。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from typing import Any, Callable

from minicode.tooling import BackgroundTaskResult

# 内存中的后台任务注册表
_background_tasks: dict[str, dict[str, Any]] = {}

# 任务槽位管理
_max_slots: int = 5  # 最大并发后台任务数
_slot_callbacks: dict[str, Callable] = {}  # 任务完成回调函数注册表

# 用于并发工具执行的线程安全锁
_lock = threading.Lock()


def _is_process_alive(pid: int) -> bool | None:
    """检查进程是否存活，跨平台实现。

    在 Windows 上使用 ctypes 调用 OpenProcess/GetExitCodeProcess；
    在 Unix 上使用 os.kill(pid, 0) 发送信号 0。

    返回:
        True  — 进程存活
        False — 进程已确定结束
        None  — 无法确定（视为"已失败"）
    """
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) raises OSError for *every* case
        # (including when the process exists), so we use ctypes instead.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # Cannot open → process gone

            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    else:
        # Unix: signal 0 checks existence without actually sending a signal
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM — process exists but we can't signal it; still alive
            return True
        except OSError:
            return None


def _refresh_record(record: dict[str, Any]) -> dict[str, Any]:
    """检查正在运行的进程是否仍存活，并更新记录状态。

    对于状态为 "running" 的记录，通过 PID 检测进程存活状态：
    - 存活：保持状态不变
    - 已结束：状态更新为 "completed"
    - 无法确定：状态更新为 "failed"

    参数:
        record: 后台任务记录字典，应包含 status 和 pid 字段

    返回:
        更新后的记录字典
    """
    if record.get("status") != "running":
        return record
    pid = record.get("pid")
    if pid is None:
        return record

    alive = _is_process_alive(pid)
    if alive is True:
        return record
    elif alive is False:
        record["status"] = "completed"
    else:
        record["status"] = "failed"
    return record


def register_background_shell_task(command: str, pid: int, cwd: str) -> BackgroundTaskResult:
    """注册一个后台 shell 任务到任务管理系统。

    生成唯一任务 ID，创建 BackgroundTaskResult 记录，并将任务信息
    存入内存注册表。cwd 参数被忽略（保留接口兼容性）。

    参数:
        command: 执行的 shell 命令字符串
        pid: 进程 ID
        cwd: 工作目录（当前未使用）

    返回:
        BackgroundTaskResult，包含任务 ID、类型、命令、PID 和状态
    """
    del cwd
    result = BackgroundTaskResult(
        taskId=f"task_{uuid.uuid4().hex[:8]}",
        type="local_bash",
        command=command,
        pid=pid,
        status="running",
        startedAt=int(time.time() * 1000),
    )
    with _lock:
        _background_tasks[result.taskId] = {
            "taskId": result.taskId,
            "type": result.type,
            "command": result.command,
            "pid": result.pid,
            "status": result.status,
            "startedAt": result.startedAt,
            "label": command[:60],
        }
    return result


def list_background_tasks() -> list[dict[str, Any]]:
    """返回所有当前跟踪的后台任务列表（刷新状态后）。

    对每个任务调用 _refresh_record 更新其存活状态，
    确保返回的数据反映最新的进程状态。

    返回:
        后台任务记录字典列表，每个字典包含 taskId、type、
        command、pid、status 等字段
    """
    with _lock:
        records = list(_background_tasks.values())
    return [_refresh_record(record) for record in records]


def get_background_task(task_id: str) -> dict[str, Any] | None:
    """根据任务 ID 获取单个后台任务（刷新状态后）。

    参数:
        task_id: 任务唯一标识

    返回:
        任务记录字典；如果任务不存在则返回 None
    """
    with _lock:
        record = _background_tasks.get(task_id)
    if record is None:
        return None
    return _refresh_record(record)


# ---------------------------------------------------------------------------
# 任务槽位管理
# ---------------------------------------------------------------------------

def get_slot_stats() -> dict[str, Any]:
    """获取当前任务槽位的使用统计信息。

    统计运行中任务数、总跟踪任务数，并计算可用槽位数。

    返回:
        包含 used_slots、max_slots、available_slots、total_tracked 的字典
    """
    with _lock:
        running = sum(1 for r in _background_tasks.values() if r.get("status") == "running")
        total = len(_background_tasks)
        max_slots = _max_slots
    return {
        "used_slots": running,
        "max_slots": max_slots,
        "available_slots": max_slots - running,
        "total_tracked": total,
    }


def can_start_new_task() -> bool:
    """检查是否有可用槽位启动新任务。

    返回:
        有可用槽位返回 True，否则返回 False
    """
    stats = get_slot_stats()
    return stats["available_slots"] > 0


def set_max_slots(max_slots: int) -> None:
    """设置最大并发后台任务数。

    确保最小值至少为 1，防止配置错误导致无法启动任何任务。

    参数:
        max_slots: 目标最大槽位数
    """
    global _max_slots
    with _lock:
        _max_slots = max(1, max_slots)  # 至少保留 1 个槽位


def register_completion_callback(task_id: str, callback: Callable) -> None:
    """为指定的任务注册完成回调函数。

    当 check_completed_tasks 检测到该任务完成时，将自动调用回调。
    回调接收 (task_id, record) 两个参数。

    参数:
        task_id: 要监视的任务 ID
        callback: 任务完成时调用的可调用对象
    """
    with _lock:
        _slot_callbacks[task_id] = callback


def check_completed_tasks() -> list[str]:
    """检查是否有任务已完成，并触发相应的完成回调。

    遍历所有状态为 "running" 的任务，刷新其存活状态。
    对状态变为非 running 的任务，执行注册的完成回调（如有）。
    回调异常会被静默捕获，不会中断遍历过程。

    返回:
        已完成任务的 ID 列表
    """
    completed: list[str] = []
    with _lock:
        records = list(_background_tasks.items())
    for task_id, record in records:
        if record.get("status") != "running":
            continue
        refreshed = _refresh_record(record)
        if refreshed["status"] == "running":
            continue
        with _lock:
            _background_tasks[task_id] = refreshed
            callback = _slot_callbacks.pop(task_id, None)
        completed.append(task_id)
        # 触发回调
        if callback:
            try:
                callback(task_id, refreshed)
            except Exception:
                pass  # 不让回调错误中断遍历循环
    return completed


def format_slot_status() -> str:
    """将任务槽位状态格式化为可读字符串，用于展示。

    包含槽位使用统计和当前正在运行的任务列表。

    返回:
        格式化后的状态描述字符串
    """
    stats = get_slot_stats()
    running_tasks = [
        r for r in _background_tasks.values() if r.get("status") == "running"
    ]

    lines = [
        "Background Task Slots",
        "=" * 50,
        f"Slots: {stats['used_slots']}/{stats['max_slots']} used",
        f"Available: {stats['available_slots']}",
        f"Total tracked: {stats['total_tracked']}",
        "",
    ]

    if running_tasks:
        lines.append("Running Tasks:")
        for task in running_tasks:
            lines.append(
                f"  [{task.get('taskId', '?')}] {task.get('label', task.get('command', 'unknown'))}"
            )
        lines.append("")

    return "\n".join(lines)
