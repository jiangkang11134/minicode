"""API 重试与指数退避模块，为模型适配器提供容错能力。

处理瞬时故障（429、5xx）并自动重试，支持指数退避、Retry-After
响应头解析，以及基于语义的错误分类与自适应退避策略。
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 最大重试次数
MAX_RETRIES = 3

# 基础退避时长（秒）
BASE_BACKOFF = 1.0

# 最大退避上限（60 秒）
MAX_BACKOFF = 60.0

# 抖动因子（0.5 表示 +/-50% 随机化）
JITTER_FACTOR = 0.5

# 可重试的 HTTP 状态码集合
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# 语义错误分类
# ---------------------------------------------------------------------------

class ErrorCategory(str, Enum):
    """API 错误的语义分类枚举。

    每个类别具有不同的重试特征：
    - RATE_LIMIT: 服务器过载，积极退避
    - SERVER_ERROR: 瞬时服务器问题，适中退避
    - NETWORK_ERROR: 连接问题，快速重试
    - AUTH_ERROR: 凭据问题，不重试（通常为永久性）
    - INPUT_ERROR: 错误请求，不重试（重试会再次失败）
    - OVERLOAD: 模型/服务器过载，更长退避
    - UNKNOWN: 未分类，使用默认退避
    """
    RATE_LIMIT = "rate_limit"       # 429
    SERVER_ERROR = "server_error"   # 500, 502, 503, 504
    NETWORK_ERROR = "network_error" # Connection refused, timeout, DNS
    AUTH_ERROR = "auth_error"       # 401, 403
    INPUT_ERROR = "input_error"     # 400, 422
    OVERLOAD = "overload"           # 529, Anthropic-specific
    UNKNOWN = "unknown"


# 类别特定的退避倍率
_CATEGORY_BACKOFF: dict[ErrorCategory, float] = {
    ErrorCategory.RATE_LIMIT: 2.0,    # 速率限制加倍基础退避
    ErrorCategory.SERVER_ERROR: 1.0,  # 标准指数退避
    ErrorCategory.NETWORK_ERROR: 0.5, # 网络问题快速重试
    ErrorCategory.OVERLOAD: 3.0,      # 过载时激进退避
    ErrorCategory.UNKNOWN: 1.0,       # 默认
}

# 类别特定的最大重试次数覆盖
_CATEGORY_MAX_RETRIES: dict[ErrorCategory, int | None] = {
    ErrorCategory.NETWORK_ERROR: 5,   # 网络瞬时问题允许更多重试
    ErrorCategory.OVERLOAD: 5,        # 过载时更多重试
    ErrorCategory.RATE_LIMIT: 4,      # 速率限制略多几次
}

# 错误消息中表示过载的模式
_OVERLOAD_PATTERNS = re.compile(
    r"(?:overloaded|overload|capacity|too many requests|"
    r"temporarily unavailable|please try again later|"
    r"service is currently unavailable|api is temporarily|"
    r"capacity exceeded|high demand)",
    re.IGNORECASE,
)

# 表示网络级错误的模式
_NETWORK_ERROR_PATTERNS = re.compile(
    r"(?:connection\s*(?:refused|reset|timeout|aborted)|"
    r"timed?\s*out|dns\s*resolution|name\s*resolution|"
    r"network\s*(?:error|unreachable|down)|"
    r"socket\s*(?:error|closed)|eof\s*occurred|"
    r"ssl\s*error|certificate\s*verify|handshake\s*failed)",
    re.IGNORECASE,
)


def classify_error(error: Exception) -> ErrorCategory:
    """将异常分类为语义类别，用于自适应重试决策。

    同时使用 HTTP 状态码和错误消息模式来确定错误类别，
    从而实现更智能的重试策略。

    参数:
        error: 待分类的异常对象

    返回:
        对应的 ErrorCategory 枚举值
    """
    # 优先检查 HTTP 状态码（最可靠）
    status_code = getattr(error, "status_code", None)

    if status_code is not None:
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT
        if status_code == 529:
            return ErrorCategory.OVERLOAD
        if status_code in (401, 403):
            return ErrorCategory.AUTH_ERROR
        if status_code in (400, 422, 404, 405, 409, 413, 415):
            return ErrorCategory.INPUT_ERROR
        if status_code in (500, 502, 503, 504):
            # 在消息中检查过载关键词
            msg = str(error).lower()
            if _OVERLOAD_PATTERNS.search(msg):
                return ErrorCategory.OVERLOAD
            return ErrorCategory.SERVER_ERROR

    # 对非 HTTP 错误，检查消息模式
    msg = str(error)
    if _NETWORK_ERROR_PATTERNS.search(msg):
        return ErrorCategory.NETWORK_ERROR
    if _OVERLOAD_PATTERNS.search(msg):
        return ErrorCategory.OVERLOAD

    # 检查常见的异常类型
    error_type_name = type(error).__name__.lower()
    if any(name in error_type_name for name in ("timeout", "connection", "socket")):
        return ErrorCategory.NETWORK_ERROR

    return ErrorCategory.UNKNOWN


def is_retryable(category: ErrorCategory) -> bool:
    """判断给定的错误类别是否可重试。

    参数:
        category: 错误类别

    返回:
        可重试返回 True，否则返回 False
    """
    return category in (
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.SERVER_ERROR,
        ErrorCategory.NETWORK_ERROR,
        ErrorCategory.OVERLOAD,
        ErrorCategory.UNKNOWN,
    )


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------

class APIRetryExhaustedError(Exception):
    """所有重试尝试均已用尽时抛出的异常。

    记录最终尝试次数、最后一次错误以及错误类别，
    便于调用方进行后续处理或日志记录。
    """

    def __init__(
        self,
        message: str,
        attempts: int,
        last_error: Exception | None = None,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error
        self.category = category


# ---------------------------------------------------------------------------
# 退避计算
# ---------------------------------------------------------------------------

def calculate_backoff(
    attempt: int,
    retry_after: float | None = None,
    base: float = BASE_BACKOFF,
    max_wait: float = MAX_BACKOFF,
    jitter: float = JITTER_FACTOR,
    category: ErrorCategory | None = None,
) -> float:
    """计算带指数退避和抖动的等待时长。

    支持基于错误类别的自适应退避：
    - RATE_LIMIT: 2 倍基础时长，优先尊重 Retry-After 头部
    - OVERLOAD: 3 倍基础时长，更长等待
    - NETWORK_ERROR: 0.5 倍基础时长，快速重试
    - SERVER_ERROR: 标准指数退避
    - UNKNOWN: 标准指数退避

    参数:
        attempt: 当前重试次数（从 0 开始）
        retry_after: Retry-After 响应头的秒数值（如有）
        base: 基础退避时长（秒）
        max_wait: 最大退避上限（秒）
        jitter: 抖动因子，用于随机化
        category: 错误类别，用于自适应退避

    返回:
        下次重试前应等待的秒数
    """
    # 应用类别特定的倍率到基础时长
    effective_base = base
    if category is not None:
        effective_base = base * _CATEGORY_BACKOFF.get(category, 1.0)

    if retry_after is not None and retry_after > 0:
        # 尊重 Retry-After 头部，但应用类别的最小值保证
        min_wait = effective_base * (2 ** min(attempt, 2))
        return max(min(retry_after, max_wait), min_wait)

    # 指数退避：effective_base * 2^attempt
    backoff = effective_base * (2 ** attempt)

    # 添加抖动：backoff * (1 +/- jitter)
    jitter_range = backoff * jitter
    backoff = backoff + random.uniform(-jitter_range, jitter_range)

    # 确保正值并上限封顶
    return max(0.1, min(backoff, max_wait))


# ---------------------------------------------------------------------------
# 重试装饰器
# ---------------------------------------------------------------------------

@dataclass
class RetryState:
    """跟踪重试状态，用于监控和回调。

    记录尝试次数、总等待时间、历史错误类别以及是否最终成功。
    """
    attempts: int = 0
    max_attempts: int = MAX_RETRIES
    total_wait_time: float = 0.0
    last_error: str | None = None
    last_category: ErrorCategory = ErrorCategory.UNKNOWN
    category_history: list[ErrorCategory] = field(default_factory=list)
    succeeded: bool = False


def retry_with_backoff(
    func: Callable,
    *args: Any,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    retryable_errors: set[int] = RETRYABLE_STATUS,
    on_retry: Callable[[RetryState], None] | None = None,
    **kwargs: Any,
) -> Any:
    """执行函数并自动进行带指数退避的重试。

    使用语义错误分类进行自适应重试：
    - 速率限制（429）: 积极退避，尊重 Retry-After
    - 服务器错误（5xx）: 标准指数退避
    - 网络错误: 快速重试，允许更多尝试次数
    - 认证/输入错误: 不重试（永久性）
    - 过载: 最长退避，最多重试次数

    参数:
        func: 要执行的函数
        *args: func 的位置参数
        max_retries: 最大重试次数
        base_backoff: 基础退避时长（秒）
        max_backoff: 最大退避上限（秒）
        retryable_errors: 可重试的 HTTP 状态码集合
        on_retry: 每次重试时触发的可选回调，接收 RetryState 对象
        **kwargs: func 的关键字参数

    返回:
        函数调用成功后的结果

    抛出:
        APIRetryExhaustedError: 所有重试尝试均用尽时抛出
    """
    state = RetryState(max_attempts=max_retries)

    for attempt in range(max_retries + 1):
        try:
            result = func(*args, **kwargs)
            state.succeeded = True
            state.attempts = attempt + 1
            return result

        except HTTPError as e:
            # 对错误进行语义分类
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)

            # 检查错误类别是否可重试
            if not is_retryable(category):
                raise

            # 检查类别特定的最大重试次数
            cat_max = _CATEGORY_MAX_RETRIES.get(category)
            effective_max = cat_max if cat_max is not None else max_retries

            state.attempts = attempt + 1
            state.last_error = str(e)

            if attempt >= effective_max:
                raise APIRetryExhaustedError(
                    f"API call failed after {attempt + 1} attempts "
                    f"(category: {category.value}): {e}",
                    attempts=attempt + 1,
                    last_error=e,
                    category=category,
                )

            # 提取 Retry-After 头部（如有）
            retry_after = getattr(e, "retry_after", None)

            # 基于错误类别计算自适应退避
            wait_time = calculate_backoff(
                attempt,
                retry_after=retry_after,
                base=base_backoff,
                max_wait=max_backoff,
                category=category,
            )

            state.total_wait_time += wait_time

            # 通知重试回调
            if on_retry:
                on_retry(state)

            # 等待后重试
            time.sleep(wait_time)

        except Exception as e:
            # 对非 HTTP 错误也进行语义分类
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)

            if is_retryable(category) and attempt < max_retries:
                state.attempts = attempt + 1
                state.last_error = str(e)

                wait_time = calculate_backoff(
                    attempt,
                    base=base_backoff,
                    max_wait=max_backoff,
                    category=category,
                )
                state.total_wait_time += wait_time

                if on_retry:
                    on_retry(state)

                time.sleep(wait_time)
                continue

            # 非可重试的非 HTTP 错误，直接抛出
            raise


# ---------------------------------------------------------------------------
# HTTP 错误封装
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """携带状态码和可选 Retry-After 响应头的 HTTP 错误异常。

    封装了 HTTP 响应的错误信息、状态码、重试等待时间以及原始响应对象，
    供 retry_with_backoff 等函数自动解析和决策。
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        retry_after: float | None = None,
        response: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.response = response


def raise_for_status(response: Any, error_class: type[HTTPError] = HTTPError) -> None:
    """检查 HTTP 响应状态码，必要时抛出错误。

    这是一个通用封装函数，可适配 urllib、requests、httpx 等多种
    HTTP 库的响应对象。自动提取 Retry-After 头部和错误消息。

    参数:
        response: HTTP 响应对象（支持 urllib、requests、httpx 等）
        error_class: 用于抛出的异常类，默认为 HTTPError
    """
    status_code = getattr(response, "status", None) or getattr(response, "status_code", None)

    if status_code is None:
        return

    # 提取 Retry-After 头部
    retry_after = None
    if hasattr(response, "getheader"):
        retry_after_str = response.getheader("Retry-After")
    elif hasattr(response, "headers"):
        retry_after_str = response.headers.get("Retry-After")
    else:
        retry_after_str = None

    if retry_after_str:
        try:
            retry_after = float(retry_after_str)
        except (ValueError, TypeError):
            pass

    # 检查是否为错误状态码
    if status_code >= 400:
        # 尝试从响应体中获取错误消息
        error_message = str(status_code)
        if hasattr(response, "read"):
            try:
                body = response.read().decode("utf-8", errors="replace")
                error_message = f"{status_code}: {body[:200]}"
            except Exception:
                pass
        elif hasattr(response, "text"):
            error_message = f"{status_code}: {response.text[:200]}"

        raise error_class(error_message, status_code, retry_after, response)


# ---------------------------------------------------------------------------
# 异步兼容封装（预留）
# ---------------------------------------------------------------------------

async def retry_with_backoff_async(
    func: Callable,
    *args: Any,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    retryable_errors: set[int] = RETRYABLE_STATUS,
    on_retry: Callable[[RetryState], None] | None = None,
    **kwargs: Any,
) -> Any:
    """retry_with_backoff 的异步版本。

    使用 asyncio.sleep 替代 time.sleep 实现非阻塞等待。
    支持与同步版本相同的语义错误分类和自适应退避策略。

    参数:
        func: 要执行的函数（async 或 sync 均可）
        *args: func 的位置参数
        max_retries: 最大重试次数
        base_backoff: 基础退避时长（秒）
        max_backoff: 最大退避上限（秒）
        retryable_errors: 可重试的 HTTP 状态码集合
        on_retry: 每次重试时触发的可选回调
        **kwargs: func 的关键字参数

    返回:
        函数调用成功后的结果

    抛出:
        APIRetryExhaustedError: 所有重试尝试均用尽时抛出
    """
    import asyncio

    state = RetryState(max_attempts=max_retries)

    for attempt in range(max_retries + 1):
        try:
            # 对于 async 函数使用 await，sync 函数直接调用
            if hasattr(func, "__await__"):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            state.succeeded = True
            state.attempts = attempt + 1
            return result

        except HTTPError as e:
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)

            if not is_retryable(category):
                raise

            cat_max = _CATEGORY_MAX_RETRIES.get(category)
            effective_max = cat_max if cat_max is not None else max_retries

            state.attempts = attempt + 1
            state.last_error = str(e)

            if attempt >= effective_max:
                raise APIRetryExhaustedError(
                    f"API call failed after {attempt + 1} attempts "
                    f"(category: {category.value}): {e}",
                    attempts=attempt + 1,
                    last_error=e,
                    category=category,
                )

            retry_after = getattr(e, "retry_after", None)
            wait_time = calculate_backoff(
                attempt,
                retry_after=retry_after,
                base=base_backoff,
                max_wait=max_backoff,
                category=category,
            )

            state.total_wait_time += wait_time

            if on_retry:
                on_retry(state)

            await asyncio.sleep(wait_time)

        except Exception as e:
            category = classify_error(e)
            state.last_category = category
            state.category_history.append(category)

            if is_retryable(category) and attempt < max_retries:
                state.attempts = attempt + 1
                state.last_error = str(e)

                wait_time = calculate_backoff(
                    attempt,
                    base=base_backoff,
                    max_wait=max_backoff,
                    category=category,
                )
                state.total_wait_time += wait_time

                if on_retry:
                    on_retry(state)

                await asyncio.sleep(wait_time)
                continue

            raise


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def is_retryable_error(error: Exception, retryable_codes: set[int] = RETRYABLE_STATUS) -> bool:
    """基于语义分类判断一个异常是否可重试。

    对于 HTTPError 使用 classify_error 分类，对于非 HTTP 异常
    也适用相同的分类逻辑。

    参数:
        error: 待检查的异常
        retryable_codes: 可重试的状态码集合（当前保留用于兼容性）

    返回:
        可重试返回 True，否则返回 False
    """
    if isinstance(error, HTTPError):
        category = classify_error(error)
        return is_retryable(category)
    # 也通过分类检查非 HTTP 错误
    return is_retryable(classify_error(error))


def format_retry_state(state: RetryState) -> str:
    """将重试状态格式化为可读字符串，用于日志或展示。

    参数:
        state: 重试状态对象

    返回:
        格式化后的状态描述字符串
    """
    if state.succeeded:
        return f"Succeeded on attempt {state.attempts}"

    cat_summary = ""
    if state.category_history:
        from collections import Counter
        counts = Counter(c.value for c in state.category_history)
        cat_summary = f" ({', '.join(f'{k}x{v}' for k, v in counts.most_common(3))})"

    return (
        f"Failed after {state.attempts} attempts{cat_summary}, "
        f"waited {state.total_wait_time:.1f}s total"
    )
