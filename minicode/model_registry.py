"""SmartCode 的统一模型注册表与路由系统。

支持通过单一配置系统接入多个 LLM 提供商：
- Anthropic (Claude) — 原生 Messages API
- OpenAI (GPT) — Chat Completions API
- OpenRouter — 统一的 200+ 模型网关
- 自定义 OpenAI 兼容端点 (vLLM、Ollama、LiteLLM 等)

设计灵感来源于 Hermes Agent 的提供者/模型抽象。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any



# ---------------------------------------------------------------------------
# Provider types
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    """LLM 提供商枚举。

    定义支持的模型提供商类型，包括 Anthropic、OpenAI、OpenRouter、
    自定义兼容端点和模拟模式。
    """
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"
    MOCK = "mock"


# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    """模型的静态元数据。

    描述一个模型的核心属性，包括其标识名称、所属提供商、
    上下文窗口大小、价格信息以及能力特性（流式、工具调用、视觉识别等）。
    """
    name: str                          # 标准模型 ID
    provider: Provider                 # 使用的提供商
    display_name: str = ""             # 人类可读的名称
    context_window: int = 128_000      # Token 限制
    max_output_tokens: int | None = None
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    pricing_input: float = 3.0        # 每 1M 输入 token 的美元价格
    pricing_output: float = 15.0      # 每 1M 输出 token 的美元价格

    def __post_init__(self):
        """初始化后处理。

        如果未设置 display_name，则使用 name 作为默认显示名称。
        """
        if not self.display_name:
            self.display_name = self.name


class ReasoningEffort(str, Enum):
    """推理力度枚举。

    控制模型在推理过程中投入的计算资源级别，
    从低到超高，用于在速度和深度之间做权衡。
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


@dataclass
class ModelSelectionSignal:
    """用于控制论模型选择的观测状态。

    包含任务复杂度、预算压力、延迟压力、近期失败次数等
    环境信号，供控制器做出模型推荐决策。
    """
    task_complexity: str = "moderate"
    budget_pressure: float = 0.0
    latency_pressure: float = 0.0
    recent_failures: int = 0
    requires_tools: bool = True
    requires_long_context: bool = False
    current_model: str = ""


@dataclass
class ModelSelectionDecision:
    """模型/路由推荐的控制器输出。

    包含推荐的模型、提供商、推理力度、评分以及回退方案。
    """
    model: str
    provider: Provider
    reasoning_effort: ReasoningEffort
    score: float
    reasons: list[str] = field(default_factory=list)
    fallback_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将决策结果转换为字典。

        返回:
            包含模型、提供商、推理力度、评分、原因列表和回退模型的字典。
        """
        return {
            "model": self.model,
            "provider": self.provider.value,
            "reasoning_effort": self.reasoning_effort.value,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "fallback_model": self.fallback_model,
        }


class ModelSelectionController:
    """风险/成本自适应的模型推荐控制器。

    根据任务复杂度、预算压力、延迟压力和上下文需求等信号，
    自动计算并推荐最合适的模型和推理力度。
    """
    def decide(self, signal: ModelSelectionSignal) -> ModelSelectionDecision:
        """根据输入信号做出模型选择决策。

        综合考虑候选模型的性能、成本、延迟和上下文适配度，
        计算加权评分后返回最优推荐及回退方案。

        参数:
            signal: 包含任务环境状态的观测信号。

        返回:
            包含推荐模型、提供商、推理力度和评分的决策结果。
        """
        candidates = [
            info for info in list_available_models()
            if info.supports_tools or not signal.requires_tools
        ]
        if not candidates:
            info = resolve_model_info(signal.current_model or "claude-sonnet-4-20250514")
            return ModelSelectionDecision(
                model=info.name,
                provider=info.provider,
                reasoning_effort=ReasoningEffort.MEDIUM,
                score=0.0,
                reasons=["无可用候选模型"],
            )

        reasons: list[str] = []
        complexity = signal.task_complexity.lower()
        target_power = {"simple": 0.25, "moderate": 0.55, "complex": 0.85}.get(complexity, 0.55)
        if signal.recent_failures > 0:
            target_power = min(1.0, target_power + 0.10 * signal.recent_failures)
            reasons.append(f"近期失败次数: {signal.recent_failures}")
        if signal.requires_long_context:
            target_power = min(1.0, target_power + 0.10)
            reasons.append("需要长上下文")
        if signal.budget_pressure >= 0.70:
            target_power = max(0.20, target_power - 0.25)
            reasons.append("预算压力较高")
        elif signal.budget_pressure <= 0.20 and complexity == "complex":
            target_power = min(1.0, target_power + 0.10)
            reasons.append("预算允许使用更强模型")
        if signal.latency_pressure >= 0.70:
            target_power = max(0.20, target_power - 0.15)
            reasons.append("延迟压力较高")

        scored: list[tuple[float, ModelInfo, list[str]]] = []
        for info in candidates:
            power = self._model_power(info)
            cost = self._model_cost(info)
            latency = self._latency_proxy(info)
            context_fit = 1.0 if not signal.requires_long_context else min(1.0, info.context_window / 200_000)

            score = 1.0 - abs(power - target_power)
            score -= signal.budget_pressure * cost * 0.45
            score -= signal.latency_pressure * latency * 0.30
            score += context_fit * 0.15
            if signal.current_model and info.name == resolve_model_info(signal.current_model).name:
                score += 0.05
            if signal.requires_tools and not info.supports_tools:
                score -= 1.0

            candidate_reasons = [
                f"power={power:.2f}",
                f"cost={cost:.2f}",
                f"context={info.context_window // 1000}K",
            ]
            scored.append((score, info, candidate_reasons))

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best, candidate_reasons = scored[0]
        fallback = scored[1][1].name if len(scored) > 1 else None
        effort = self._reasoning_effort(target_power, signal)
        return ModelSelectionDecision(
            model=best.name,
            provider=best.provider,
            reasoning_effort=effort,
            score=max(0.0, best_score),
            reasons=reasons + candidate_reasons,
            fallback_model=fallback,
        )

    def _model_power(self, info: ModelInfo) -> float:
        """评估模型的能力指数。

        根据模型名称中的关键词（如 opus、sonnet、haiku 等）
        判断模型的能力等级，返回 0.0~1.0 之间的数值。

        参数:
            info: 模型的元数据信息。

        返回:
            模型能力指数，越高表示能力越强。
        """
        name = info.name.lower()
        if "opus" in name or name == "o1" or "gemini-2.5-pro" in name:
            return 0.95
        if "sonnet" in name or "gpt-4o" in name or "o3" in name or "r1" in name:
            return 0.75
        if "mini" in name or "haiku" in name or "flash" in name or "deepseek-chat" in name:
            return 0.35
        return 0.55

    def _model_cost(self, info: ModelInfo) -> float:
        """计算模型的相对成本指数。

        基于模型输入和输出价格的混合平均值，
        归一化到 0.0~1.0 范围。

        参数:
            info: 模型的元数据信息。

        返回:
            相对成本指数，越高表示成本越高。
        """
        blended = (info.pricing_input + info.pricing_output) / 2
        return min(1.0, blended / 45.0)

    def _latency_proxy(self, info: ModelInfo) -> float:
        """估算模型的相对延迟指数。

        基于模型能力指数估算其延迟表现，
        能力越强的模型通常延迟越高。

        参数:
            info: 模型的元数据信息。

        返回:
            相对延迟指数，越高表示延迟越高。
        """
        power = self._model_power(info)
        return min(1.0, 0.25 + power * 0.65)

    def _reasoning_effort(
        self,
        target_power: float,
        signal: ModelSelectionSignal,
    ) -> ReasoningEffort:
        """根据目标能力和环境信号确定推荐的推理力度。

        综合考虑预算压力、延迟压力和目标能力值，
        选择合适的推理力度级别（低/中/高/超高）。

        参数:
            target_power: 目标模型能力指数（0.0~1.0）。
            signal: 包含预算和延迟压力的环境信号。

        返回:
            推荐的推理力度枚举值。
        """
        if signal.budget_pressure >= 0.80 or signal.latency_pressure >= 0.85:
            return ReasoningEffort.LOW
        if target_power >= 0.90:
            return ReasoningEffort.XHIGH
        if target_power >= 0.75:
            return ReasoningEffort.HIGH
        if target_power >= 0.45:
            return ReasoningEffort.MEDIUM
        return ReasoningEffort.LOW


# ---------------------------------------------------------------------------
# Built-in model catalog
# ---------------------------------------------------------------------------

BUILTIN_MODELS: dict[str, ModelInfo] = {}

def _register(info: ModelInfo) -> None:
    """向内置模型目录注册一个模型。

    将模型信息存入全局 BUILTIN_MODELS 字典，
    并同时注册该模型的所有常见别名。

    参数:
        info: 待注册的模型元数据。
    """
    BUILTIN_MODELS[info.name] = info
    # 同时注册常见别名
    for alias in _aliases(info.name):
        if alias not in BUILTIN_MODELS:
            BUILTIN_MODELS[alias] = info


def _aliases(name: str) -> list[str]:
    """生成模型名称的常见别名。

    例如："claude-sonnet-4-20250514" 会生成
    "claude-sonnet-4" 和 "sonnet-4" 两个别名。

    参数:
        name: 原始模型名称。

    返回:
        生成的别名列表。
    """
    result: list[str] = []
    # 例如 "claude-sonnet-4-20250514" -> "claude-sonnet-4", "sonnet-4"
    parts = name.split("-")
    if "claude" in parts:
        idx = parts.index("claude")
        family = "-".join(parts[idx:idx + 2])  # claude-sonnet-4
        if family != name:
            result.append(family)
    if "gpt" in parts:
        idx = parts.index("gpt")
        family = "-".join(parts[idx:idx + 2])  # gpt-4o
        if family != name:
            result.append(family)
    return result


# --- Anthropic models ---
_register(ModelInfo("claude-sonnet-4-20250514", Provider.ANTHROPIC,
    context_window=200_000, max_output_tokens=16_384,
    pricing_input=3.0, pricing_output=15.0))
_register(ModelInfo("claude-opus-4-20250514", Provider.ANTHROPIC,
    context_window=200_000, max_output_tokens=16_384,
    pricing_input=15.0, pricing_output=75.0))
_register(ModelInfo("claude-haiku-3-20240307", Provider.ANTHROPIC,
    context_window=100_000, max_output_tokens=4_096,
    pricing_input=0.25, pricing_output=1.25))

# --- OpenAI models ---
_register(ModelInfo("gpt-4o", Provider.OPENAI,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=2.50, pricing_output=10.0))
_register(ModelInfo("gpt-4o-mini", Provider.OPENAI,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=0.15, pricing_output=0.60))
_register(ModelInfo("gpt-4-turbo", Provider.OPENAI,
    context_window=128_000, max_output_tokens=4_096,
    pricing_input=10.0, pricing_output=30.0))
_register(ModelInfo("gpt-5.5", Provider.OPENAI,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=3.0, pricing_output=15.0))
_register(ModelInfo("gpt5.5", Provider.OPENAI,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=3.0, pricing_output=15.0))
_register(ModelInfo("o1", Provider.OPENAI,
    context_window=200_000, max_output_tokens=100_000,
    pricing_input=15.0, pricing_output=60.0, supports_tools=False))
_register(ModelInfo("o1-mini", Provider.OPENAI,
    context_window=128_000, max_output_tokens=65_536,
    pricing_input=3.0, pricing_output=12.0, supports_tools=False))
_register(ModelInfo("o3-mini", Provider.OPENAI,
    context_window=200_000, max_output_tokens=100_000,
    pricing_input=1.10, pricing_output=4.40))

# --- OpenRouter popular models ---
_register(ModelInfo("openrouter/auto", Provider.OPENROUTER,
    display_name="OpenRouter Auto", context_window=200_000,
    pricing_input=3.0, pricing_output=15.0))
_register(ModelInfo("anthropic/claude-sonnet-4", Provider.OPENROUTER,
    context_window=200_000, max_output_tokens=16_384,
    pricing_input=3.0, pricing_output=15.0))
_register(ModelInfo("anthropic/claude-opus-4", Provider.OPENROUTER,
    context_window=200_000, max_output_tokens=16_384,
    pricing_input=15.0, pricing_output=75.0))
_register(ModelInfo("openai/gpt-4o", Provider.OPENROUTER,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=2.50, pricing_output=10.0))
_register(ModelInfo("openai/gpt-4o-mini", Provider.OPENROUTER,
    context_window=128_000, max_output_tokens=16_384,
    pricing_input=0.15, pricing_output=0.60))
_register(ModelInfo("google/gemini-2.5-pro", Provider.OPENROUTER,
    context_window=1_000_000, max_output_tokens=8_192,
    pricing_input=1.25, pricing_output=10.0, supports_vision=True))
_register(ModelInfo("google/gemini-2.5-flash", Provider.OPENROUTER,
    context_window=1_000_000, max_output_tokens=8_192,
    pricing_input=0.15, pricing_output=0.60, supports_vision=True))
_register(ModelInfo("meta-llama/llama-4-maverick", Provider.OPENROUTER,
    context_window=1_000_000, max_output_tokens=8_192,
    pricing_input=0.20, pricing_output=0.60))
_register(ModelInfo("deepseek/deepseek-r1", Provider.OPENROUTER,
    context_window=128_000, max_output_tokens=8_192,
    pricing_input=0.55, pricing_output=2.19))
_register(ModelInfo("deepseek/deepseek-chat", Provider.OPENROUTER,
    context_window=128_000, max_output_tokens=8_192,
    pricing_input=0.14, pricing_output=0.28))
_register(ModelInfo("deepseek-v4-pro[1m]", Provider.ANTHROPIC,
    display_name="DeepSeek V4 Pro",
    context_window=128_000, max_output_tokens=8_192,
    pricing_input=0.10, pricing_output=0.40))
_register(ModelInfo("qwen/qwen3-235b-a22b", Provider.OPENROUTER,
    context_window=128_000, max_output_tokens=8_192,
    pricing_input=0.22, pricing_output=0.88))
_register(ModelInfo("minimax/minimax-m1", Provider.OPENROUTER,
    context_window=1_000_000, max_output_tokens=8_192,
    pricing_input=0.20, pricing_output=0.80))


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def detect_provider(model: str, runtime: dict | None = None) -> Provider:
    """根据模型名称和运行时配置自动检测应使用的提供商。

    检测优先级：
    1. OpenRouter — 如果设置了 OPENROUTER_API_KEY 或模型名以 "openrouter/" 开头
    2. OpenAI — 如果模型匹配 OpenAI 模式或设置了 OPENAI_API_KEY
    3. 自定义端点 — 如果设置了 CUSTOM_API_BASE_URL
    4. Anthropic — 默认回退

    参数:
        model: 模型名称字符串。
        runtime: 可选的运行时配置字典，可包含 openaiBaseUrl 等字段。

    返回:
        检测到的 Provider 枚举值。
    """
    model_lower = model.lower()

    # 1. OpenRouter 检测
    if os.environ.get("OPENROUTER_API_KEY") or model_lower.startswith("openrouter/"):
        return Provider.OPENROUTER
    # 同时检查提供商标记前缀，如 "anthropic/", "openai/", "google/"
    for prefix in ("anthropic/", "openai/", "google/", "meta-llama/", "deepseek/",
                   "qwen/", "minimax/", "mistralai/"):
        if model_lower.startswith(prefix):
            if os.environ.get("OPENROUTER_API_KEY"):
                return Provider.OPENROUTER
            # 也可能是使用此类命名的自定义端点
            if runtime and runtime.get("openaiBaseUrl"):
                return Provider.CUSTOM
            # 默认将带提供商前缀的模型视为 OpenRouter
            return Provider.OPENROUTER

    # 2. DeepSeek 直连 API 检测
    if model_lower.startswith("deepseek") or "deepseek" in model_lower:
        if os.environ.get("DEEPSEEK_API_KEY"):
            return Provider.CUSTOM
        # 如果在内置模型目录中已注册为 CUSTOM，则使用该类型
        if model in BUILTIN_MODELS and BUILTIN_MODELS[model].provider == Provider.CUSTOM:
            return Provider.CUSTOM

    # 3. OpenAI 检测
    openai_prefixes = ("gpt-5", "gpt-4", "gpt-3.5", "gpt5", "o1-", "o3-", "chatgpt-")
    openai_exact = {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-5.5", "gpt5.5", "o1", "o1-mini", "o3-mini"}
    if model_lower in openai_exact or any(model_lower.startswith(p) for p in openai_prefixes):
        return Provider.OPENAI
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return Provider.OPENAI

    # 4. 自定义端点检测
    custom_base = (
        os.environ.get("CUSTOM_API_BASE_URL", "")
        or (runtime or {}).get("customBaseUrl", "")
    )
    if custom_base:
        return Provider.CUSTOM

    # 5. 默认：Anthropic
    return Provider.ANTHROPIC


def resolve_model_info(model: str, provider: Provider | None = None) -> ModelInfo:
    """将模型名称解析为 ModelInfo 对象，对未知模型使用最佳回退。

    首先在内置模型目录中查找精确匹配，然后尝试不区分大小写的匹配。
    如果仍未找到，则根据提供商（或自动检测）生成一个最佳努力的 ModelInfo。

    参数:
        model: 模型名称。
        provider: 可选的提供商类型，若未指定则自动检测。

    返回:
        模型的元数据信息对象。
    """
    # 先检查内置模型目录
    if model in BUILTIN_MODELS:
        return BUILTIN_MODELS[model]

    # 尝试不区分大小写的查找
    for key, info in BUILTIN_MODELS.items():
        if key.lower() == model.lower():
            return info

    # 未知模型：生成一个最佳努力的 ModelInfo
    resolved_provider = provider or detect_provider(model)
    return ModelInfo(
        name=model,
        provider=resolved_provider,
        context_window=128_000,
        pricing_input=3.0,
        pricing_output=15.0,
    )


# ---------------------------------------------------------------------------
# Provider configuration builder
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """已解析的模型提供商配置。

    包含提供商类型、模型名称、请求基础 URL、API 密钥
    以及额外的请求头部和参数。
    """
    provider: Provider
    model: str
    base_url: str
    api_key: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_openai_compatible(self) -> bool:
        """判断该提供商是否使用 OpenAI Chat Completions API 格式。

        返回:
            如果提供商为 OpenAI、OpenRouter 或自定义端点则返回 True。
        """
        return self.provider in (Provider.OPENAI, Provider.OPENROUTER, Provider.CUSTOM)


def build_provider_config(model: str, runtime: dict | None = None) -> ProviderConfig:
    """根据模型名称和运行时配置构建提供商配置。

    该函数集中了所有提供商相关的 URL/密钥/头部逻辑，
    这些逻辑以前分散在 main.py、headless.py、gateway.py 等文件中。

    参数:
        model: 模型名称（如 "claude-sonnet-4-20250514", "openai/gpt-4o"）。
        runtime: 可选的运行时配置字典，可包含各提供商的 baseUrl、apiKey 等字段。

    返回:
        包含完整提供商配置的 ProviderConfig 对象。
    """
    runtime = runtime or {}
    provider = detect_provider(model, runtime)
    resolve_model_info(model, provider)

    if provider == Provider.OPENROUTER:
        return ProviderConfig(
            provider=Provider.OPENROUTER,
            model=model,
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api").rstrip("/"),
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            extra_headers={
                "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://github.com/minicode-py"),
                "X-Title": os.environ.get("OPENROUTER_TITLE", "SmartCode Python"),
            },
            extra_params={
                # OpenRouter 支持提供商特定的路由
                "transforms": os.environ.get("OPENROUTER_TRANSFORMS", "").split(",")
                if os.environ.get("OPENROUTER_TRANSFORMS") else None,
            },
        )

    if provider == Provider.OPENAI:
        base_url = (
            runtime.get("openaiBaseUrl", "")
            or os.environ.get("OPENAI_BASE_URL", "")
            or os.environ.get("OPENAI_API_BASE", "")
            or "https://api.openai.com"
        ).rstrip("/")
        api_key = runtime.get("openaiApiKey", "") or os.environ.get("OPENAI_API_KEY", "")
        return ProviderConfig(
            provider=Provider.OPENAI,
            model=model,
            base_url=base_url,
            api_key=api_key,
        )

    if provider == Provider.CUSTOM:
        # 优先检查 DeepSeek 特定的环境变量
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = (
            os.environ.get("CUSTOM_API_BASE_URL", "")
            or (deepseek_key and "https://api.deepseek.com/v1" or "")
            or runtime.get("customBaseUrl", "")
        ).rstrip("/")
        api_key = (
            os.environ.get("CUSTOM_API_KEY", "")
            or deepseek_key
            or os.environ.get("OPENAI_API_KEY", "")
            or runtime.get("customApiKey", "")
        )
        return ProviderConfig(
            provider=Provider.CUSTOM,
            model=model,
            base_url=base_url,
            api_key=api_key,
            extra_headers=_parse_extra_headers("CUSTOM_API_EXTRA_HEADERS"),
        )

    # 默认：Anthropic
    base_url = (
        os.environ.get("ANTHROPIC_BASE_URL", "")
        or runtime.get("baseUrl", "")
        or "https://api.anthropic.com"
    ).rstrip("/")
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or runtime.get("apiKey", "")
    )
    auth_token = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        or runtime.get("authToken", "")
    )
    # Anthropic 使用 x-api-key 头部，但为了简单起见保存在 api_key 中
    # 适配器会处理其中的差异
    return ProviderConfig(
        provider=Provider.ANTHROPIC,
        model=model,
        base_url=base_url,
        api_key=api_key or auth_token,
        extra_params={"auth_token": auth_token} if auth_token else {},
    )


def _parse_extra_headers(env_var: str) -> dict[str, str]:
    """从环境变量中解析 'Key1:Val1,Key2:Val2' 格式的自定义头部。

    参数:
        env_var: 环境变量名称，其值应为逗号分隔的键值对。

    返回:
        解析后的头部字典，若变量为空则返回空字典。
    """
    raw = os.environ.get(env_var, "")
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


# ---------------------------------------------------------------------------
# Model adapter factory (centralized replacement for scattered if/elif)
# ---------------------------------------------------------------------------

def create_model_adapter(
    model: str,
    tools: Any,
    runtime: dict | None = None,
    force_mock: bool = False,
) -> Any:
    """为指定模型创建合适的 ModelAdapter 实例。 【为什么需要】统一模型适配器工厂，替代原先分散在
    main.py、headless.py、gateway.py 等文件中的重复模型
    选择逻辑，避免多路径维护的不一致问题。

    ╔══ 完整执行流程 ══╗
    ║  第1步: 检测 provider                                 ║
    ║   ├─ force_mock=True 或 MINI_CODE_MODEL_MODE=mock     ║
    ║   │   └─ 直接降级为 MockModelAdapter                  ║
    ║   ├─ build_provider_config() → detect_provider()      ║
    ║   │      ├─ OpenRouter 检测（API Key 或模型前缀）     ║
    ║   │      ├─ OpenAI 检测（模型名匹配或 API Key 推断）  ║
    ║   │      ├─ 自定义端点检测（CUSTOM_API_BASE_URL）     ║
    ║   │      └─ 默认回退到 Anthropic                      ║
    ║  第2步: 创建对应的适配器实例                           ║
    ║   ├─ is_openai_compatible → OpenAIModelAdapter        ║
    ║   │      ├─ OpenRouter: 注入 baseUrl/apiKey + 头部    ║
    ║   │      ├─ Custom: 注入 baseUrl/apiKey + 自定义头部   ║
    ║   │      └─ OpenAI: 注入 baseUrl/apiKey               ║
    ║   ├─ Provider.ANTHROPIC → AnthropicModelAdapter       ║
    ║   │      ├─ 注入 model/baseUrl/authToken               ║
    ║   │      └─ 非标准端点自动禁用扩展思考                  ║
    ║   └─ force_mock → MockModelAdapter                    ║
    ║  第3步: 绑定工具注册表                                 ║
    ║   ├─ 将 tools 实例作为构造函数参数传入适配器           ║
    ║   └─ 适配器通过注册表管理工具调用与结果路由            ║
    ║  第4步: force_mock 降级路径                            ║
    ║   └─ 跳过 provider 检测，直接返回 MockModelAdapter()  ║
    ╚══════════════════════════════════════════════════╝

    参数:
        model: 模型名称（如 "claude-sonnet-4-20250514", "openai/gpt-4o"）。
        tools: 工具注册表实例。
        runtime: 可选的运行时配置字典。
        force_mock: 是否强制使用模拟模式（用于测试或无 API 密钥的情况）。

    返回:
        ModelAdapter 实例（AnthropicModelAdapter、OpenAIModelAdapter 或 MockModelAdapter）。
    """
    if force_mock or os.environ.get("MINI_CODE_MODEL_MODE") == "mock":
        from minicode.mock_model import MockModelAdapter
        return MockModelAdapter()

    provider_config = build_provider_config(model, runtime)

    # OpenRouter / 自定义端点 / OpenAI 都使用 OpenAI 兼容 API
    if provider_config.is_openai_compatible:
        from minicode.openai_adapter import OpenAIModelAdapter
        # 将提供商配置注入 runtime，以便适配器使用
        enriched_runtime = dict(runtime or {})
        enriched_runtime["model"] = provider_config.model
        if provider_config.provider == Provider.OPENROUTER:
            enriched_runtime["openaiBaseUrl"] = provider_config.base_url
            enriched_runtime["openaiApiKey"] = provider_config.api_key
            enriched_runtime["_openrouter_headers"] = provider_config.extra_headers
            enriched_runtime["_openrouter_params"] = provider_config.extra_params
        elif provider_config.provider == Provider.CUSTOM:
            enriched_runtime["openaiBaseUrl"] = provider_config.base_url
            enriched_runtime["openaiApiKey"] = provider_config.api_key
            enriched_runtime["_custom_headers"] = provider_config.extra_headers
        elif provider_config.provider == Provider.OPENAI:
            enriched_runtime["openaiBaseUrl"] = provider_config.base_url
            enriched_runtime["openaiApiKey"] = provider_config.api_key
        return OpenAIModelAdapter(enriched_runtime, tools)

    # Anthropic
    from minicode.anthropic_adapter import AnthropicModelAdapter
    enriched = dict(runtime or {})
    enriched["model"] = provider_config.model
    if "baseUrl" not in enriched:
        enriched["baseUrl"] = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    if "authToken" not in enriched and "apiKey" not in enriched:
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if token:
            enriched["authToken"] = token
    # 对非标准 Anthropic 端点（如 DeepSeek 等）禁用扩展思考功能
    if "api.anthropic.com" not in enriched.get("baseUrl", ""):
        enriched["disableThinking"] = True
    return AnthropicModelAdapter(enriched, tools)


# ---------------------------------------------------------------------------
# Runtime model switching
# ---------------------------------------------------------------------------

@dataclass
class ModelSwitch:
    """模型切换操作的结果。

    包含切换成功标志、新旧模型名称、提供商及描述消息。
    """
    success: bool
    old_model: str
    new_model: str
    provider: Provider
    message: str


def list_available_models(provider: Provider | None = None) -> list[ModelInfo]:
    """列出所有可用的模型，可选的按提供商筛选。

    对内置模型目录进行去重（别名指向同一个 ModelInfo），
    并按提供商和价格排序。

    参数:
        provider: 可选的提供商类型，用于筛选结果。

    返回:
        模型元数据信息列表。
    """
    models = list(BUILTIN_MODELS.values())
    # 去重（别名指向同一个 ModelInfo）
    seen: set[str] = set()
    unique: list[ModelInfo] = []
    for m in models:
        if m.name not in seen:
            seen.add(m.name)
            unique.append(m)
    if provider:
        unique = [m for m in unique if m.provider == provider]
    return sorted(unique, key=lambda m: (m.provider.value, m.pricing_input))


def format_model_list(provider: Provider | None = None) -> str:
    """将可用模型格式化为可读的表格字符串。

    按提供商分组展示各模型的名称、价格、上下文大小和工具支持情况，
    并附带使用说明。

    参数:
        provider: 可选的提供商类型，用于筛选展示的模型。

    返回:
        格式化后的模型列表字符串。
    """
    models = list_available_models(provider)
    if not models:
        return "没有可用模型。"

    lines = ["可用模型", "=" * 70, ""]

    current_provider: Provider | None = None
    for m in models:
        if m.provider != current_provider:
            current_provider = m.provider
            lines.append(f"  [{current_provider.value.upper()}]")
            lines.append(f"  {'-' * 50}")

        pricing = f"${m.pricing_input:.2f}/${m.pricing_output:.2f}"
        ctx = f"{m.context_window // 1000}K"
        tools_flag = "支持工具" if m.supports_tools else "不支持工具"
        lines.append(f"    {m.name:<45} {pricing:<14} {ctx:<8} {tools_flag}")

    lines.append("")
    lines.append("  价格：每 1M token 的输入/输出费用 | 上下文：token 限制")
    lines.append("")
    lines.append("  使用方法：")
    lines.append("    /model <名称>            — 切换到指定模型")
    lines.append("    /model anthropic         — 列出 Anthropic 模型")
    lines.append("    /model openrouter        — 列出 OpenRouter 模型")
    lines.append("    /model status            — 显示当前模型信息")
    return "\n".join(lines)


def format_model_status(model: str, runtime: dict | None = None) -> str:
    """格式化当前模型的状态信息。

    包含模型详情（提供商、基础 URL、上下文大小、定价等）、
    API 密钥状态以及控制论系统的推荐结果。

    参数:
        model: 当前模型的名称。
        runtime: 可选的运行时配置字典。

    返回:
        格式化后的模型状态字符串。
    """
    provider = detect_provider(model, runtime)
    info = resolve_model_info(model, provider)
    pconfig = build_provider_config(model, runtime)
    recommendation = ModelSelectionController().decide(
        ModelSelectionSignal(
            task_complexity="moderate",
            budget_pressure=float((runtime or {}).get("budgetPressure", 0.0) or 0.0),
            latency_pressure=float((runtime or {}).get("latencyPressure", 0.0) or 0.0),
            recent_failures=int((runtime or {}).get("recentFailures", 0) or 0),
            requires_tools=True,
            current_model=model,
        )
    )

    lines = [
        "当前模型",
        "=" * 50,
        f"  模型:     {info.display_name}",
        f"  提供商:   {info.provider.value}",
        f"  基础 URL: {pconfig.base_url}",
        f"  上下文:   {info.context_window:,} tokens",
        f"  价格:     ${info.pricing_input:.2f} / ${info.pricing_output:.2f} (输入/输出 每 1M)",
        f"  工具:     {'支持' if info.supports_tools else '不支持'}",
        f"  视觉:     {'支持' if info.supports_vision else '不支持'}",
        f"  API 密钥: {'*' * 8}{pconfig.api_key[-4:]}" if len(pconfig.api_key) > 4 else "  API 密钥: 未设置",
        "",
        "控制论系统推荐",
        f"  模型:     {recommendation.model}",
        f"  推理力度: {recommendation.reasoning_effort.value}",
        f"  评分:     {recommendation.score:.2f}",
        f"  原因:     {', '.join(recommendation.reasons[:4])}",
    ]
    return "\n".join(lines)
