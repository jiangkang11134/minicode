"""API 调用的成本和用量追踪。

跟踪会话中的 token 使用量、API 成本和代码变更。
灵感来自 Claude Code 的 cost-tracker.ts 实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

# Precomputed Decimal constants for performance
_DECIMAL_1M = Decimal("1000000")
_DECIMAL_2 = Decimal("2")

# ---------------------------------------------------------------------------
# Pricing (approximate, per 1M tokens)
# ---------------------------------------------------------------------------

MODEL_PRICING = {
    # Anthropic models (USD per 1M tokens)
    "claude-sonnet-4-20250514": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-opus-4-20250514": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "claude-haiku-3-20240307": {
        "input": 0.25,
        "output": 1.25,
        "cache_read": 0.03,
        "cache_write": 0.30,
    },
    # OpenAI models
    "gpt-4o": {
        "input": 2.50,
        "output": 10.0,
        "cache_read": 1.25,
        "cache_write": 2.50,
    },
    "gpt-4o-mini": {
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.08,
        "cache_write": 0.15,
    },
    "gpt-4-turbo": {
        "input": 10.0,
        "output": 30.0,
        "cache_read": 5.0,
        "cache_write": 10.0,
    },
    "o1": {
        "input": 15.0,
        "output": 60.0,
        "cache_read": 7.50,
        "cache_write": 15.0,
    },
    "o1-mini": {
        "input": 3.0,
        "output": 12.0,
        "cache_read": 1.50,
        "cache_write": 3.0,
    },
    "o3-mini": {
        "input": 1.10,
        "output": 4.40,
        "cache_read": 0.55,
        "cache_write": 1.10,
    },
    # OpenRouter models (pricing via OpenRouter, approximate)
    "openrouter/auto": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "anthropic/claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "anthropic/claude-opus-4": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "openai/gpt-4o": {
        "input": 2.50,
        "output": 10.0,
        "cache_read": 1.25,
        "cache_write": 2.50,
    },
    "openai/gpt-4o-mini": {
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.08,
        "cache_write": 0.15,
    },
    "google/gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.0,
        "cache_read": 0.63,
        "cache_write": 1.25,
    },
    "google/gemini-2.5-flash": {
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.08,
        "cache_write": 0.15,
    },
    "meta-llama/llama-4-maverick": {
        "input": 0.20,
        "output": 0.60,
        "cache_read": 0.10,
        "cache_write": 0.20,
    },
    "deepseek/deepseek-r1": {
        "input": 0.55,
        "output": 2.19,
        "cache_read": 0.14,
        "cache_write": 0.55,
    },
    "deepseek/deepseek-chat": {
        "input": 0.14,
        "output": 0.28,
        "cache_read": 0.07,
        "cache_write": 0.14,
    },
    "qwen/qwen3-235b-a22b": {
        "input": 0.22,
        "output": 0.88,
        "cache_read": 0.11,
        "cache_write": 0.22,
    },
    "minimax/minimax-m1": {
        "input": 0.20,
        "output": 0.80,
        "cache_read": 0.10,
        "cache_write": 0.20,
    },
    # Default fallback
    "default": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
}


# ---------------------------------------------------------------------------
# Cost calculation (standalone function for use outside CostTracker)
# ---------------------------------------------------------------------------

def calculate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """计算单次 API 调用的成本。

    根据模型名称查询 MODEL_PRICING 中的定价，然后根据 input/output/cache
    各类 token 数量按比例计算总成本。

    参数:
        model: 模型名称。
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        cache_read_tokens: 缓存读取 token 数。
        cache_creation_tokens: 缓存写入 token 数。

    返回:
        以 USD 计的成本。
    """
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    return (
        (input_tokens / _DECIMAL_1M) * pricing["input"]
        + (output_tokens / _DECIMAL_1M) * pricing["output"]
        + (cache_read_tokens / _DECIMAL_1M) * pricing["cache_read"]
        + (cache_creation_tokens / _DECIMAL_1M) * pricing["cache_write"]
    )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ModelUsage:
    """单个模型的使用统计。

    记录指定模型在会话中的 token 使用量、调用次数、错误次数和总耗时。
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    call_count: int = 0
    error_count: int = 0
    total_duration_ms: int = 0

    @property
    def total_tokens(self) -> int:
        """返回该模型所有 token 类型的总和。 """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def avg_duration_ms(self) -> float:
        """返回该模型每次 API 调用的平均耗时（毫秒）。 """
        if self.call_count == 0:
            return 0.0
        return self.total_duration_ms / self.call_count


@dataclass
class CostTracker:
    """追踪会话中的 API 成本和用量。

    记录全局汇总数据（总成本、总耗时、代码变更行数）以及
    按模型拆分的详细使用情况。灵感来自 Claude Code 的 cost-tracker.ts。
    """
    # Global totals
    total_cost_usd: float = 0.0
    total_api_duration_ms: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0
    total_lines_modified: int = 0

    # Per-model usage
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)

    # Session info
    session_start: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)

    def add_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """记录一次 API 使用。

        根据模型定价查询单价，累加各类 token 的成本并更新全局和模型级统计。

        参数:
            model: 模型名称。
            input_tokens: 输入 token 数。
            output_tokens: 输出 token 数。
            duration_ms: API 调用耗时（毫秒）。
            cache_read_tokens: 缓存读取 token 数。
            cache_write_tokens: 缓存写入 token 数。

        返回:
            本次调用的计算成本（USD）。
        """
        # Get pricing
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])

        # Calculate cost
        cost = (
            (input_tokens / 1_000_000) * pricing["input"]
            + (output_tokens / 1_000_000) * pricing["output"]
            + (cache_read_tokens / 1_000_000) * pricing["cache_read"]
            + (cache_write_tokens / 1_000_000) * pricing["cache_write"]
        )

        # Update model usage
        if model not in self.model_usage:
            self.model_usage[model] = ModelUsage()

        usage = self.model_usage[model]
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.cache_read_tokens += cache_read_tokens
        usage.cache_write_tokens += cache_write_tokens
        usage.cost_usd += cost
        usage.call_count += 1
        usage.total_duration_ms += duration_ms

        # Update totals
        self.total_cost_usd += cost
        self.total_api_duration_ms += duration_ms
        self.last_updated = time.time()

        return cost

    def record_error(self, model: str) -> None:
        """记录一次 API 错误。

        参数:
            model: 发生错误的模型名称。
        """
        if model not in self.model_usage:
            self.model_usage[model] = ModelUsage()

        self.model_usage[model].error_count += 1
        self.last_updated = time.time()

    def record_code_changes(
        self,
        lines_added: int = 0,
        lines_removed: int = 0,
    ) -> None:
        """记录代码变更行数。

        参数:
            lines_added: 新增行数。
            lines_removed: 删除行数。
        """
        self.total_lines_added += lines_added
        self.total_lines_removed += lines_removed
        self.total_lines_modified += lines_added + lines_removed
        self.last_updated = time.time()

    def get_model_usage(self, model: str) -> ModelUsage:
        """获取指定模型的使用统计。

        参数:
            model: 模型名称。

        返回:
            该模型的 ModelUsage 实例，如无记录则返回空的默认值。
        """
        return self.model_usage.get(model, ModelUsage())

    def get_total_tokens(self) -> int:
        """获取所有模型的总 token 使用量。 """
        return sum(u.total_tokens for u in self.model_usage.values())

    def get_total_calls(self) -> int:
        """获取所有模型的总 API 调用次数。 """
        return sum(u.call_count for u in self.model_usage.values())

    def get_total_errors(self) -> int:
        """获取所有模型的总 API 错误次数。 """
        return sum(u.error_count for u in self.model_usage.values())

    # -----------------------------------------------------------------------
    # Formatting
    # -----------------------------------------------------------------------

    def format_cost_report(self, detailed: bool = False) -> str:
        """格式化成本和用量报告。

        参数:
            detailed: 是否包含按模型的拆分详情。

        返回:
            格式化的报告字符串。
        """
        lines = [
            "Cost & Usage Report",
            "=" * 60,
            "",
            "Summary:",
            f"  Total cost: ${self.total_cost_usd:.4f}",
            f"  Total API calls: {self.get_total_calls()}",
            f"  Total API errors: {self.get_total_errors()}",
            f"  Total tokens: {self.get_total_tokens():,}",
            f"  Total API duration: {self.total_api_duration_ms / 1000:.1f}s",
            "",
            "Code Changes:",
            f"  Lines added: {self.total_lines_added:,}",
            f"  Lines removed: {self.total_lines_removed:,}",
            f"  Total modified: {self.total_lines_modified:,}",
        ]

        if detailed and self.model_usage:
            lines.extend([
                "",
                "Per-Model Breakdown:",
                "-" * 60,
            ])

            for model, usage in self.model_usage.items():
                lines.extend([
                    "",
                    f"  {model}:",
                    f"    Cost: ${usage.cost_usd:.4f}",
                    f"    Calls: {usage.call_count}",
                    f"    Errors: {usage.error_count}",
                    f"    Tokens: {usage.total_tokens:,}",
                    f"      Input: {usage.input_tokens:,}",
                    f"      Output: {usage.output_tokens:,}",
                    f"      Cache read: {usage.cache_read_tokens:,}",
                    f"      Cache write: {usage.cache_write_tokens:,}",
                    f"    Avg duration: {usage.avg_duration_ms:.0f}ms",
                ])

        # Session duration
        session_duration = time.time() - self.session_start
        lines.extend([
            "",
            "-" * 60,
            f"Session duration: {session_duration / 60:.1f} minutes",
            f"Cost per minute: ${self.total_cost_usd / max(1, session_duration / 60):.4f}",
        ])

        return "\n".join(lines)

    def format_short_summary(self) -> str:
        """格式化为状态栏使用的简短摘要。

        返回:
            简短的成本摘要字符串。
        """
        if self.total_cost_usd == 0:
            return "Cost: $0.0000"

        return (
            f"Cost: ${self.total_cost_usd:.4f} | "
            f"Tokens: {self.get_total_tokens():,} | "
            f"Calls: {self.get_total_calls()}"
        )
