"""产品表面（Product Surfaces）模块。

负责收集并汇总 MiniCode 运行时的各类产品状态表面，包括：
- 指令层（Instruction Layer）信息
- 扩展（Extension）清单
- 钩子（Hook）状态
- 委托任务（Delegation）状态
- 就绪性（Readiness）报告
- 产品快照（Product Snapshot）

这些表面信息用于构建 PromptBundle，提供给 LLM 作为上下文。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from minicode.background_tasks import get_slot_stats, list_background_tasks
from minicode.config import (
    MINI_CODE_EXTENSIONS_DIR,
    MINI_CODE_MANAGED_POLICY_PATH,
    MINI_CODE_USER_PROFILE_PATH,
    configured_model_fallbacks,
    describe_fallback_guidance,
    describe_provider_channel,
    default_model_fallbacks,
    effective_model_fallbacks,
    load_runtime_config,
    project_extensions_dir,
    project_managed_policy_path,
    project_user_profile_path,
    validate_provider_runtime,
)
from minicode.hooks import get_hook_manager
from minicode.model_registry import detect_provider


@dataclass(frozen=True, slots=True)
class InstructionLayer:
    """指令层的描述信息。

    表示一个指令来源（如 CLAUDE.md、用户配置、托管策略），
    包含作用域、类型、路径以及预览和完整内容。
    """
    name: str
    scope: str
    kind: str
    path: str
    exists: bool
    preview: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """扩展的清单描述信息。

    记录扩展名称、作用域、版本号、描述、启用状态及入口点路径。
    """
    name: str
    scope: str
    path: str
    version: str = ""
    description: str = ""
    enabled: bool = True
    entrypoint: str = ""


@dataclass(frozen=True, slots=True)
class HookStatus:
    """钩子系统运行状态。

    统计注册的钩子总数、启用数、调用总次数和总耗时。
    """
    total_hooks: int
    enabled_hooks: int
    total_calls: int
    total_duration_ms: int
    summary: str


@dataclass(frozen=True, slots=True)
class DelegationStatus:
    """后台委托任务的运行状态。

    包含运行中的任务数、总跟踪数、槽位容量及活跃任务标签。
    """
    running_tasks: int
    total_tracked: int
    max_slots: int
    available_slots: int
    active_labels: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """运行时就绪性报告。

    描述当前运行时的 Provider 状态、Channel 信息、回退模型的就绪情况、
    以及未解决的 issues 汇总。
    """
    status: str
    provider: str
    provider_ready: bool
    provider_channel: str = ""
    fallback_ready: bool = False
    fallback_candidates: list[str] = field(default_factory=list)
    viable_fallbacks: list[str] = field(default_factory=list)
    fallback_guidance: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """完整的产品提示包，汇总运行时的各类表面信息。

    包含指令层、扩展清单、钩子状态、委托状态、就绪性报告和产品快照，
    用于提供给 LLM 作为全貌上下文。
    """
    prompt: str
    instruction_layers: list[InstructionLayer]
    instruction_summary: str
    hook_status: HookStatus
    delegation_status: DelegationStatus
    extension_manifests: list[ExtensionManifest]
    extension_summary: str
    readiness_report: ReadinessReport
    readiness_summary: str
    product_snapshot: dict[str, Any]


def _maybe_read_text(path: Path) -> str:
    """尝试读取文件内容，如果失败则返回空字符串。

    参数:
        path: 文件路径。

    返回:
        文件内容字符串，读取失败时返回空字符串。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _preview_text(content: str, limit: int = 100) -> str:
    """生成文本内容的前缀预览。

    将内容规范化为单行空格分隔形式，截取前 limit 个字符。

    参数:
        content: 原始文本内容。
        limit: 预览长度限制，默认 100。

    返回:
        截断后的预览字符串。
    """
    normalized = " ".join(content.split())
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _surface_value(item: Any, field_name: str, default: Any = None) -> Any:
    """安全地获取对象或字典的属性/键值。

    优先使用 hasattr/getattr 访问对象属性，如果是字典则使用 .get()。

    参数:
        item: 对象或字典。
        field_name: 属性名或键名。
        default: 未找到时的默认值。

    返回:
        属性值或键值，未找到则返回 default。
    """
    if hasattr(item, field_name):
        return getattr(item, field_name)
    if isinstance(item, dict):
        return item.get(field_name, default)
    return default


def collect_instruction_layers(cwd: str | Path) -> list[InstructionLayer]:
    """收集当前工作目录下的所有指令层。

    按优先级顺序扫描全局（home 目录）和项目（cwd）范围的
    CLAUDE.md、用户配置和托管策略文件。

    参数:
        cwd: 当前工作目录路径。

    返回:
        InstructionLayer 列表。
    """
    cwd_path = Path(cwd)
    candidates = [
        ("global-claude", "global", "claude", Path.home() / ".claude" / "CLAUDE.md"),
        ("global-user", "global", "user", MINI_CODE_USER_PROFILE_PATH),
        ("global-managed", "global", "managed", MINI_CODE_MANAGED_POLICY_PATH),
        ("project-claude", "project", "claude", cwd_path / "CLAUDE.md"),
        ("project-user", "project", "user", project_user_profile_path(cwd_path)),
        ("project-managed", "project", "managed", project_managed_policy_path(cwd_path)),
    ]
    layers: list[InstructionLayer] = []
    for name, scope, kind, path in candidates:
        content = _maybe_read_text(path) if path.exists() else ""
        layers.append(
            InstructionLayer(
                name=name,
                scope=scope,
                kind=kind,
                path=str(path),
                exists=path.exists(),
                preview=_preview_text(content),
                content=content,
            )
        )
    return layers


def format_instruction_summary(layers: list[dict[str, Any]] | list[InstructionLayer]) -> str:
    """将指令层列表格式化为一行摘要文本。

    统计启用的指令层数量，并用 "scope:kind" 格式列出。

    参数:
        layers: InstructionLayer 对象或字典列表。

    返回:
        摘要字符串，如 "instructions: 3 active layer(s) [global:claude, project:user, project:managed]"。
    """
    usable = [layer for layer in layers if bool(_surface_value(layer, "exists", False))]
    if not usable:
        return "instructions: no active layers"
    tokens = [
        f"{_surface_value(layer, 'scope', 'unknown')}:{_surface_value(layer, 'kind', 'unknown')}"
        for layer in usable
    ]
    return f"instructions: {len(usable)} active layer(s) [{', '.join(tokens)}]"


def collect_extension_manifests(cwd: str | Path) -> list[ExtensionManifest]:
    """收集当前工作目录下所有可用的扩展清单。

    扫描全局和项目范围的扩展目录，读取每个子目录中的 extension.json 文件。

    参数:
        cwd: 当前工作目录路径。

    返回:
        ExtensionManifest 列表。
    """
    manifests: list[ExtensionManifest] = []
    search_roots = extension_search_roots(cwd)
    for scope, root in search_roots:
        if not root.exists():
            continue
        for manifest_path in sorted(root.glob("*/extension.json")):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            manifests.append(
                ExtensionManifest(
                    name=str(payload.get("name") or manifest_path.parent.name),
                    scope=scope,
                    path=str(manifest_path),
                    version=str(payload.get("version", "") or ""),
                    description=str(payload.get("description", "") or ""),
                    enabled=bool(payload.get("enabled", True)),
                    entrypoint=str(payload.get("entrypoint", "") or ""),
                )
            )
    return manifests


def extension_search_roots(cwd: str | Path) -> list[tuple[str, Path]]:
    """返回扩展搜索根目录列表。

    参数:
        cwd: 当前工作目录路径。

    返回:
        (scope, root_path) 元组列表。
    """
    return [
        ("global", MINI_CODE_EXTENSIONS_DIR),
        ("project", project_extensions_dir(cwd)),
    ]


def resolve_extension_manifest(
    cwd: str | Path,
    identifier: str,
) -> ExtensionManifest:
    """根据标识符解析唯一的扩展清单。

    支持 "scope:name" 格式指定作用域。如果标识符包含 ":"，
    前半部分可以是 "global" 或 "project" 用于限定搜索范围。
    如果匹配到多个扩展或未找到则抛出异常。

    参数:
        cwd: 当前工作目录路径。
        identifier: 扩展标识符，如 "my-ext" 或 "project:my-ext"。

    返回:
        唯一匹配的 ExtensionManifest。

    抛出:
        ValueError: 标识符为空、未找到扩展或匹配到多个扩展时抛出。
    """
    requested = str(identifier or "").strip()
    if not requested:
        raise ValueError("Extension name is required.")

    scope_filter = ""
    name_filter = requested
    if ":" in requested:
        maybe_scope, remainder = requested.split(":", 1)
        maybe_scope = maybe_scope.strip().lower()
        if maybe_scope in {"global", "project"}:
            scope_filter = maybe_scope
            name_filter = remainder.strip()
    if not name_filter:
        raise ValueError("Extension name is required.")

    matches = [
        manifest
        for manifest in collect_extension_manifests(cwd)
        if (
            (not scope_filter or manifest.scope == scope_filter)
            and manifest.name == name_filter
        )
    ]
    if not matches:
        raise ValueError(f"No extension named '{requested}' was found.")
    if len(matches) > 1:
        options = ", ".join(
            f"{manifest.scope}:{manifest.name}"
            for manifest in matches
        )
        raise ValueError(
            f"Multiple extensions matched '{requested}'. Use one of: {options}"
        )
    return matches[0]


def extension_manifest_payload(manifest: ExtensionManifest) -> dict[str, Any]:
    """读取扩展清单文件的原始 JSON 内容。

    参数:
        manifest: 扩展清单对象。

    返回:
        解析后的 JSON 字典。

    抛出:
        ValueError: 无法读取或解析 JSON 文件时抛出。
    """
    manifest_path = Path(manifest.path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Failed to read extension manifest '{manifest.path}': {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Extension manifest '{manifest.path}' is not a JSON object.")
    return payload


def set_extension_enabled(
    cwd: str | Path,
    identifier: str,
    enabled: bool,
) -> ExtensionManifest:
    """启用或禁用指定扩展。

    解析扩展清单，修改其 enabled 字段，写回文件后重新解析返回。

    参数:
        cwd: 当前工作目录路径。
        identifier: 扩展标识符。
        enabled: 是否启用。

    返回:
        更新后的 ExtensionManifest。
    """
    manifest = resolve_extension_manifest(cwd, identifier)
    payload = extension_manifest_payload(manifest)
    payload["enabled"] = bool(enabled)
    manifest_path = Path(manifest.path)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return resolve_extension_manifest(cwd, f"{manifest.scope}:{manifest.name}")


def format_extension_summary(
    manifests: list[dict[str, Any]] | list[ExtensionManifest],
) -> str:
    """将扩展清单列表格式化为一行摘要文本。

    统计启用数、总数以及项目级别的扩展数。

    参数:
        manifests: ExtensionManifest 对象或字典列表。

    返回:
        摘要字符串，如 "extensions: 3/5 enabled (2 project, 3 global)"。
    """
    if not manifests:
        return "extensions: none discovered"
    enabled = [
        manifest for manifest in manifests
        if bool(_surface_value(manifest, "enabled", False))
    ]
    project_count = sum(
        1 for manifest in manifests
        if str(_surface_value(manifest, "scope", "")) == "project"
    )
    return (
        f"extensions: {len(enabled)}/{len(manifests)} enabled "
        f"({project_count} project, {len(manifests) - project_count} global)"
    )


def build_hook_status() -> HookStatus:
    """构建钩子系统的运行状态报告。

    从 HookManager 获取统计信息，生成包含总数、启用数、调用次数和耗时的 HookStatus。

    返回:
        HookStatus 对象。
    """
    stats = get_hook_manager().get_hook_stats()
    total_hooks = int(stats.get("total_hooks", 0))
    enabled_hooks = int(stats.get("enabled_hooks", 0))
    total_calls = int(stats.get("total_calls", 0))
    total_duration_ms = int(stats.get("total_duration_ms", 0))
    if total_hooks == 0:
        summary = "hooks: none registered"
    else:
        summary = (
            f"hooks: {enabled_hooks}/{total_hooks} enabled, "
            f"{total_calls} call(s), {total_duration_ms}ms total"
        )
    return HookStatus(
        total_hooks=total_hooks,
        enabled_hooks=enabled_hooks,
        total_calls=total_calls,
        total_duration_ms=total_duration_ms,
        summary=summary,
    )


def build_delegation_status() -> DelegationStatus:
    """构建后台委托任务的运行状态报告。

    从 slot 统计和任务列表中提取运行中的任务数、可用槽位和活跃标签。

    返回:
        DelegationStatus 对象。
    """
    stats = get_slot_stats()
    tasks = list_background_tasks()
    running = [task for task in tasks if task.get("status") == "running"]
    labels = [
        str(task.get("label") or task.get("command") or task.get("taskId") or "task")
        for task in running[:3]
    ]
    summary = (
        f"delegation: {len(running)} running, "
        f"{int(stats.get('available_slots', 0))}/{int(stats.get('max_slots', 0))} slots free"
    )
    if labels:
        summary += f" [{', '.join(labels)}]"
    return DelegationStatus(
        running_tasks=len(running),
        total_tracked=int(stats.get("total_tracked", 0)),
        max_slots=int(stats.get("max_slots", 0)),
        available_slots=int(stats.get("available_slots", 0)),
        active_labels=labels,
        summary=summary,
    )


def _classify_fallbacks(
    runtime: dict[str, Any],
    provider: str,
) -> tuple[list[str], list[str], list[str]]:
    """对回退模型候选进行分类，区分可用和不可用的回退。

    对每个候选回退模型创建运行时配置并调用 validate_provider_runtime 验证。

    参数:
        runtime: 当前运行时配置字典。
        provider: 当前 provider 名称。

    返回:
        (fallback_candidates, viable_fallbacks, issues) 的三元组，分别表示
        所有候选列表、可用回退列表和不可用原因列表。
    """
    fallback_candidates = [
        candidate
        for candidate in effective_model_fallbacks(
            runtime,
            provider,
            current_model=str(runtime.get("model", "")).strip(),
        )
        if candidate != str(runtime.get("model", "")).strip()
    ]
    viable: list[str] = []
    issues: list[str] = []
    for candidate in fallback_candidates:
        candidate_runtime = dict(runtime)
        candidate_runtime["model"] = candidate
        candidate_issues = validate_provider_runtime(candidate_runtime)
        if candidate_issues:
            issues.append(f"Fallback '{candidate}' is not locally ready: {candidate_issues[0]}")
            continue
        viable.append(candidate)
    return fallback_candidates, viable, issues


def build_readiness_report(
    cwd: str | Path,
    runtime: dict[str, Any] | None = None,
) -> ReadinessReport:
    """构建当前运行时的就绪性报告。

    验证 Provider 运行时配置、检测 provider 通道、评估回退模型的可用性，
    并根据 provider 和回退的就绪状态确定整体状态（ready / warning / blocked）。

    参数:
        cwd: 当前工作目录路径。
        runtime: 运行时配置字典，默认为从配置文件加载。

    返回:
        ReadinessReport 对象。
    """
    try:
        effective_runtime = runtime or load_runtime_config(cwd)
        issues = validate_provider_runtime(effective_runtime)
        provider = detect_provider(
            str(effective_runtime.get("model", "")).strip(),
            effective_runtime,
        ).value
        provider_ready = not issues
        configured_fallbacks = configured_model_fallbacks(effective_runtime, provider)
        default_fallbacks = [
            candidate
            for candidate in default_model_fallbacks(
                effective_runtime,
                provider,
                current_model=str(effective_runtime.get("model", "")).strip(),
            )
            if candidate not in configured_fallbacks
        ]
        fallback_candidates, viable_fallbacks, fallback_issues = _classify_fallbacks(
            effective_runtime,
            provider,
        )
        provider_channel = describe_provider_channel(effective_runtime, provider)
        fallback_guidance = describe_fallback_guidance(
            effective_runtime,
            provider_name=provider,
            current_model=str(effective_runtime.get("model", "")).strip(),
        )
        issues.extend(fallback_issues)
    except Exception as exc:
        effective_runtime = runtime or {}
        issues = [str(exc)]
        provider = detect_provider(
            str(effective_runtime.get("model", "")).strip(),
            effective_runtime,
        ).value if effective_runtime else "unknown"
        provider_ready = False
        configured_fallbacks = []
        default_fallbacks = []
        fallback_candidates = []
        viable_fallbacks = []
        provider_channel = describe_provider_channel(effective_runtime, provider)
        fallback_guidance = describe_fallback_guidance(
            effective_runtime,
            provider_name=provider,
            current_model=str(effective_runtime.get("model", "")).strip(),
        )
    fallback_ready = bool(viable_fallbacks)
    if provider_ready and fallback_ready:
        status = "ready"
    elif provider_ready:
        status = "warning"
        if fallback_candidates:
            if configured_fallbacks and default_fallbacks:
                issues.append("Primary provider is ready, but no configured or default fallback model is locally ready.")
            elif configured_fallbacks:
                issues.append("Primary provider is ready, but no configured fallback model is locally ready.")
            else:
                issues.append("Primary provider is ready, but no default fallback model is locally ready.")
        else:
            issues.append("Primary provider is ready, but no configured or default fallback models are available.")
    elif fallback_ready:
        status = "warning"
        if configured_fallbacks and default_fallbacks:
            issues.insert(0, "Primary provider is blocked, but at least one configured or default fallback model is locally ready.")
        elif configured_fallbacks:
            issues.insert(0, "Primary provider is blocked, but at least one configured fallback model is locally ready.")
        else:
            issues.insert(0, "Primary provider is blocked, but at least one default fallback model is locally ready.")
    else:
        status = "blocked"
    summary = f"readiness: {status} ({provider})"
    if fallback_candidates:
        summary += f" [fallbacks {len(viable_fallbacks)}/{len(fallback_candidates)} locally ready]"
    if issues:
        summary += f" [{issues[0]}]"
    return ReadinessReport(
        status=status,
        provider=provider,
        provider_ready=provider_ready,
        provider_channel=provider_channel,
        fallback_ready=fallback_ready,
        fallback_candidates=fallback_candidates,
        viable_fallbacks=viable_fallbacks,
        fallback_guidance=fallback_guidance,
        issues=issues,
        summary=summary,
    )


def build_product_snapshot(
    cwd: str | Path,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建当前工作目录的完整产品快照。

    汇总指令层、钩子状态、委托状态、扩展清单和就绪性报告。

    参数:
        cwd: 当前工作目录路径。
        runtime: 可选的运行时配置，传递给 build_readiness_report。

    返回:
        包含所有产品表面信息的字典。
    """
    instruction_layers = collect_instruction_layers(cwd)
    hook_status = build_hook_status()
    delegation_status = build_delegation_status()
    extension_manifests = collect_extension_manifests(cwd)
    readiness_report = build_readiness_report(cwd, runtime=runtime)
    return {
        "instruction_layers": [asdict(layer) for layer in instruction_layers],
        "instruction_summary": format_instruction_summary(instruction_layers),
        "hook_status": asdict(hook_status),
        "hook_summary": hook_status.summary,
        "delegated_tasks": list_background_tasks(),
        "delegation_status": asdict(delegation_status),
        "delegation_summary": delegation_status.summary,
        "extension_manifests": [asdict(manifest) for manifest in extension_manifests],
        "extension_summary": format_extension_summary(extension_manifests),
        "readiness_report": asdict(readiness_report),
        "readiness_summary": readiness_report.summary,
    }
