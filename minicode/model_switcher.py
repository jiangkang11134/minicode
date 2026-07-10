"""Model Switcher —— 运行时动态切换模型。

管理在会话期间切换 LLM 模型的生命周期，包括适配器重建、上下文保持
和状态更新。

处理流程：
  用户请求切换模型 -> ModelSwitcher.switch_to() -> 创建新适配器 ->
  更新运行时状态 -> 返回 SwitchResult
  若切换失败 -> switch_to_fallback() -> 遍历候选模型重试
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from minicode.config import configured_model_fallbacks, default_model_fallbacks
from minicode.logging_config import get_logger
from minicode.model_registry import (
    BUILTIN_MODELS,
    ModelSelectionController,
    ModelSelectionSignal,
    build_provider_config,
    create_model_adapter,
    list_available_models,
    resolve_model_info,
)

logger = get_logger("model_switcher")


_ANTHROPIC_RUNTIME_FAMILY_DEFAULTS = {
    "claude-sonnet-4-20250514": "anthropicDefaultSonnetModel",
    "claude-opus-4-20250514": "anthropicDefaultOpusModel",
    "claude-haiku-3-20240307": "anthropicDefaultHaikuModel",
}


@dataclass
class SwitchResult:
    """模型切换操作的结果，记录切换前后的模型和提供方信息以及错误详情。"""
    success: bool
    old_model: str
    new_model: str
    old_provider: str
    new_provider: str
    reason: str
    adapter: Any | None = None
    errors: list[str] = field(default_factory=list)

    def to_log(self) -> str:
        """将切换结果格式化为日志字符串。

        返回:
            格式化的状态信息字符串，包含切换状态、模型变更和错误信息。
        """
        status = "OK" if self.success else "FAILED"
        msg = f"Switch [{status}]: {self.old_model} ({self.old_provider}) -> {self.new_model} ({self.new_provider})"
        if self.errors:
            msg += f" Errors: {'; '.join(self.errors)}"
        return msg


class ModelSwitcher:
    """运行时模型切换管理器，负责适配器的创建、销毁和状态同步。

    维护当前模型、运行时配置、工具列表和切换历史，支持故障降级
    到候选模型列表中的下一个可用模型。
    """
    def __init__(
        self,
        current_model: str,
        current_runtime: dict,
        current_tools: Any,
        available_models: dict[str, Any] | None = None,
    ):
        """初始化 ModelSwitcher。

        参数:
            current_model: 当前使用的模型名称。
            current_runtime: 当前运行时配置字典。
            current_tools: 工具列表对象。
            available_models: 可用模型字典，不传则使用 BUILTIN_MODELS。
        """
        self._current_model = current_model
        self._runtime = current_runtime
        self._tools = current_tools
        self._available_models = available_models or BUILTIN_MODELS
        inferred_default_model = ""
        try:
            if (
                detect_provider_name(current_model) == "anthropic"
                and current_model
                and not current_model.startswith("claude-")
            ):
                inferred_default_model = current_model
        except Exception:
            inferred_default_model = ""
        self._runtime_family_defaults = {
            key: str((current_runtime or {}).get(key, "") or inferred_default_model).strip()
            for key in _ANTHROPIC_RUNTIME_FAMILY_DEFAULTS.values()
        }
        self._switch_history: list[SwitchResult] = []
        self._current_adapter: Any = None
        self._failed_models: set[str] = set()

    @property
    def current_model(self) -> str:
        """当前活跃的模型名称。"""
        return self._current_model

    @property
    def switch_count(self) -> int:
        """累计切换模型的次数。"""
        return len(self._switch_history)

    def sync_current_model(self, model_name: str | None, adapter: Any | None = None) -> None:
        """将切换器的内部状态与活跃的运行时模型同步。

        当外部代码直接更改了运行时模型时调用此方法，以保持切换器的
        状态一致性。同时会尝试播种运行时家族默认值。

        参数:
            model_name: 当前模型名称，若为 None 或空字符串则不更新。
            adapter: 当前模型适配器实例，若不传则不更新。
        """
        normalized = (model_name or "").strip()
        if normalized:
            self._current_model = normalized
            self._runtime["model"] = normalized
            self._maybe_seed_runtime_family_defaults(normalized)
        if adapter is not None:
            self._current_adapter = adapter

    def record_runtime_failure(self, model_name: str | None = None) -> None:
        """将某个模型标记为当前运行时的失败状态，避免在回退窗口中再次尝试。

        参数:
            model_name: 失败的模型名称，不传则标记当前模型。
        """
        normalized = (model_name or self._current_model or "").strip()
        if normalized:
            self._failed_models.add(normalized)

    def clear_runtime_failures(self) -> None:
        """清除所有运行时失败记录，通常在模型成功响应后调用。"""
        self._failed_models.clear()

    def switch_to(self, target_model: str, reason: str = "user_request") -> SwitchResult:
        """切换到指定的目标模型。

        创建新的模型适配器，更新当前模型和运行时状态，并记录切换历史。
        若目标模型与当前模型相同则返回错误。

        参数:
            target_model: 目标模型名称。
            reason: 切换原因描述，默认为 "user_request"。

        返回:
            SwitchResult 对象，包含切换成功/失败状态及详情。
        """
        if not target_model:
            return self.switch_to_fallback(reason=reason)

        if target_model == self._current_model:
            return SwitchResult(
                success=False,
                old_model=self._current_model,
                new_model=target_model,
                old_provider=detect_provider_name(self._current_model),
                new_provider=detect_provider_name(target_model),
                reason=reason,
                errors=["Target model is already active"],
            )

        old_model = self._current_model
        old_provider = detect_provider_name(old_model)
        new_provider = detect_provider_name(target_model)

        try:
            new_adapter = create_model_adapter(
                model=target_model,
                tools=self._tools,
                runtime=self._runtime,
            )

            self._current_model = target_model
            self._current_adapter = new_adapter
            self._runtime["model"] = target_model

            result = SwitchResult(
                success=True,
                old_model=old_model,
                new_model=target_model,
                old_provider=old_provider,
                new_provider=new_provider,
                reason=reason,
                adapter=new_adapter,
            )

            self._switch_history.append(result)
            logger.info(result.to_log())
            return result

        except Exception as e:
            result = SwitchResult(
                success=False,
                old_model=old_model,
                new_model=target_model,
                old_provider=old_provider,
                new_provider=new_provider,
                reason=reason,
                errors=[str(e)],
            )
            self._switch_history.append(result)
            logger.error("Model switch failed: %s", result.to_log())
            return result

    def switch_to_fallback(self, reason: str = "fallback") -> SwitchResult:
        """切换到第一个可用的降级候选模型。

        从候选列表中依次尝试切换，返回第一个成功的 SwitchResult。
        如果所有候选都失败，返回一个指示失败的 SwitchResult。

        参数:
            reason: 降级原因描述，默认为 "fallback"。

        返回:
            SwitchResult 对象，标记最终切换成功或失败。
        """
        old_model = self._current_model
        old_provider = detect_provider_name(old_model)
        errors: list[str] = []
        candidates = self._fallback_candidates()

        logger.debug(
            "Fallback resolution: current=%s failed=%s snapshot_defaults=%s live_defaults=%s candidates=%s",
            self._current_model,
            sorted(self._failed_models),
            self._runtime_family_defaults,
            {
                key: str(self._runtime.get(key, "") or "").strip()
                for key in _ANTHROPIC_RUNTIME_FAMILY_DEFAULTS.values()
            },
            candidates,
        )

        for candidate in candidates:
            result = self.switch_to(candidate, reason=reason)
            if result.success:
                return result
            if result.errors:
                errors.extend(result.errors)

        result = SwitchResult(
            success=False,
            old_model=old_model,
            new_model="",
            old_provider=old_provider,
            new_provider="unknown",
            reason=reason,
            errors=errors or ["No viable fallback models were available"],
        )
        self._switch_history.append(result)
        logger.error("Model fallback failed: %s", result.to_log())
        return result

    def _fallback_candidates(self) -> list[str]:
        """构建模型降级的候选列表。

        候选来源按优先级排序：
        1. 运行时配置的降级模型
        2. 默认模型降级列表
        3. 环境变量（MINI_CODE_MODEL_FALLBACKS 或 <PROVIDER>_MODEL_FALLBACKS）
        4. 同 provider 下的所有可用模型
        5. ModelSelectionController 的决策结果

        返回:
            排序后的候选模型名称列表（去重、排除当前模型和已失败模型）。
        """
        current_provider = detect_provider_name(self._current_model)
        provider_env = f"{current_provider.upper()}_MODEL_FALLBACKS"
        explicit_candidates: list[str] = []
        candidates: list[str] = []
        bounded_family_fallbacks = False

        runtime_candidates = configured_model_fallbacks(self._runtime, current_provider)
        explicit_candidates.extend(runtime_candidates)
        candidates.extend(runtime_candidates)
        candidates.extend(
            default_model_fallbacks(
                self._runtime,
                current_provider,
                current_model=self._current_model,
            )
        )

        for env_var in ("MINI_CODE_MODEL_FALLBACKS", provider_env):
            parsed = _parse_model_list(os.environ.get(env_var, ""))
            explicit_candidates.extend(parsed)
            candidates.extend(parsed)

        bounded_family_fallbacks = self._should_bound_provider_family_fallbacks(explicit_candidates)
        if not bounded_family_fallbacks:
            current_info = resolve_model_info(self._current_model)
            candidates.extend(
                info.name
                for info in list_available_models(current_info.provider)
            )

        if not bounded_family_fallbacks:
            try:
                decision = ModelSelectionController().decide(
                    ModelSelectionSignal(
                        task_complexity=str(self._runtime.get("taskComplexity", "moderate") or "moderate"),
                        budget_pressure=float(self._runtime.get("budgetPressure", 0.0) or 0.0),
                        latency_pressure=float(self._runtime.get("latencyPressure", 0.0) or 0.0),
                        recent_failures=int(self._runtime.get("recentFailures", 0) or 0),
                        current_model=self._current_model,
                    )
                )
                if decision.fallback_model:
                    candidates.append(decision.fallback_model)
                candidates.append(decision.model)
            except Exception:
                pass

        seen: set[str] = set()
        ordered: list[str] = []
        for candidate in candidates:
            if candidate in explicit_candidates:
                normalized = candidate.strip()
            else:
                normalized = self._resolve_runtime_model_override(candidate)
            if (
                not normalized
                or normalized == self._current_model
                or normalized in self._failed_models
                or normalized in seen
                or not self._can_attempt_model(normalized)
            ):
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _should_bound_provider_family_fallbacks(self, explicit_candidates: list[str]) -> bool:
        """判断是否需要将降级限制在当前 provider 族系内。

        对于 Anthropic：如果当前模型是非 claude- 前缀的第三方模型，且有显式
        候选或运行时家族默认值，则限制在族系内。
        对于 OpenAI：如果使用了自定义兼容主机则限制。

        参数:
            explicit_candidates: 显式指定的降级候选列表。

        返回:
            是否需要限制降级范围。
        """
        try:
            current_provider = detect_provider_name(self._current_model)
        except Exception:
            return False
        if current_provider == "anthropic":
            if not self._current_model or self._current_model.startswith("claude-"):
                return False
            if explicit_candidates:
                return True
            return any(self._runtime_family_defaults.values())
        if current_provider == "openai":
            return self._uses_custom_openai_compatible_host()
        return False

    def _uses_custom_openai_compatible_host(self) -> bool:
        """判断当前是否使用非 OpenAI 官方的自定义兼容主机。

        检查 build_provider_config 中的 base_url 是否指向 OpenAI 官方地址
        （api.openai.com）之外的自定义端点。

        返回:
            是否使用了自定义 OpenAI 兼容主机。
        """
        if detect_provider_name(self._current_model) != "openai":
            return False
        try:
            provider_config = build_provider_config(self._current_model, self._runtime)
        except Exception:
            return False
        base_url = str(
            getattr(provider_config, "base_url", "")
            or getattr(provider_config, "api_base_url", "")
            or ""
        ).strip()
        if not base_url:
            return False
        normalized = _normalize_openai_base_url(base_url)
        return normalized not in {
            "https://api.openai.com",
            "https://api.openai.com/v1",
        }

    def _resolve_runtime_model_override(self, candidate: str) -> str:
        """解析候选模型名称，检查是否需要运行时家族默认值覆盖。

        如果候选模型在 _ANTHROPIC_RUNTIME_FAMILY_DEFAULTS 映射中，
        则尝试用运行时配置中的对应模型名称覆盖。

        参数:
            candidate: 候选模型名称。

        返回:
            覆盖后的模型名称，若无覆盖则返回原始名称。
        """
        normalized = candidate.strip()
        if not normalized:
            return ""
        override_key = _ANTHROPIC_RUNTIME_FAMILY_DEFAULTS.get(normalized)
        if not override_key:
            return normalized
        override_model = self._runtime_family_defaults.get(override_key, "")
        if not override_model:
            override_model = str(self._runtime.get(override_key, "") or "").strip()
        return override_model or normalized

    def _maybe_seed_runtime_family_defaults(self, model_name: str) -> None:
        """如果运行时家族默认值尚未设置，则用指定模型名称填充。

        仅在当前模型是 Anthropic 的非 claude- 前缀模型且尚未设置
        任何家族默认值时执行。

        参数:
            model_name: 用于填充的模型名称。
        """
        try:
            if detect_provider_name(model_name) != "anthropic" or model_name.startswith("claude-"):
                return
        except Exception:
            return
        if any(self._runtime_family_defaults.values()):
            return
        for key in _ANTHROPIC_RUNTIME_FAMILY_DEFAULTS.values():
            self._runtime_family_defaults[key] = model_name

    def _can_attempt_model(self, model_name: str) -> bool:
        """检查指定的模型是否可以尝试连接（是否有可用的 API key）。

        通过 build_provider_config 检查该模型的配置中是否包含 api_key。

        参数:
            model_name: 要检查的模型名称。

        返回:
            是否可以尝试连接该模型。
        """
        try:
            provider_config = build_provider_config(model_name, self._runtime)
        except Exception:
            return False
        return bool(provider_config.api_key)

    def get_switch_history(self) -> list[dict[str, Any]]:
        """获取可读的切换历史记录列表。

        返回:
            字典列表，每项包含 old（原模型）、new（新模型）、
            reason（原因）、success（是否成功）和 errors（错误信息）。
        """
        return [
            {
                "old": s.old_model,
                "new": s.new_model,
                "reason": s.reason,
                "success": s.success,
                "errors": s.errors,
            }
            for s in self._switch_history
        ]

    def get_current_adapter(self) -> Any | None:
        """获取当前的模型适配器实例。

        返回:
            当前适配器对象，若尚未设置则返回 None。
        """
        return self._current_adapter


def detect_provider_name(model: str) -> str:
    """获取模型对应的 provider 名称字符串。

    通过 model_registry.resolve_model_info 解析模型信息并返回提供方名称。

    参数:
        model: 模型名称。

    返回:
        Provider 的 value 属性字符串。
    """
    info = resolve_model_info(model)
    return info.provider.value


def _parse_model_list(raw: str) -> list[str]:
    """解析逗号分隔的模型列表字符串。

    参数:
        raw: 原始逗号分隔字符串。

    返回:
        去除空白后的模型名称列表。
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_openai_base_url(raw: str) -> str:
    """规范化 OpenAI 兼容 API 的基础 URL。

    添加默认 scheme（https），解析路径部分，统一 /v1/messages 路径为 /v1。

    参数:
        raw: 原始 URL 字符串。

    返回:
        规范化后的 URL 字符串，空输入返回空字符串。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = (parsed.path or "").rstrip("/")
    if path == "/v1/messages":
        path = "/v1"
    return f"{scheme}://{netloc}{path}"
