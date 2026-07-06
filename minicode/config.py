"""MiniCode 配置管理模块。

提供 MiniCode 的配置加载、合并、验证和持久化功能，
包括模型回退策略、供应商通道描述、MCP 配置管理等功能。
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Any


MINI_CODE_DIR = Path.home() / ".mini-code"
MINI_CODE_SETTINGS_PATH = MINI_CODE_DIR / "settings.json"
MINI_CODE_HISTORY_PATH = MINI_CODE_DIR / "history.json"
MINI_CODE_PERMISSIONS_PATH = MINI_CODE_DIR / "permissions.json"
MINI_CODE_MCP_PATH = MINI_CODE_DIR / "mcp.json"
MINI_CODE_USER_PROFILE_PATH = MINI_CODE_DIR / "USER.md"
MINI_CODE_MANAGED_POLICY_PATH = MINI_CODE_DIR / "MANAGED.md"
MINI_CODE_EXTENSIONS_DIR = MINI_CODE_DIR / "extensions"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def project_user_profile_path(cwd: str | Path | None = None) -> Path:
    """获取项目级别的 USER.md 路径。 返回指定工作目录下的 .mini-code/USER.md 文件路径，
    用于存储项目级别的用户配置文件。

    参数:
        cwd: 工作目录路径，默认为当前工作目录。

    返回:
        项目级别 USER.md 文件的 Path 对象。
    """
    return Path(cwd or Path.cwd()) / ".mini-code" / "USER.md"


def project_managed_policy_path(cwd: str | Path | None = None) -> Path:
    """获取项目级别的 MANAGED.md 路径。 返回指定工作目录下的 .mini-code/MANAGED.md 文件路径，
    用于存储项目级别的托管策略文件。

    参数:
        cwd: 工作目录路径，默认为当前工作目录。

    返回:
        项目级别 MANAGED.md 文件的 Path 对象。
    """
    return Path(cwd or Path.cwd()) / ".mini-code" / "MANAGED.md"


def project_extensions_dir(cwd: str | Path | None = None) -> Path:
    """获取项目级别的扩展目录路径。 返回指定工作目录下的 .mini-code/extensions 目录路径，
    用于存放项目级别的扩展组件。

    参数:
        cwd: 工作目录路径，默认为当前工作目录。

    返回:
        项目级别扩展目录的 Path 对象。
    """
    return Path(cwd or Path.cwd()) / ".mini-code" / "extensions"

# 已知的合法模型名称（用于拼写检查提示）
KNOWN_MODELS = [
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-haiku-3-20240307",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-5.5",
    "gpt5.5",
    "o1",
    "o1-mini",
    "o3-mini",
    # OpenRouter popular models
    "openrouter/auto",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-opus-4",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "meta-llama/llama-4-maverick",
    "deepseek/deepseek-r1",
    "deepseek/deepseek-chat",
    "qwen/qwen3-235b-a22b",
    "minimax/minimax-m1",
]


def _coerce_model_list(value: Any) -> list[str]:
    """将多种格式的模型列表输入统一转换为有序去重的字符串列表。 支持逗号分隔的字符串、列表、元组或集合作为输入，
    去除空值和重复项后返回有序列表。

    参数:
        value: 模型列表的输入，可以是字符串、列表、元组或集合。

    返回:
        有序且去重的模型名称字符串列表，输入无效时返回空列表。
    """
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def configured_model_fallbacks(
    runtime: dict[str, Any] | None,
    provider_name: str | None = None,
) -> list[str]:
    """获取运行时配置中指定的模型回退列表。 从运行配置中读取通用回退模型（fallbackModels）以及
    供应商特定的回退模型配置，合并后返回有序去重列表。

    参数:
        runtime: 运行时配置字典。
        provider_name: 供应商名称，用于查找供应商特定的回退配置。

    返回:
        有序且去重的回退模型名称列表。
    """
    runtime = runtime or {}
    candidates = _coerce_model_list(runtime.get("fallbackModels"))
    provider_key = (provider_name or "").strip().lower()
    provider_specific_keys = {
        "anthropic": "anthropicFallbackModels",
        "openai": "openaiFallbackModels",
        "openrouter": "openrouterFallbackModels",
        "custom": "customFallbackModels",
    }
    if provider_key in provider_specific_keys:
        candidates.extend(_coerce_model_list(runtime.get(provider_specific_keys[provider_key])))
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def default_model_fallbacks(
    runtime: dict[str, Any] | None,
    provider_name: str | None = None,
    current_model: str | None = None,
) -> list[str]:
    """根据当前模型和供应商推断默认的模型回退列表。 当未配置显式回退模型时，根据当前活跃模型和供应商类型，
    自动推断合理的回退模型顺序。同时考虑其他供应商的可用性。

    参数:
        runtime: 运行时配置字典。
        provider_name: 供应商名称（anthropic/openai/openrouter/custom）。
        current_model: 当前使用的模型名称，用于决定回退优先级。

    返回:
        推断出的回退模型名称列表（不含当前模型本身）。
    """
    runtime = runtime or {}
    provider_key = (provider_name or "").strip().lower()
    active_model = str(current_model or runtime.get("model", "")).strip()
    candidates: list[str] = []

    has_openai = bool(runtime.get("openaiApiKey")) and _is_valid_http_url(runtime.get("openaiBaseUrl"))
    has_openrouter = bool(runtime.get("openrouterApiKey")) and _is_valid_http_url(runtime.get("openrouterBaseUrl"))

    if provider_key == "anthropic":
        sonnet_default = str(runtime.get("anthropicDefaultSonnetModel") or "claude-sonnet-4-20250514").strip()
        haiku_default = str(runtime.get("anthropicDefaultHaikuModel") or "claude-haiku-3-20240307").strip()
        if active_model == "claude-opus-4-20250514":
            candidates.extend([sonnet_default, haiku_default])
        elif active_model == "claude-haiku-3-20240307":
            candidates.append(sonnet_default)
        elif active_model.startswith("claude-"):
            candidates.append(haiku_default)
        else:
            if has_openai:
                candidates.extend(["gpt-4o", "gpt-4o-mini"])
            if has_openrouter:
                candidates.append("openrouter/auto")
    elif provider_key == "openai":
        if active_model == "gpt-4o-mini":
            candidates.append("gpt-4o")
        elif active_model == "gpt-4o":
            candidates.append("gpt-4o-mini")
        else:
            candidates.extend(["gpt-4o", "gpt-4o-mini"])
        if has_openrouter:
            candidates.append("openrouter/auto")
    elif provider_key == "openrouter":
        candidates.append("openrouter/auto")
        if has_openai:
            candidates.append("gpt-4o-mini")
    elif provider_key == "custom":
        if has_openai:
            candidates.extend(["gpt-4o", "gpt-4o-mini"])
        elif has_openrouter:
            candidates.append("openrouter/auto")

    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized or normalized == active_model or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def effective_model_fallbacks(
    runtime: dict[str, Any] | None,
    provider_name: str | None = None,
    current_model: str | None = None,
) -> list[str]:
    """合并配置回退和默认回退，返回最终有效的模型回退列表。 优先使用配置的显式回退模型，然后补充默认推断的回退模型，
    两者合并去重后返回，确保不包含当前模型。

    参数:
        runtime: 运行时配置字典。
        provider_name: 供应商名称。
        current_model: 当前使用的模型名称。

    返回:
        最终有效的去重回退模型名称列表。
    """
    runtime = runtime or {}
    active_model = str(current_model or runtime.get("model", "")).strip()
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in [
        *configured_model_fallbacks(runtime, provider_name),
        *default_model_fallbacks(runtime, provider_name, current_model=active_model),
    ]:
        normalized = str(candidate or "").strip()
        if not normalized or normalized == active_model or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def describe_provider_channel(
    runtime: dict[str, Any] | None,
    provider_name: str | None = None,
) -> str:
    """描述当前运行时配置的供应商通道状态。 根据运行时配置和供应商类型，返回通道配置情况的文字描述，
    包括使用了哪些认证方式和端点。

    参数:
        runtime: 运行时配置字典。
        provider_name: 供应商名称，为空时自动检测。

    返回:
        描述供应商通道配置状态的字符串。
    """
    runtime = runtime or {}
    provider_key = (provider_name or "").strip().lower()
    if not provider_key:
        from minicode.model_registry import detect_provider

        provider_key = detect_provider(
            str(runtime.get("model", "")).strip(),
            runtime,
        ).value

    if provider_key == "anthropic":
        has_base = _is_valid_http_url(runtime.get("baseUrl"))
        has_token = bool(runtime.get("authToken"))
        has_key = bool(runtime.get("apiKey"))
        if has_base and has_token and has_key:
            return "anthropic-compatible via baseUrl/authToken (+ apiKey)"
        if has_base and has_token:
            return "anthropic-compatible via baseUrl/authToken"
        if has_key:
            return "anthropic via apiKey"
        return "anthropic channel not configured"

    if provider_key == "openai":
        if runtime.get("openaiApiKey") and _is_valid_http_url(runtime.get("openaiBaseUrl")):
            return "openai via openaiApiKey/openaiBaseUrl"
        return "openai channel not configured"

    if provider_key == "openrouter":
        if runtime.get("openrouterApiKey") and _is_valid_http_url(runtime.get("openrouterBaseUrl")):
            return "openrouter via openrouterApiKey/openrouterBaseUrl"
        return "openrouter channel not configured"

    if provider_key == "custom":
        if runtime.get("customApiKey") and _is_valid_http_url(runtime.get("customBaseUrl")):
            return "custom via customApiKey/customBaseUrl"
        return "custom channel not configured"

    return f"{provider_key or 'unknown'} channel"


def describe_fallback_guidance(
    runtime: dict[str, Any] | None,
    provider_name: str | None = None,
    current_model: str | None = None,
) -> list[str]:
    """生成模型回退配置的指导建议列表。 分析当前运行时配置，检查显式和默认回退模型的配置情况，
    以及缺失的凭证信息，提供配置改进建议。

    参数:
        runtime: 运行时配置字典。
        provider_name: 供应商名称，为空时自动检测。
        current_model: 当前使用的模型名称。

    返回:
        配置改进建议的字符串列表。
    """
    runtime = runtime or {}
    provider_key = (provider_name or "").strip().lower()
    if not provider_key:
        from minicode.model_registry import detect_provider

        provider_key = detect_provider(
            str(current_model or runtime.get("model", "")).strip(),
            runtime,
        ).value

    active_model = str(current_model or runtime.get("model", "")).strip()
    configured = configured_model_fallbacks(runtime, provider_key)
    defaults = default_model_fallbacks(runtime, provider_key, current_model=active_model)
    guidance: list[str] = []
    provider_specific_key = {
        "anthropic": "anthropicFallbackModels",
        "openai": "openaiFallbackModels",
        "openrouter": "openrouterFallbackModels",
        "custom": "customFallbackModels",
    }.get(provider_key, "fallbackModels")

    if (
        provider_key == "anthropic"
        and bool(runtime.get("authToken"))
        and _is_valid_http_url(runtime.get("baseUrl"))
        and not runtime.get("apiKey")
    ):
        guidance.append(
            "Primary runtime is using a single anthropic-compatible channel from baseUrl/authToken."
        )

    if not configured:
        if defaults:
            preview = ", ".join(defaults[:3])
            guidance.append(
                "Default failover is already available for this runtime"
                f"{': ' + preview if preview else '.'}"
                " If those models are still unavailable on the current provider, "
                f"set fallbackModels or {provider_specific_key} to models that the provider actually exposes, "
                "or switch provider credentials."
            )
        else:
            guidance.append(
                f"Add fallbackModels or {provider_specific_key} to enable model failover."
            )

    if provider_key in {"anthropic", "custom"}:
        if not runtime.get("openaiApiKey") and not runtime.get("openrouterApiKey") and not runtime.get("customApiKey"):
            guidance.append(
                "No local fallback credentials are configured for OpenAI, OpenRouter, or custom providers."
            )
    elif provider_key == "openai":
        if not runtime.get("openrouterApiKey") and not runtime.get("customApiKey"):
            guidance.append(
                "No local fallback credentials are configured for OpenRouter or custom providers."
            )
    elif provider_key == "openrouter":
        if not runtime.get("openaiApiKey") and not runtime.get("customApiKey"):
            guidance.append(
                "No local fallback credentials are configured for OpenAI or custom providers."
            )

    ordered: list[str] = []
    seen: set[str] = set()
    for item in guidance:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _suggest_model_name(typed: str) -> str:
    """根据输入字符串建议最接近的合法模型名称。 先尝试前缀匹配，再尝试模糊包含匹配，从已知模型列表中
    查找最佳匹配项。

    参数:
        typed: 用户输入的模型名称片段。

    返回:
        最匹配的已知模型名称，未找到时返回空字符串。
    """
    if not typed:
        return ""

    # 简单的前缀匹配
    for model in KNOWN_MODELS:
        if model.startswith(typed.lower()):
            return model

    # 模糊匹配：包含输入字符的模型
    for model in KNOWN_MODELS:
        if typed.lower() in model:
            return model

    return ""


def project_mcp_path(cwd: str | Path | None = None) -> Path:
    """获取项目级别的 MCP 配置文件路径。 返回指定工作目录下的 .mcp.json 文件路径。

    参数:
        cwd: 工作目录路径，默认为当前工作目录。

    返回:
        项目级别 .mcp.json 文件的 Path 对象。
    """
    return Path(cwd or Path.cwd()) / ".mcp.json"


def _read_json_file(file_path: Path) -> dict[str, Any]:
    """读取 JSON 文件并解析为字典。 文件不存在时返回空字典，避免抛出异常。

    参数:
        file_path: JSON 文件的完整路径。

    返回:
        解析后的字典，文件不存在时返回空字典。
    """
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def read_settings_file(file_path: Path) -> dict[str, Any]:
    """读取设置文件的 JSON 内容。 参数:
        file_path: 设置文件的完整路径。

    返回:
        设置字典，文件不存在时返回空字典。
    """
    return _read_json_file(file_path)


def read_mcp_config_file(file_path: Path) -> dict[str, Any]:
    """读取 MCP 配置文件中的 MCP 服务器配置。 从 JSON 配置文件中提取 mcpServers 字段，确保返回值为字典。

    参数:
        file_path: MCP 配置文件的完整路径。

    返回:
        MCP 服务器配置字典，文件不存在或格式无效时返回空字典。
    """
    parsed = _read_json_file(file_path)
    if not isinstance(parsed, dict):
        return {}
    mcp_servers = parsed.get("mcpServers", {})
    return mcp_servers if isinstance(mcp_servers, dict) else {}


def merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """合并两个设置字典，深度合并 env 和 mcpServers 字段。 对顶层字段进行简单覆盖，对 env 和 mcpServers 字段进行
    深度合并：env 合并环境变量，mcpServers 合并 MCP 服务器配置
    并递归合并其 env 子字段。

    参数:
        base: 基础设置字典。
        override: 覆盖设置字典。

    返回:
        合并后的设置字典。
    """
    merged_mcp = dict(base.get("mcpServers", {}))
    for name, server in override.get("mcpServers", {}).items():
        current = dict(merged_mcp.get(name, {}))
        next_server = dict(server)
        current.update(next_server)
        current["env"] = {
            **dict(merged_mcp.get(name, {}).get("env", {})),
            **dict(next_server.get("env", {})),
        }
        merged_mcp[name] = current

    return {
        **base,
        **override,
        "env": {
            **dict(base.get("env", {})),
            **dict(override.get("env", {})),
        },
        "mcpServers": merged_mcp,
    }


def load_effective_settings(
    cwd: str | Path | None = None,
    *,
    trust_project_mcp: bool = False,
) -> dict[str, Any]:
    """加载最终生效的配置，合并多级配置源。 按优先级从低到高依次加载并合并：
    1. Claude 全局设置（~/.claude/settings.json）
    2. MiniCode 全局 MCP 配置（~/.mini-code/mcp.json）
    3. 项目级 .mcp.json（需要显式信任）
    4. MiniCode 全局设置（~/.mini-code/settings.json）

    参数:
        cwd: 工作目录路径。
        trust_project_mcp: 是否信任并加载项目级别的 .mcp.json。

    返回:
        合并后的最终生效设置字典。
    """
    claude_settings = read_settings_file(CLAUDE_SETTINGS_PATH)
    global_mcp = read_mcp_config_file(MINI_CODE_MCP_PATH)

    # Security (issue #13): project-level .mcp.json is NOT loaded by default.
    # A cloned project could define a malicious MCP server (e.g. curl | sh).
    # Require explicit opt-in via --trust-project-mcp flag or env var.
    project_mcp: dict[str, Any] = {}
    pmp = project_mcp_path(cwd)
    if trust_project_mcp:
        project_mcp = read_mcp_config_file(pmp)
    elif pmp.exists():
        import logging
        logging.getLogger("minicode.config").warning(
            "Project .mcp.json found at %s but NOT loaded (security: use "
            "--trust-project-mcp or MINI_CODE_TRUST_PROJECT_MCP=1).", pmp,
        )

    mini_code_settings = read_settings_file(MINI_CODE_SETTINGS_PATH)

    return merge_settings(
        merge_settings(
            merge_settings(claude_settings, {"mcpServers": global_mcp}),
            {"mcpServers": project_mcp},
        ),
        mini_code_settings,
    )


def save_mini_code_settings(updates: dict[str, Any]) -> None:
    """保存 MiniCode 全局设置到 ~/.mini-code/settings.json。 将更新与现有设置合并后写入文件，自动创建父目录。

    参数:
        updates: 需要更新或覆盖的设置字典。
    """
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_settings_file(MINI_CODE_SETTINGS_PATH)
    next_settings = merge_settings(existing, updates)
    MINI_CODE_SETTINGS_PATH.write_text(
        json.dumps(next_settings, indent=2) + "\n",
        encoding="utf-8",
    )


def load_runtime_config(
    cwd: str | Path | None = None,
    *,
    trust_project_mcp: bool | None = None,
) -> dict[str, Any]:
    """加载并构建完整的运行时配置字典。 【为什么需要】运行时配置是整个 MiniCode 的配置中枢，
    从多级配置源（settings.json、环境变量等）收集模型、认证、
    通道、路径、偏好等信息，确保后续各模块在统一且完整的
    配置上下文中运行。

    ╔══ 完整执行流程 ══╗
    ║  第1步: 读取 ~/.mini-code/settings.json                ║
    ║   └─ 加载全局 MiniCode 设置（模型、API Key 等）        ║
    ║  第2步: 读取 .env 字段                                  ║
    ║   └─ 提取有效设置中的 env 字典（settings_env）          ║
    ║  第3步: 环境变量覆盖                                    ║
    ║   └─ os.environ 中的同名变量覆盖 settings_env 的值      ║
    ║  第4步: MCP 配置加载                                    ║
    ║   ├─ Claude 全局 MCP (~/.claude/settings.json)          ║
    ║   ├─ MiniCode 全局 MCP (~/.mini-code/mcp.json)          ║
    ║   └─ 项目级 .mcp.json（需要 --trust-project-mcp）       ║
    ║  第5步: 合并配置                                        ║
    ║   ├─ 合并 mcpServers（深度合并各层 env 子字段）         ║
    ║   ├─ 合并 env（settings_env + os.environ）              ║
    ║   ├─ 运行时设置解析（runtime_setting 优先级逻辑）        ║
    ║   ├─ 验证 model 和 auth 配置完整性                      ║
    ║   └─ 返回完整 runtime 字典                              ║
    ╚══════════════════════════════════════════════════╝

    参数:
        cwd: 工作目录路径。
        trust_project_mcp: 是否信任项目级别的 .mcp.json，
            未指定时从环境变量 MINI_CODE_TRUST_PROJECT_MCP 读取。

    返回:
        完整的运行时配置字典。

    抛出:
        RuntimeError: 未配置模型或未配置任何认证信息时抛出。
    """
    if trust_project_mcp is None:
        trust_project_mcp = os.environ.get("MINI_CODE_TRUST_PROJECT_MCP", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    load_effective = load_effective_settings
    try:
        signature = inspect.signature(load_effective)
    except (TypeError, ValueError):
        signature = None
    accepts_trust_project_mcp = signature is None or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or name == "trust_project_mcp"
        for name, parameter in signature.parameters.items()
    )
    if accepts_trust_project_mcp:
        effective = load_effective(cwd, trust_project_mcp=trust_project_mcp)
    else:
        effective = load_effective(cwd)
    settings_env = dict(effective.get("env", {}))
    env = {**settings_env, **os.environ}

    def runtime_setting(name: str, *, prefer_settings_env: bool = False) -> str:
        """获取运行时设置值，支持优先从 settings.env 读取。 按指定优先级从设置环境变量或系统环境变量中读取配置值。

        参数:
            name: 环境变量名称。
            prefer_settings_env: 是否优先从 settings.env 读取。

        返回:
            配置值字符串，未找到时返回空字符串。
        """
        if prefer_settings_env:
            value = settings_env.get(name)
            if value not in (None, ""):
                return str(value).strip()
        value = os.environ.get(name)
        if value not in (None, ""):
            return str(value).strip()
        value = settings_env.get(name)
        if value not in (None, ""):
            return str(value).strip()
        return ""

    model = (
        os.environ.get("MINI_CODE_MODEL")
        or effective.get("model")
        or runtime_setting("ANTHROPIC_MODEL", prefer_settings_env=True)
    )

    # --- Provider-specific base URLs ---
    # Anthropic
    base_url = runtime_setting("ANTHROPIC_BASE_URL", prefer_settings_env=True) or "https://api.anthropic.com"
    auth_token = runtime_setting("ANTHROPIC_AUTH_TOKEN", prefer_settings_env=True) or None
    api_key = runtime_setting("ANTHROPIC_API_KEY", prefer_settings_env=True) or None

    # OpenAI
    openai_base_url = (
        runtime_setting("OPENAI_BASE_URL", prefer_settings_env=True)
        or runtime_setting("OPENAI_API_BASE", prefer_settings_env=True)
        or effective.get("openaiBaseUrl", "")
        or "https://api.openai.com"
    )
    openai_api_key = (
        runtime_setting("OPENAI_API_KEY", prefer_settings_env=True)
        or effective.get("openaiApiKey", "")
    )

    # OpenRouter
    openrouter_base_url = (
        runtime_setting("OPENROUTER_BASE_URL", prefer_settings_env=True)
        or "https://openrouter.ai/api"
    )
    openrouter_api_key = runtime_setting("OPENROUTER_API_KEY", prefer_settings_env=True)

    # Custom endpoint
    custom_base_url = (
        runtime_setting("CUSTOM_API_BASE_URL", prefer_settings_env=True)
        or effective.get("customBaseUrl", "")
    )
    custom_api_key = (
        runtime_setting("CUSTOM_API_KEY", prefer_settings_env=True)
        or effective.get("customApiKey", "")
        or openai_api_key
    )

    raw_max_output_tokens = (
        os.environ.get("MINI_CODE_MAX_OUTPUT_TOKENS")
        or effective.get("maxOutputTokens")
        or env.get("MINI_CODE_MAX_OUTPUT_TOKENS")
    )
    max_output_tokens = None
    if raw_max_output_tokens is not None:
        try:
            parsed = int(raw_max_output_tokens)
            if parsed > 0:
                max_output_tokens = parsed
        except (TypeError, ValueError):
            max_output_tokens = None

    # Validate: at least one auth method must be available
    has_auth = any([
        auth_token, api_key, openai_api_key, openrouter_api_key, custom_api_key,
    ])
    if not model:
        raise RuntimeError("No model configured. Set ~/.mini-code/settings.json or ANTHROPIC_MODEL.")
    if not has_auth:
        raise RuntimeError(
            "No auth configured. Set one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "OPENROUTER_API_KEY, or CUSTOM_API_KEY."
        )

    # --- User profile paths ---
    global_user_profile = MINI_CODE_USER_PROFILE_PATH
    proj_user_profile = project_user_profile_path(cwd)
    global_managed_policy = MINI_CODE_MANAGED_POLICY_PATH
    proj_managed_policy = project_managed_policy_path(cwd)
    global_extensions = MINI_CODE_EXTENSIONS_DIR
    proj_extensions = project_extensions_dir(cwd)

    # --- User preferences from settings (lightweight, not from USER.md) ---
    user_preferences = effective.get("userPreferences", {})
    response_language = (
        str(env.get("MINI_CODE_LANGUAGE", "")).strip()
        or user_preferences.get("language", "")
    )
    response_verbosity = (
        str(env.get("MINI_CODE_VERBOSITY", "")).strip()
        or user_preferences.get("verbosity", "")
    )
    fallback_models = _coerce_model_list(
        os.environ.get("MINI_CODE_MODEL_FALLBACKS", "")
        or effective.get("fallbackModels", [])
    )
    anthropic_fallback_models = _coerce_model_list(
        os.environ.get("ANTHROPIC_MODEL_FALLBACKS", "")
        or effective.get("anthropicFallbackModels", [])
    )
    openai_fallback_models = _coerce_model_list(
        os.environ.get("OPENAI_MODEL_FALLBACKS", "")
        or effective.get("openaiFallbackModels", [])
    )
    openrouter_fallback_models = _coerce_model_list(
        os.environ.get("OPENROUTER_MODEL_FALLBACKS", "")
        or effective.get("openrouterFallbackModels", [])
    )
    custom_fallback_models = _coerce_model_list(
        os.environ.get("CUSTOM_MODEL_FALLBACKS", "")
        or effective.get("customFallbackModels", [])
    )

    return {
        "model": model,
        "configuredModel": model,
        "baseUrl": base_url,
        "authToken": auth_token,
        "apiKey": api_key,
        "anthropicDefaultSonnetModel": str(
            runtime_setting("ANTHROPIC_DEFAULT_SONNET_MODEL", prefer_settings_env=True)
            or effective.get("anthropicDefaultSonnetModel")
            or runtime_setting("ANTHROPIC_MODEL", prefer_settings_env=True)
            or effective.get("model", "")
        ).strip(),
        "anthropicDefaultOpusModel": str(
            runtime_setting("ANTHROPIC_DEFAULT_OPUS_MODEL", prefer_settings_env=True)
            or effective.get("anthropicDefaultOpusModel")
            or runtime_setting("ANTHROPIC_MODEL", prefer_settings_env=True)
            or effective.get("model", "")
        ).strip(),
        "anthropicDefaultHaikuModel": str(
            runtime_setting("ANTHROPIC_DEFAULT_HAIKU_MODEL", prefer_settings_env=True)
            or effective.get("anthropicDefaultHaikuModel")
            or runtime_setting("ANTHROPIC_MODEL", prefer_settings_env=True)
            or effective.get("model", "")
        ).strip(),
        "openaiBaseUrl": openai_base_url,
        "openaiApiKey": openai_api_key,
        "openrouterBaseUrl": openrouter_base_url,
        "openrouterApiKey": openrouter_api_key,
        "customBaseUrl": custom_base_url,
        "customApiKey": custom_api_key,
        "maxOutputTokens": max_output_tokens,
        "mcpServers": effective.get("mcpServers", {}),
        "globalUserProfilePath": str(global_user_profile),
        "projectUserProfilePath": str(proj_user_profile),
        "globalManagedPolicyPath": str(global_managed_policy),
        "projectManagedPolicyPath": str(proj_managed_policy),
        "globalExtensionsDir": str(global_extensions),
        "projectExtensionsDir": str(proj_extensions),
        "responseLanguage": response_language,
        "responseVerbosity": response_verbosity,
        "fallbackModels": fallback_models,
        "anthropicFallbackModels": anthropic_fallback_models,
        "openaiFallbackModels": openai_fallback_models,
        "openrouterFallbackModels": openrouter_fallback_models,
        "customFallbackModels": custom_fallback_models,
        "runtimeProfile": str(
            os.environ.get("MINI_CODE_RUNTIME_PROFILE")
            or effective.get("runtimeProfile", "")
            or "single"
        ).strip().lower(),
        "toolProfile": str(
            os.environ.get("MINI_CODE_TOOL_PROFILE")
            or effective.get("toolProfile", "")
            or "core"
        ).strip().lower(),
        "sourceSummary": f"config: {MINI_CODE_SETTINGS_PATH} > {CLAUDE_SETTINGS_PATH} > process.env",
    }


def _is_valid_http_url(value: str | None) -> bool:
    """判断字符串是否为合法的 HTTP/HTTPS URL。 使用 urllib.parse.urlparse 解析并验证 scheme 和 netloc。

    参数:
        value: 待验证的 URL 字符串。

    返回:
        合法返回 True，否则返回 False。
    """
    if not value:
        return False
    parsed = urlparse(str(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_provider_runtime(runtime: dict[str, Any]) -> list[str]:
    """验证运行时配置中检测到的供应商所需的认证和端点配置。 根据模型自动检测供应商类型，检查对应的 API Key 和
    Base URL 是否已正确配置。

    参数:
        runtime: 运行时配置字典。

    返回:
        错误消息列表，配置完全正确时返回空列表。
    """
    from minicode.model_registry import Provider, detect_provider

    model = str(runtime.get("model", "")).strip()
    provider = detect_provider(model, runtime)
    errors: list[str] = []

    if provider == Provider.OPENAI:
        if not runtime.get("openaiApiKey"):
            errors.append(
                "Provider is openai for this model, but OPENAI_API_KEY/openaiApiKey is not configured."
            )
        if not _is_valid_http_url(runtime.get("openaiBaseUrl")):
            errors.append("OpenAI base URL must be an http(s) URL.")
    elif provider == Provider.OPENROUTER:
        if not runtime.get("openrouterApiKey"):
            errors.append(
                "Provider is openrouter for this model, but OPENROUTER_API_KEY is not configured."
            )
        if not _is_valid_http_url(runtime.get("openrouterBaseUrl")):
            errors.append("OpenRouter base URL must be an http(s) URL.")
    elif provider == Provider.CUSTOM:
        if not runtime.get("customBaseUrl"):
            errors.append("Provider is custom, but CUSTOM_API_BASE_URL/customBaseUrl is not configured.")
        elif not _is_valid_http_url(runtime.get("customBaseUrl")):
            errors.append("Custom base URL must be an http(s) URL.")
        if not runtime.get("customApiKey"):
            errors.append("Provider is custom, but CUSTOM_API_KEY/customApiKey is not configured.")
    elif provider == Provider.ANTHROPIC:
        if not (runtime.get("apiKey") or runtime.get("authToken")):
            errors.append(
                "Provider is anthropic for this model, but ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is not configured."
            )
        if not _is_valid_http_url(runtime.get("baseUrl")):
            errors.append("Anthropic base URL must be an http(s) URL.")

    return errors


def get_mcp_config_path(scope: str, cwd: str | Path | None = None) -> Path:
    """根据作用域获取 MCP 配置文件的路径。 作用域为 "project" 时返回项目级别路径，否则返回全局路径。

    参数:
        scope: 作用域，"project" 表示项目级别，其他值表示全局。
        cwd: 工作目录路径。

    返回:
        MCP 配置文件的 Path 对象。
    """
    return project_mcp_path(cwd) if scope == "project" else MINI_CODE_MCP_PATH


def load_scoped_mcp_servers(scope: str, cwd: str | Path | None = None) -> dict[str, Any]:
    """加载指定作用域的 MCP 服务器配置。 参数:
        scope: 作用域，"project" 或 "global"。
        cwd: 工作目录路径。

    返回:
        MCP 服务器配置字典。
    """
    return read_mcp_config_file(get_mcp_config_path(scope, cwd))


def save_scoped_mcp_servers(scope: str, servers: dict[str, Any], cwd: str | Path | None = None) -> None:
    """保存 MCP 服务器配置到指定作用域的配置文件。 将服务器配置写入对应的 MCP 配置文件，自动创建父目录。

    参数:
        scope: 作用域，"project" 或 "global"。
        servers: MCP 服务器配置字典。
        cwd: 工作目录路径。
    """
    target = get_mcp_config_path(scope, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")


def validate_config(cwd: str | Path | None = None) -> tuple[bool, list[str]]:
    """验证配置完整性，返回验证结果和消息列表。 检查模型是否配置、API Key 是否配置、模型名称拼写是否正确、
    MCP 配置文件是否合法等。提供友好的错误提示和修复建议。

    参数:
        cwd: 工作目录路径。

    返回:
        包含 (是否有效, 消息列表) 的元组。
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        config = load_runtime_config(cwd)
        errors.extend(validate_provider_runtime(config))

        # 检查模型名称拼写
        model = config.get("model", "")
        if model and not any(model.lower() == km.lower() for km in KNOWN_MODELS):
            suggestion = _suggest_model_name(model)
            if suggestion:
                warnings.append(
                    f"Unknown model '{model}'. Did you mean '{suggestion}'?"
                )
            else:
                warnings.append(
                    f"Unknown model '{model}'. Known models: {', '.join(KNOWN_MODELS[:3])}..."
                )

        # 检查 MCP 配置
        mcp_servers = config.get("mcpServers", {})
        for name, server in mcp_servers.items():
            if not server.get("command"):
                errors.append(f"MCP server '{name}' has no command configured")

        return len(errors) == 0, errors + warnings

    except RuntimeError as e:
        error_msg = str(e)

        # 提供友好的错误消息
        if "No model configured" in error_msg:
            suggestion = _suggest_model_name(os.environ.get("MINI_CODE_MODEL", ""))
            help_msg = (
                f"Error: {error_msg}\n\n"
                "How to fix:\n"
                "  1. Set model name: export ANTHROPIC_MODEL=claude-sonnet-4-20250514\n"
                "  2. Or edit ~/.mini-code/settings.json:\n"
                f'     {{"model": "claude-sonnet-4-20250514"}}\n'
            )
            if suggestion:
                help_msg += f"\n  Did you mean: {suggestion}?\n"
            help_msg += f"\n  Known models: {', '.join(KNOWN_MODELS[:3])}..."
            errors.append(help_msg)

        elif "No auth configured" in error_msg:
            help_msg = (
                f"Error: {error_msg}\n\n"
                "How to fix:\n"
                "  1. Anthropic:  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "  2. OpenAI:     export OPENAI_API_KEY=sk-...\n"
                "  3. OpenRouter: export OPENROUTER_API_KEY=sk-or-...\n"
                "  4. Custom:     export CUSTOM_API_KEY=... + CUSTOM_API_BASE_URL=...\n"
                "  5. Or edit ~/.mini-code/settings.json:\n"
                '     {"env": {"ANTHROPIC_API_KEY": "sk-ant-..."}}\n'
            )
            errors.append(help_msg)
        else:
            errors.append(str(e))

        return False, errors
    except Exception as e:
        return False, [f"Unexpected error: {e}"]


def format_config_diagnostic(cwd: str | Path | None = None) -> str:
    """格式化配置诊断信息为可读字符串。 运行配置验证并生成格式化的诊断报告，包含配置状态、
    当前配置摘要、供应商通道信息等。

    参数:
        cwd: 工作目录路径。

    返回:
        格式化的诊断信息字符串。
    """
    is_valid, messages = validate_config(cwd)

    lines = ["Configuration Diagnostics", "=" * 40, ""]

    if is_valid:
        lines.append("Status: OK")
        if messages:
            lines.append("")
            lines.append("Warnings:")
            for msg in messages:
                lines.append(f"  [WARN] {msg}")
    else:
        lines.append("Status: ERRORS")
        lines.append("")
        lines.append("Errors:")
        for msg in messages:
            lines.append(f"  [ERROR] {msg}")

    # 显示当前配置摘要
    try:
        config = load_runtime_config(cwd)
        model_name = config.get('model', 'not set')
        lines.append("")
        lines.append("Current Configuration")
        lines.append("-" * 40)
        lines.append(f"  Model: {model_name}")

        # Show provider info
        from minicode.model_registry import detect_provider, Provider
        provider = detect_provider(model_name, config)
        lines.append(f"  Provider: {provider.value}")
        lines.append(f"  Channel: {describe_provider_channel(config, provider.value)}")

        if provider == Provider.ANTHROPIC:
            lines.append(f"  Base URL: {config.get('baseUrl', 'not set')}")
            auth_methods = []
            if config.get("authToken"):
                auth_methods.append("ANTHROPIC_AUTH_TOKEN")
            if config.get("apiKey"):
                auth_methods.append("ANTHROPIC_API_KEY")
        elif provider == Provider.OPENAI:
            lines.append(f"  OpenAI Base URL: {config.get('openaiBaseUrl', 'not set')}")
            auth_methods = ["OPENAI_API_KEY"] if config.get("openaiApiKey") else []
        elif provider == Provider.OPENROUTER:
            lines.append(f"  OpenRouter Base URL: {config.get('openrouterBaseUrl', 'not set')}")
            auth_methods = ["OPENROUTER_API_KEY"] if config.get("openrouterApiKey") else []
        elif provider == Provider.CUSTOM:
            lines.append(f"  Custom Base URL: {config.get('customBaseUrl', 'not set')}")
            auth_methods = ["CUSTOM_API_KEY"] if config.get("customApiKey") else []
        else:
            auth_methods = []

        lines.append(f"  Auth: {', '.join(auth_methods) or 'none'}")

        fallback_models = effective_model_fallbacks(config, provider.value, current_model=model_name)
        if fallback_models:
            lines.append(f"  Fallback Models: {', '.join(fallback_models)}")
        lines.append(f"  MCP Servers: {len(config.get('mcpServers', {}))}")
        lines.append(f"  Tool Profile: {config.get('toolProfile', 'core')}")

        # User profile info
        global_profile_path = config.get('globalUserProfilePath', '')
        project_profile_path = config.get('projectUserProfilePath', '')
        if global_profile_path:
            gp_exists = Path(global_profile_path).exists()
            lines.append(f"  Global Profile: {global_profile_path} ({'exists' if gp_exists else 'not found'})")
        if project_profile_path:
            pp_exists = Path(project_profile_path).exists()
            lines.append(f"  Project Profile: {project_profile_path} ({'exists' if pp_exists else 'not found'})")
        if config.get('responseLanguage'):
            lines.append(f"  Response Language: {config.get('responseLanguage')}")
        if config.get('responseVerbosity'):
            lines.append(f"  Response Verbosity: {config.get('responseVerbosity')}")
    except Exception:
        pass

    return "\n".join(lines)
