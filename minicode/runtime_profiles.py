"""SmartCode 运行时 Profile 定义与管理。

定义不同的运行时 profile，每个 profile 包含 agent 轮次执行时使用的参数，
如最大步骤数、空响应重试限制、working memory 配置、widening 策略等。

内置 profile：
- "single"：标准单次执行模式，适用于常规任务
- "single-deep"：深度单次执行模式，适用于复杂任务，允许更多步骤和重试
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """具名的运行时 profile，控制一次 agent 轮次的行为。

    定义 agent 在单次轮次中使用的各项参数，包括最大步骤数、
    空响应和可恢复思考的重试限制、working memory 生命周期、
    严格步骤验证和 widening 策略等。
    """

    name: str
    max_steps: int | None
    empty_response_retry_limit: int = 2
    recoverable_thinking_retry_limit: int = 3
    working_memory_ttl_seconds: float | None = 1800
    working_memory_importance: float = 1.0
    strict_step_verification: bool = False
    widen_after_step: int | None = None
    widening_step_bonus: int = 0


_PROFILES: dict[str, RuntimeProfile] = {
    "single": RuntimeProfile(
        name="single",
        max_steps=10,
        empty_response_retry_limit=2,
        recoverable_thinking_retry_limit=3,
        working_memory_ttl_seconds=1800,
        working_memory_importance=1.0,
        strict_step_verification=False,
        widen_after_step=None,
        widening_step_bonus=0,
    ),
    "single-deep": RuntimeProfile(
        name="single-deep",
        max_steps=80,
        empty_response_retry_limit=3,
        recoverable_thinking_retry_limit=5,
        working_memory_ttl_seconds=7200,
        working_memory_importance=1.4,
        strict_step_verification=True,
        widen_after_step=6,
        widening_step_bonus=6,
    ),
}


def get_runtime_profile(name: str | None) -> RuntimeProfile:
    """根据名称获取运行时 profile。

    如果 name 为 None 或空字符串，返回默认的 "single" profile。
    名称匹配不区分大小写，自动去除首尾空白。
    如果未找到匹配的 profile，也返回默认的 "single" profile。

    参数:
        name: profile 名称，可以为 None。

    返回:
        匹配的 RuntimeProfile 对象，默认返回 "single"。
    """
    key = str(name or "single").strip().lower()
    return _PROFILES.get(key, _PROFILES["single"])


def resolve_runtime_profile(
    runtime: Mapping[str, Any] | None,
    *,
    fallback_max_steps: int | None = None,
) -> RuntimeProfile:
    """从 runtime 配置字典中解析出最终的 RuntimeProfile。

    从 runtime 字典中提取 "runtimeProfile" 键作为 profile 名称，
    并根据 profile 类型和 fallback_max_steps 参数计算最终的 max_steps：
    - "single-deep" profile：取 max(profile.max_steps, fallback_max_steps)
    - 其他 profile：直接使用 fallback_max_steps
    - profile.max_steps 为 None 时使用 fallback_max_steps

    参数:
        runtime: 包含运行时配置的映射，可包含 "runtimeProfile" 键。
        fallback_max_steps: 可选的备选最大步骤数。

    返回:
        解析后的 RuntimeProfile 对象。
    """
    requested_name = runtime.get("runtimeProfile") if runtime else None
    profile = get_runtime_profile(str(requested_name or "single"))

    resolved_max_steps = profile.max_steps
    if fallback_max_steps is not None:
        if profile.name == "single-deep":
            if resolved_max_steps is None:
                resolved_max_steps = fallback_max_steps
            else:
                resolved_max_steps = max(resolved_max_steps, fallback_max_steps)
        else:
            resolved_max_steps = fallback_max_steps

    return replace(profile, max_steps=resolved_max_steps)
