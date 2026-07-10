"""模型调用模块 — 封装 LLM 适配器的调用、回退、错误处理。

从 agent_loop_lite.py 拆分而来，职责：
1. 调用模型适配器（_model_next）
2. 格式化 API 故障消息（_summarize_model_api_failure）
3. 上下文阻塞限制计算（_is_at_blocking_limit）
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable

from minicode.config import describe_fallback_guidance, describe_provider_channel
from minicode.logging_config import get_logger
from minicode.model_registry import detect_provider
from minicode.state import AppState, Store
from minicode.types import AgentStep, ChatMessage, ModelAdapter

logger = get_logger("model_caller")


# ══════════════════════════════════════════════════════════════
# 错误分类与摘要
# ══════════════════════════════════════════════════════════════


def _looks_like_provider_availability_error(error_message: str) -> bool:
    """判断错误是否由服务端临时不可用（而非客户端配置问题）引起。

    【为什么需要】当所有 fallback 都失败时，需要区分是"服务端临时故障"还是"用户配置错误"。
    前者提示用户等服务恢复，后者提示检查配置。

    参数:
        error_message: 模型 API 返回的原始错误消息

    返回:
        True=服务端不可用，False=其他原因
    """
    normalized = error_message.lower()
    return any(marker in normalized for marker in (
        "no available channel", "temporarily unavailable", "service unavailable",
        "please try again later", "capacity exceeded", "overloaded", "high demand",
        "503", "502", "500",
    ))


def _summarize_model_api_failure(
    *,
    error_type: str,
    error: Exception,
    active_model_id: str = "",
    fallback_errors: list[str] | None = None,
    runtime: dict[str, Any] | None = None,
) -> str:
    """把模型 API 错误翻译成用户可读的故障消息，含解决指引。

    【为什么需要】原始 API 错误是给开发者看的（HTTP 状态码），普通用户看不懂。
    这个函数做两层转化：①把原始错误翻译成自然语言；②如果所有 fallback 都失败了，
    分析是服务端故障还是配置问题，给出下一步操作指引。

    参数:
        error_type: 异常类型名（如 ConnectionError）
        error: 异常实例
        active_model_id: 当前活跃的模型 ID（可选）
        fallback_errors: fallback 切换过程中的错误列表（可选）
        runtime: 运行时配置字典（可选）

    返回:
        格式化的故障摘要字符串，用户可直接看到
    """
    fallback_errors = fallback_errors or []
    if fallback_errors:
        combined = " ".join(fallback_errors)
        if (
            "no viable fallback models were available" in combined.lower()
            and any(_looks_like_provider_availability_error(item) for item in fallback_errors + [str(error)])
        ):
            runtime = runtime or {}
            guidance_model = (
                str(runtime.get("configuredModel", "")).strip()
                or str(runtime.get("model", "")).strip()
                or active_model_id or "the active model"
            )
            provider = detect_provider(guidance_model, runtime).value if guidance_model else "unknown"
            channel = describe_provider_channel(runtime, provider)
            guidance = describe_fallback_guidance(runtime, provider_name=provider, current_model=guidance_model)
            guidance_suffix = f" Next step: {guidance[0]}" if guidance else ""
            return (
                f"Provider availability failure: {guidance_model} failed and all viable "
                f"fallback models were unavailable. Active channel: {channel}. "
                f"Last error ({error_type}): {error}{guidance_suffix}"
            )
    return f"Model API error ({error_type}): {error}"


def _extract_model_id_from_provider_error(error: Exception) -> str:
    """从错误消息中提取模型名。

    【为什么需要】API 错误消息里可能提到了具体模型名（"model claude-sonnet-4 under group..."），
    但模型名没有作为单独字段返回，需要用正则从文本里抠出来用于日志和 fallback 决策。

    参数:
        error: 异常实例

    返回:
        模型 ID 字符串，没找到则返回空字符串
    """
    message = str(error)
    match = re.search(r"model\s+([^\s]+)\s+under\s+group", message, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _infer_active_model_id(
    model: ModelAdapter,
    runtime: dict[str, Any] | None,
    error: Exception | None = None,
) -> str:
    """从多个来源推断当前模型 ID。

    【为什么需要】模型 ID 可能存在于多个地方（适配器属性、runtime 配置、错误消息里），
    而且不同场景下可能有的地方为空。这个函数统一查找逻辑，调用者不需要关心从哪拿。

    优先级：model.model_id → runtime["model"] → 错误消息提取

    参数:
        model: 模型适配器实例
        runtime: 运行时配置（可选）
        error: 异常实例，用于从错误消息中提取（可选）

    返回:
        模型 ID 字符串，没找到则返回空字符串
    """
    explicit = str(getattr(model, "model_id", "") or "").strip()
    if explicit:
        return explicit
    runtime_model = str((runtime or {}).get("model", "") or "").strip()
    if runtime_model:
        return runtime_model
    if error is not None:
        return _extract_model_id_from_provider_error(error)
    return ""


# ══════════════════════════════════════════════════════════════
# 上下文阻塞限制
# ══════════════════════════════════════════════════════════════

def _is_at_blocking_limit(
    token_count: int,
    context_window: int,
    *,
    effective_window_ratio: float = 0.90,
    min_reserve_tokens: int = 3_000,
) -> bool:
    """API 调用前检查上下文是否快满了，避免 413 错误。

    【为什么需要】模型 API 在上下文超限时会返回 413 错误（prompt too long），
    这种错误不可恢复且浪费一次 API 调用。与其等它报错，不如在发送前就检查 token 量提前拦住。
    类似内存快满时的 OOM 预判。

    计算：effective_window = context_window * 0.9，再减去 min_reserve_tokens（为响应保留的空间）。
    如果当前 token 数 >= 这个阈值就阻止调用。

    参数:
        token_count: 当前 token 总数
        context_window: 模型上下文窗口大小
        effective_window_ratio: 有效窗口比例，默认 0.90（留 10% 余量）
        min_reserve_tokens: 最少保留 token 数，默认 3000（给模型回复用）

    返回:
        True=已达到阻塞限制，不应发送 API 请求
    """
    effective_window = int(context_window * effective_window_ratio)
    blocking_limit = max(1, effective_window - min_reserve_tokens)
    return token_count >= blocking_limit




# ══════════════════════════════════════════════════════════════
# 模型适配器调用
# ══════════════════════════════════════════════════════════════

def _model_next(
    model: ModelAdapter,
    messages: list[ChatMessage],
    *,
    on_stream_chunk: Callable[[str], None] | None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    store: Store[AppState] | None,
) -> AgentStep:
    """调用模型适配器（动态兼容不同适配器签名）。"""
    kwargs: dict[str, Any] = {"on_stream_chunk": on_stream_chunk}
    try:
        sig = inspect.signature(model.next)
        param_names = set(sig.parameters.keys())
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if has_kwargs or "on_thinking_delta" in param_names:
            kwargs["on_thinking_delta"] = on_thinking_chunk
        if has_kwargs or "store" in param_names:
            kwargs["store"] = store
    except (TypeError, ValueError):
        pass
    return model.next(messages, **kwargs)
