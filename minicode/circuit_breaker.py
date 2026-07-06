"""Compaction 失败的断路器（Circuit Breaker）。

防止连续 compaction 失败时陷入无限重试循环。
灵感来自 Claude Code 的自动压缩断路器（最多连续 3 次失败后永久阻塞，直到手动重置）。

设计：
  - 每次 compaction 失败时计数器递增
  - 达到阈值（默认 3）后，compaction 被阻塞
  - 手动重置或任意一次成功 compaction 均可恢复
  - 阻塞状态在显式重置前保持粘性
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class CircuitBreakerConfig:
    """Compaction 断路器的配置项。 """

    # 跳闸前允许的连续失败次数
    failure_threshold: int = 3

    # 跳闸后，自动重置的等待时间（秒）。0 表示永不自动重置（仅手动）。
    auto_reset_seconds: float = 0.0


@dataclass
class CircuitBreakerState:
    """断路器内部状态的快照，用于外部检测。 """

    consecutive_failures: int = 0
    is_open: bool = False
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: float | None = None
    last_success_time: float | None = None
    opened_at: float | None = None


class CompactionCircuitBreaker:
    """追踪 compaction 尝试次数，在连续失败后阻断操作。 """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        """初始化断路器。

        参数:
            config: 断路器配置，未提供时使用默认配置。
        """
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState()
        self._lock = threading.Lock()

    # ── public API ──────────────────────────────────────────────────────────

    def record_success(self) -> None:
        """标记一次成功的 compaction 操作。 """
        with self._lock:
            self._state.consecutive_failures = 0
            self._state.total_successes += 1
            self._state.last_success_time = time.time()

    def record_failure(self) -> None:
        """标记一次失败的 compaction 操作，可能触发断路器断开。 """
        with self._lock:
            self._state.consecutive_failures += 1
            self._state.total_failures += 1
            self._state.last_failure_time = time.time()
            if self._state.consecutive_failures >= self.config.failure_threshold:
                self._state.is_open = True
                self._state.opened_at = time.time()

    def is_allowed(self) -> bool:
        """检查 compaction 当前是否被允许执行。

        如果断路器处于断开状态，会检查是否已超过自动重置时间；
        若满足条件则自动重置并允许通过。

        返回:
            True 表示 compaction 可以执行，False 表示被阻断。
        """
        with self._lock:
            if not self._state.is_open:
                return True
            # Check auto-reset
            if (
                self.config.auto_reset_seconds > 0
                and self._state.opened_at is not None
                and time.time() - self._state.opened_at >= self.config.auto_reset_seconds
            ):
                self._reset()
                return True
            return False

    def reset(self) -> None:
        """手动将断路器重置为关闭状态。 """
        with self._lock:
            self._reset()

    def get_state(self) -> CircuitBreakerState:
        """返回当前状态的快照。 """
        with self._lock:
            return CircuitBreakerState(
                consecutive_failures=self._state.consecutive_failures,
                is_open=self._state.is_open,
                total_failures=self._state.total_failures,
                total_successes=self._state.total_successes,
                last_failure_time=self._state.last_failure_time,
                last_success_time=self._state.last_success_time,
                opened_at=self._state.opened_at,
            )

    def _reset(self) -> None:
        """内部重置逻辑（调用者必须持有 _lock）。 """
        self._state.consecutive_failures = 0
        self._state.is_open = False
        self._state.opened_at = None


# ── Module-level convenience ─────────────────────────────────────────────────

_default_breaker: CompactionCircuitBreaker | None = None


def get_compaction_circuit_breaker() -> CompactionCircuitBreaker:
    """获取或创建模块级别的 compaction 断路器单例。 """
    global _default_breaker
    if _default_breaker is None:
        _default_breaker = CompactionCircuitBreaker()
    return _default_breaker
