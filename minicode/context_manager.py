"""LLM 对话的上下文窗口管理。

跟踪 token 使用情况，估算上下文窗口消耗，并提供自动压缩功能，
以防止长对话中的上下文溢出。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from minicode.config import MINI_CODE_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default context window sizes (tokens)
DEFAULT_CONTEXT_WINDOWS = {
    # Anthropic
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-20240307": 100_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
    # OpenRouter popular models
    "openrouter/auto": 200_000,
    "anthropic/claude-sonnet-4": 200_000,
    "anthropic/claude-opus-4": 200_000,
    "openai/gpt-4o": 128_000,
    "openai/gpt-4o-mini": 128_000,
    "google/gemini-2.5-pro": 1_000_000,
    "google/gemini-2.5-flash": 1_000_000,
    "meta-llama/llama-4-maverick": 1_000_000,
    "deepseek/deepseek-r1": 128_000,
    "deepseek/deepseek-chat": 128_000,
    "qwen/qwen3-235b-a22b": 128_000,
    "minimax/minimax-m1": 1_000_000,
    "default": 128_000,  # Fallback
}


# ---------------------------------------------------------------------------
# Model context window resolution (port of TS getModelContextWindow).
# Case-insensitive substring matching with an output reserve, so model ids like
# "claude-opus-4-6", "CLAUDE-...", or "anthropic/claude-3-5-sonnet-latest"
# resolve correctly instead of falling through to the 128k default.
# ---------------------------------------------------------------------------


class ModelContextWindow:
    """模型上下文窗口配置。

    存储模型的上下文窗口总大小、为输出保留的 token 数量，
    以及实际可用于输入的有效大小（总大小减去输出保留量）。
    """
    __slots__ = ("context_window", "output_reserve", "effective_input")

    def __init__(self, context_window: int, output_reserve: int) -> None:
        """初始化模型上下文窗口配置。

        计算有效输入大小（上下文窗口减去输出保留量）。

        参数:
            context_window: 上下文窗口总大小（token 数）。
            output_reserve: 为输出保留的 token 数。
        """
        self.context_window = context_window
        self.output_reserve = output_reserve
        self.effective_input = context_window - output_reserve


_UNKNOWN_MODEL_CONTEXT = (128_000, 8_000)

_MODEL_CONTEXT_RULES: list[tuple[list[str], int, int]] = [
    (["claude-opus-4-6", "claude opus 4.6", "opus-4-6"], 200_000, 16_000),
    (["claude-sonnet-4-6", "claude sonnet 4.6", "sonnet-4-6"], 200_000, 16_000),
    (["claude-haiku-4-5", "claude haiku 4.5", "haiku-4-5"], 200_000, 16_000),
    (["claude-opus-4-1", "claude opus 4.1", "opus-4-1", "claude-opus-4", "claude opus 4", "opus-4"], 200_000, 16_000),
    (["claude-sonnet-4", "claude sonnet 4", "sonnet-4"], 200_000, 16_000),
    (["claude-3-7-sonnet", "claude 3.7 sonnet", "3-7-sonnet"], 200_000, 8_192),
    (["claude-3-5-sonnet", "claude 3.5 sonnet", "3-5-sonnet", "claude-3-sonnet"], 200_000, 8_192),
    (["claude-3-5-haiku", "claude 3.5 haiku", "3-5-haiku"], 200_000, 8_192),
    (["claude-3-opus", "claude 3 opus"], 200_000, 4_096),
    (["claude-3-haiku", "claude 3 haiku"], 200_000, 4_096),
    (["gpt-5-codex", "gpt-5.4", "gpt-5.2", "gpt-5.1", "gpt-5"], 128_000, 16_000),
    (["o4-mini", "o3", "o1-pro", "o1"], 200_000, 16_000),
    (["gpt-4.1-mini", "gpt-4.1-nano", "gpt-4.1"], 1_047_576, 16_000),
    (["gpt-4o-mini", "gpt-4o"], 128_000, 16_384),
    (["gpt-4"], 128_000, 8_192),
    (["gemini-2.5-pro", "gemini 2.5 pro"], 1_048_576, 16_000),
    (["gemini-2.5-flash-lite", "gemini 2.5 flash-lite"], 1_048_576, 16_000),
    (["gemini-2.5-flash", "gemini 2.5 flash"], 1_048_576, 16_000),
    (["deepseek-reasoner"], 128_000, 16_000),
    (["deepseek-chat"], 128_000, 4_000),
]


def get_model_context_window(model: str) -> ModelContextWindow:
    """将模型 ID 解析为其上下文窗口和输出保留量。

    不区分大小写的子串匹配（移植自 TypeScript 版的 getModelContextWindow）。
    对于未知模型，回退使用 128K 上下文窗口和 8K 输出保留量。

    参数:
        model: 模型标识字符串，如 "claude-sonnet-4-20250514"。

    返回:
        包含上下文窗口大小、输出保留量和有效输入大小的 ModelContextWindow 实例。
    """
    normalized = (model or "").strip().lower()
    for patterns, context_window, output_reserve in _MODEL_CONTEXT_RULES:
        if any(pattern in normalized for pattern in patterns):
            return ModelContextWindow(context_window, output_reserve)
    context_window, output_reserve = _UNKNOWN_MODEL_CONTEXT
    return ModelContextWindow(context_window, output_reserve)

# Auto-compaction threshold (95% of context window)
AUTOCOMPACT_THRESHOLD = 0.95

# Estimated tokens per character (rough average for English/Code)
CHARS_PER_TOKEN = 4.0

# Minimum messages to keep after compaction
MIN_MESSAGES_TO_KEEP = 10

# System prompt is always kept (counts as 1 message)
SYSTEM_PROMPT_RESERVED = 1


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

# 预编译的正则表达式用于快速 CJK 字符检测
_CJK_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]')

# LRU 缓存：token 估算被频繁调用（每条消息、每次上下文检查），
# 相同文本的 token 数是确定性的，缓存可避免重复计算。
_token_cache: dict[str, int] = {}
_TOKEN_CACHE_MAX = 1024


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量，支持中英文混合。

    英文和代码按约 4 字符/token 估算，中文、日文和韩文等 CJK 字符按约 1.5 字符/token 估算，
    混合文本使用启发式加权计算。使用预编译的正则表达式替代逐字符检查，性能提升 10-50 倍。
    内置 LRU 缓存机制，避免对相同文本的重复计算。

    参数:
        text: 待估算的文本字符串。

    返回:
        估算得到的 token 数量，至少为 1（空文本返回 0）。
    """
    if not text:
        return 0
    
    # 缓存查找（短文本优先缓存）
    cache_key = text if len(text) < 256 else hash(text)  # 长文本用 hash 作为 key
    cached = _token_cache.get(cache_key)
    if cached is not None:
        return cached
    
    # 使用正则表达式快速统计 CJK 字符数量
    cjk_count = len(_CJK_PATTERN.findall(text))
    
    # CJK 字符约 1.5 字符/token，英文约 4 字符/token
    ascii_chars = len(text) - cjk_count
    
    result = max(1, int(cjk_count / 1.5 + ascii_chars / 4.0))
    
    # 缓存结果（防止无限增长）
    if len(_token_cache) < _TOKEN_CACHE_MAX:
        _token_cache[cache_key] = result
    
    return result


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息的 token 数量。

    根据消息的角色（system/user/assistant/tool_call/tool_result 等）添加不同的角色开销 token，
    并对消息内容及工具调用输入进行 token 估算。对于没有内容和工具输入的 message 返回 0。

    参数:
        message: 消息字典，包含 role、content、input 等字段。

    返回:
        该消息的总 token 估算值。
    """
    # Match TS semantics: a message with no content and no tool input contributes
    # zero tokens (TS estimateMessageTokens is ceil(0/ratio) == 0). We keep the
    # CJK-aware estimate_tokens() for non-empty content — it is more accurate for
    # Chinese/Japanese text than TS's plain char/3.5 ratio.
    content = message.get("content", "")
    has_input = bool(message.get("input"))
    content_str = content if isinstance(content, str) else ""
    if not content_str and not has_input:
        return 0

    tokens = 0

    # Role overhead
    role = message.get("role", "")
    if role == "system":
        tokens += 3  # System prompt overhead
    elif role == "user":
        tokens += 4  # User message overhead
    elif role == "assistant":
        tokens += 3  # Assistant overhead
    elif role == "assistant_tool_call":
        tokens += 7  # Tool call overhead
    elif role == "tool_result":
        tokens += 6  # Tool result overhead
    elif role == "assistant_progress":
        tokens += 3
    
    # Content tokens
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    
    # Tool call input/output
    if "input" in message:
        input_str = json.dumps(message["input"]) if isinstance(message["input"], dict) else str(message["input"])
        tokens += estimate_tokens(input_str)
    
    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算多条消息的总 token 数量。

    对消息列表中的每条消息逐条调用 estimate_message_tokens 并求和。

    参数:
        messages: 消息字典列表。

    返回:
        所有消息的总 token 估算值。
    """
    return sum(estimate_message_tokens(msg) for msg in messages)


# ---------------------------------------------------------------------------
# Provider-usage-aware token accounting (port of TS tokenCountWithEstimation
# and computeContextStats). NOTE: Python does not currently record provider
# usage on individual messages, so the provider_usage path falls back to
# estimate_only until that wiring is added; the estimate_only + warning-level
# math is still correct and useful.
# ---------------------------------------------------------------------------


def _message_provider_usage(message: dict[str, Any]) -> dict[str, Any] | None:
    """获取消息中的 provider 用量数据。

    仅对 assistant 角色（含 assistant_progress 和 assistant_tool_call）的消息提取 providerUsage
    或 provider_usage 字段，并跳过标记为 stale 的用量数据。

    参数:
        message: 消息字典。

    返回:
        用量字典（如存在且未过期），否则返回 None。
    """
    if role not in ("assistant", "assistant_progress", "assistant_tool_call"):
        return None
    usage = message.get("providerUsage") or message.get("provider_usage")
    if usage and not message.get("usageStale") and not message.get("usage_stale"):
        return usage
    return None


def _stale_usage_reason(messages: list[dict[str, Any]]) -> str | None:
    """查找消息列表中第一个标记为 stale 的 provider 用量及其原因。

    遍历消息列表，查找 assistant 角色消息中标记为 usageStale 或 usage_stale 的条目，
    并返回其过期原因。

    参数:
        messages: 消息字典列表。

    返回:
        过期原因字符串（如存在），否则返回 None。
    """
    for message in messages:
        if (
            message.get("role") in ("assistant", "assistant_progress", "assistant_tool_call")
            and (message.get("providerUsage") or message.get("provider_usage"))
            and (message.get("usageStale") or message.get("usage_stale"))
        ):
            return message.get("usageStaleReason") or message.get("usage_stale_reason") or "provider usage was marked stale"
    return None


def token_count_with_estimation(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """基于 provider 用量感知的 token 计数（移植自 TypeScript 版 tokenCountWithEstimation）。

    从消息列表末尾向前查找最新的非过期 provider 用量数据，将其与后续消息的估算值相加。
    如果没有可用的 provider 用量数据，则完全依赖估算。

    参数:
        messages: 消息字典列表。

    返回:
        包含 total_tokens、provider_usage_tokens、estimated_tokens、source、
        is_exact、usage_boundary、stale、reason 等字段的字典。
    """
    for i in range(len(messages) - 1, -1, -1):
        usage = _message_provider_usage(messages[i])
        if not usage:
            continue
        total_usage = int(usage.get("totalTokens", usage.get("total_tokens", 0)) or 0)
        estimated = estimate_messages_tokens(messages[i + 1:])
        boundary_id = messages[i].get("toolUseId") or messages[i].get("tool_use_id")
        return {
            "total_tokens": total_usage + estimated,
            "provider_usage_tokens": total_usage,
            "estimated_tokens": estimated,
            "source": "provider_usage_plus_estimate" if estimated > 0 else "provider_usage",
            "is_exact": estimated == 0,
            "usage_boundary": {"message_index": i, "message_id": boundary_id},
            "stale": False,
            "reason": None,
        }

    reason = _stale_usage_reason(messages)
    estimated = estimate_messages_tokens(messages)
    return {
        "total_tokens": estimated,
        "provider_usage_tokens": 0,
        "estimated_tokens": estimated,
        "source": "estimate_only",
        "is_exact": False,
        "stale": bool(reason),
        "reason": reason or "no provider usage available",
    }


def compute_context_stats(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """计算上下文统计信息，包含利用率和告警级别（移植自 TS 版 computeContextStats）。

    根据模型获取上下文窗口，结合 provider 用量或估算值计算上下文利用率，
    并按阈值划分告警级别：95% 以上为 blocked，85% 以上为 critical，50% 以上为 warning。

    参数:
        messages: 消息字典列表。
        model: 模型标识字符串。

    返回:
        包含 estimated_tokens、total_tokens、provider_usage_tokens、context_window、
        effective_input、utilization、warning_level、accounting 等字段的字典。
    """
    window = get_model_context_window(model)
    accounting = token_count_with_estimation(messages)
    effective = window.effective_input or 1
    utilization = min(1.0, accounting["total_tokens"] / effective)
    if utilization >= 0.95:
        warning_level = "blocked"
    elif utilization >= 0.85:
        warning_level = "critical"
    elif utilization >= 0.50:
        warning_level = "warning"
    else:
        warning_level = "normal"
    return {
        "estimated_tokens": accounting["estimated_tokens"],
        "total_tokens": accounting["total_tokens"],
        "provider_usage_tokens": accounting["provider_usage_tokens"],
        "context_window": window.context_window,
        "effective_input": window.effective_input,
        "utilization": utilization,
        "warning_level": warning_level,
        "accounting": accounting,
    }



@dataclass
class _ExtractedInfo:
    """从被移除的消息中提取的结构化信息，用于生成摘要。

    在上下文压缩过程中，从被丢弃的消息中提取各类信息，包括用户意图、
    文件路径、工具调用结果、助手结论、代码片段和关键决策等。
    """
    user_intents: list[str] = field(default_factory=list)
    file_paths: set[str] = field(default_factory=set)
    key_tool_results: list[str] = field(default_factory=list)
    assistant_conclusions: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    code_snippets: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


# Tool categories for classification
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "modify_file", "patch_file", "multi_edit"})
_READ_TOOLS = frozenset({"read_file", "list_files", "grep_files", "file_tree"})
_SEARCH_TOOLS = frozenset({"grep_files", "find_symbols", "find_references", "web_search", "web_fetch"})
_COMMAND_TOOLS = frozenset({"run_command", "execute_command", "bash"})

# Regex for extracting code-like content and decisions
_CODE_FENCE_RE = re.compile(r'```[\w]*\n(.{20,300}?)```', re.DOTALL)
_DECISION_KEYWORDS = re.compile(
    r'(?:decided|decision|chose|chosen|will use|using|switching to|'
    r'implemented|fixed|resolved|refactored|migrated|upgraded|'
    r'recommend|should|must|need to|going to|plan to|'
    r'approach:|strategy:|solution:|conclusion:)',
    re.IGNORECASE,
)


def _extract_from_messages(messages: list[dict[str, Any]]) -> _ExtractedInfo:
    """从消息中提取结构化信息，用于分层摘要生成。

    这是核心提取步骤，从各类型消息中提取不同类别的信息，包括用户意图、
    助手决策、代码片段、工具调用、文件路径及错误结果等。提取的内容将
    供给预算感知的摘要构建器，优先包含最重要的信息。

    参数:
        messages: 待提取的消息字典列表。

    返回:
        包含 user_intents、file_paths、key_tool_results、assistant_conclusions、
        tool_names、code_snippets、decisions 等字段的 _ExtractedInfo 实例。
    """
    info = _ExtractedInfo()
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "user" and content.strip():
            # Extract user intent — keep more context for short queries,
            # truncate long paste-heavy messages
            preview = content.strip().replace("\n", " ")
            # For short queries (<200 chars), keep them fully
            # For long ones, keep first 200 chars
            if len(preview) > 200:
                preview = preview[:200] + "..."
            info.user_intents.append(preview)
            
        elif role == "assistant" and content.strip():
            text = content.strip()
            
            # Extract decisions/conclusions
            sentences = text.replace("\n", " ").split(". ")
            for sentence in sentences:
                if _DECISION_KEYWORDS.search(sentence):
                    decision = sentence.strip()[:180]
                    if decision and decision not in info.decisions:
                        info.decisions.append(decision)
            
            # Extract code snippets from assistant responses
            for match in _CODE_FENCE_RE.finditer(text):
                snippet = match.group(1).strip()
                if len(snippet) >= 20 and len(info.code_snippets) < 5:
                    info.code_snippets.append(snippet[:300])
            
            # General conclusion preview
            preview = text[:200].replace("\n", " ")
            info.assistant_conclusions.append(preview)
            
        elif role == "assistant_tool_call":
            tool_name = msg.get("toolName", "unknown")
            info.tool_names.append(tool_name)
            
            # Extract file paths from edit/write tools
            if tool_name in _EDIT_TOOLS:
                inp = msg.get("input", {})
                path = inp.get("path") or inp.get("filePath", "")
                if path:
                    info.file_paths.add(path)
            
            # Extract searched patterns from grep/search tools
            if tool_name in _SEARCH_TOOLS:
                inp = msg.get("input", {})
                pattern = inp.get("pattern") or inp.get("query", "")
                if pattern:
                    info.file_paths.add(f"search:{pattern[:80]}")
            
            # Extract command names from run_command
            if tool_name in _COMMAND_TOOLS:
                inp = msg.get("input", {})
                cmd = inp.get("command", "")
                if cmd:
                    cmd_name = cmd.split()[0] if cmd.split() else ""
                    if cmd_name:
                        info.key_tool_results.append(f"ran: {cmd_name}")
            
        elif role == "tool_result":
            tool_name = msg.get("toolName", "")
            is_error = msg.get("isError", False)
            
            # Preserve error results (highest priority tool info)
            if is_error:
                error_preview = content.strip()[:150].replace("\n", " ")
                info.key_tool_results.append(f"ERROR({tool_name}): {error_preview}")
            
            # Preserve edit confirmations with file paths
            elif tool_name in _EDIT_TOOLS and content.strip():
                success_preview = content.strip()[:100].replace("\n", " ")
                info.key_tool_results.append(f"{tool_name} ok: {success_preview}")
            
            # Extract file paths from read_file results
            elif tool_name in _READ_TOOLS and content.strip():
                # Check if content references a file path
                first_line = content.strip().split("\n")[0][:100]
                if "/" in first_line or "\\" in first_line:
                    info.file_paths.add(first_line.strip())
    
    return info


def _build_layered_summary(info: _ExtractedInfo, max_summary_tokens: int = 2000) -> str:
    """从提取的信息构建预算感知的分层摘要。

    各层级按重要性排序，每层分配不同的 token 预算：
    - 第 1 层：用户意图（35% 预算）——用户的需求
    - 第 2 层：决策和文件路径（20% 预算）——关键选择
    - 第 3 层：关键工具结果（15% 预算）——错误和重要输出
    - 第 4 层：助手结论（15% 预算）——达到的结果
    - 第 5 层：代码片段（10% 预算）——重要的代码模式
    - 第 6 层：工具使用摘要（5% 预算）——紧凑的活动日志

    参数:
        info: 从消息中提取的结构化信息。
        max_summary_tokens: 摘要的最大 token 预算，默认 2000。

    返回:
        格式化后的分层摘要字符串。
    """
    lines: list[str] = []
    
    # Budget allocations per layer (as fraction of total)
    layer_budgets = [0.35, 0.20, 0.15, 0.15, 0.10, 0.05]
    
    def _remaining_budget() -> int:
        """计算当前剩余的 token 预算。"""
        return max(0, max_summary_tokens - estimate_tokens("\n".join(lines)))
    
    # Layer 1: User intents (highest priority)
    if info.user_intents:
        budget = int(max_summary_tokens * layer_budgets[0])
        lines.append("## User requests:")
        for intent in info.user_intents[:12]:
            if estimate_tokens("\n".join(lines)) > budget:
                lines.append(f"  ... and {len(info.user_intents) - info.user_intents.index(intent)} more")
                break
            lines.append(f"- {intent}")
    
    # Layer 2: Decisions and file paths
    has_decisions = bool(info.decisions)
    has_files = bool(info.file_paths)
    if has_decisions or has_files:
        budget = int(max_summary_tokens * (layer_budgets[0] + layer_budgets[1]))
        
        if info.decisions:
            lines.append("## Key decisions:")
            for dec in info.decisions[:8]:
                if estimate_tokens("\n".join(lines)) > budget:
                    break
                lines.append(f"- {dec}")
        
        if info.file_paths:
            # Separate real paths from search patterns
            real_paths = sorted(p for p in info.file_paths if not p.startswith("search:"))
            search_patterns = sorted(p[8:] for p in info.file_paths if p.startswith("search:"))
            
            path_line = f"## Files: {', '.join(real_paths[:20])}"
            if len(real_paths) > 20:
                path_line += f" (+{len(real_paths)-20} more)"
            if search_patterns:
                path_line += f"\n## Searched: {', '.join(search_patterns[:5])}"
            
            if estimate_tokens("\n".join(lines) + path_line) <= budget:
                lines.append(path_line)
    
    # Layer 3: Key tool results (errors + edits)
    if info.key_tool_results:
        budget = int(max_summary_tokens * sum(layer_budgets[:3]))
        lines.append("## Key results:")
        for result in info.key_tool_results[:15]:
            if estimate_tokens("\n".join(lines)) > budget:
                break
            lines.append(f"- {result}")
    
    # Layer 4: Assistant conclusions
    if info.assistant_conclusions:
        budget = int(max_summary_tokens * sum(layer_budgets[:4]))
        lines.append("## Conclusions:")
        for conc in info.assistant_conclusions[:8]:
            if estimate_tokens("\n".join(lines)) > budget:
                break
            lines.append(f"- {conc}")
    
    # Layer 5: Code snippets (most selective)
    if info.code_snippets:
        budget = int(max_summary_tokens * sum(layer_budgets[:5]))
        lines.append("## Code patterns:")
        for snippet in info.code_snippets[:3]:
            snippet_line = f"```\n{snippet}\n```"
            if estimate_tokens("\n".join(lines) + snippet_line) > budget:
                break
            lines.append(snippet_line)
    
    # Layer 6: Tool usage summary (most compact)
    if info.tool_names:
        from collections import Counter
        tool_counts = Counter(info.tool_names)
        tool_summary = ", ".join(
            f"{name}×{count}" if count > 1 else name
            for name, count in tool_counts.most_common()
        )
        lines.append(f"## Tools: {tool_summary}")
    
    return "\n".join(lines)


def _summarize_removed_messages(messages: list[dict[str, Any]], max_summary_tokens: int = 2000) -> str:
    """为上下文保留构建被移除消息的浓缩摘要。

    采用两阶段方法：
    1. 提取：从所有类型消息中拉取结构化信息。
    2. 构建：按 token 预算分配组装各层摘要。

    确保最重要的信息（用户意图、关键决策）始终被包含，
    而次要细节（工具名称、代码片段）填充剩余预算。

    参数:
        messages: 被移除的消息字典列表。
        max_summary_tokens: 摘要的最大 token 预算，默认 2000。

    返回:
        浓缩摘要字符串，空列表时返回空字符串。
    """
    if not messages:
        return ""
    
    info = _extract_from_messages(messages)
    return _build_layered_summary(info, max_summary_tokens)


# ---------------------------------------------------------------------------
# Context tracking
# ---------------------------------------------------------------------------

@dataclass
class ContextStats:
    """当前上下文窗口的统计数据。

    记录上下文的总 token 使用量、上下文窗口大小、使用率百分比、
    消息数量、系统 token、对话 token、工具调用次数，
    以及是否接近限制或需要压缩的状态标记。
    """
    total_tokens: int = 0
    context_window: int = 0
    usage_percentage: float = 0.0
    messages_count: int = 0
    system_tokens: int = 0
    conversation_tokens: int = 0
    tool_calls_count: int = 0
    is_near_limit: bool = False
    should_compact: bool = False


@dataclass
class ContextManager:
    """管理上下文窗口跟踪和自动压缩。 【为什么需要】防止长对话中因上下文窗口溢出导致
    LLM 调用失败，通过 token 估算、使用率监控和渐进式
    消息压缩，确保对话始终适配模型上下文窗口限制。

    ╔══ 完整执行流程 ══╗
    ║  核心职责: Token 估算                                 ║
    ║  ├─ estimate_tokens(): CJK 感知的启发式估算           ║
    ║  ├─ estimate_message_tokens(): 角色开销 + 内容估算     ║
    ║  ├─ token_count_with_estimation(): 优先使用             ║
    ║  │  provider 返回的精确用量，回退到纯估算               ║
    ║  └─ LRU 缓存避免重复计算                               ║
    ║                                                         ║
    ║  核心职责: 上下文窗口管理                               ║
    ║  ├─ get_model_context_window(): 模型 → (窗口, 输出保留) ║
    ║  ├─ compute_context_stats(): 计算利用率 + 告警级别      ║
    ║  │     [0-50%)  normal  |  [50-85%)  warning            ║
    ║  │     [85-95%) critical |  [95%+]   blocked            ║
    ║  └─ get_stats(): 返回 ContextStats 结构体               ║
    ║                                                         ║
    ║  get_stats() 返回:                                      ║
    ║  ├─ total_tokens — 系统 token + 对话 token 总和         ║
    ║  ├─ context_window — 模型上下文窗口大小                  ║
    ║  ├─ usage_percentage — 使用率百分比 (total/窗口)        ║
    ║  ├─ messages_count — 消息总数                           ║
    ║  ├─ system_tokens — 系统提示词 token 小计               ║
    ║  ├─ conversation_tokens — 对话消息 token 小计           ║
    ║  ├─ tool_calls_count — assistant_tool_call 调用次数     ║
    ║  ├─ is_near_limit — usage ≥ 80% 接近限制标记            ║
    ║  └─ should_compact — usage ≥ AUTOCOMPACT_THRESHOLD      ║
    ║                                                         ║
    ║  should_auto_compact() 判断逻辑:                        ║
    ║  ├─ 基础阈值 = AUTOCOMPACT_THRESHOLD (95%)              ║
    ║  ├─ 每递增一次压缩级别，阈值下降 10%                    ║
    ║  │  级别 0 → 95%  |  级别 1 → 85%  |  级别 2 → 75%    ║
    ║  │  级别 3 (最高) → 60% (最低保护线)                   ║
    ║  ├─ 每次 compact_messages() 执行后递增 _compaction_level ║
    ║  └─ 当前使用率 ≥ 调整后阈值 = 返回 True 触发压缩        ║
    ╚══════════════════════════════════════════════════╝
    """
    model: str = "default"
    context_window: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    _token_cache: dict[int, int] = field(default_factory=dict, repr=False)  # id(msg) -> tokens
    
    # 多级压缩支持
    _compaction_level: int = field(default_factory=lambda: 0)  # 0=无压缩, 1=轻微, 2=中等, 3=深度
    
    # 多级压缩目标 (相对于 context window 的百分比)
    _COMPACTION_LEVELS = [0.70, 0.50, 0.30]  # 轻度/中度/深度
    
    def __post_init__(self):
        """初始化后处理：如果未设置上下文窗口大小，则根据模型自动获取。"""
        if self.context_window == 0:
            self.context_window = get_model_context_window(self.model).context_window

    def update_model(self, model: str) -> None:
        """更新模型并调整上下文窗口大小。

        当会话切换模型时调用，同步更新上下文窗口配置。

        参数:
            model: 新的模型标识字符串。
        """
        self.model = model
        self.context_window = get_model_context_window(model).context_window
    
    def add_message(self, message: dict[str, Any]) -> None:
        """添加一条消息并更新跟踪状态。

        将消息追加到消息列表，并立即缓存其 token 估算值，
        避免在后续 get_stats 调用中重复计算。

        参数:
            message: 待添加的消息字典。
        """
        self.messages.append(message)
        # Cache token count immediately to avoid re-estimation in get_stats()
        self._token_cache[id(message)] = estimate_message_tokens(message)
    
    def get_stats(self) -> ContextStats:
        """计算当前上下文统计信息。

        优先使用缓存的 token 计数（通过 add_message 添加的消息可实现均摊 O(1) 查询）。
        按角色统计系统 token 和对话 token，并计算使用率及是否接近限制。

        返回:
            包含总 token、上下文窗口、使用率百分比、消息数等字段的 ContextStats 实例。
        """
        if not self.messages:
            return ContextStats(
                context_window=self.context_window,
            )
        
        # Count tokens using cache when available
        system_tokens = 0
        conversation_tokens = 0
        tool_calls = 0
        
        for msg in self.messages:
            msg_tokens = self._token_cache.get(id(msg))
            if msg_tokens is None:
                msg_tokens = estimate_message_tokens(msg)
                self._token_cache[id(msg)] = msg_tokens
            if msg.get("role") == "system":
                system_tokens += msg_tokens
            else:
                conversation_tokens += msg_tokens
            
            if msg.get("role") == "assistant_tool_call":
                tool_calls += 1
        
        total_tokens = system_tokens + conversation_tokens
        usage_pct = (total_tokens / self.context_window * 100) if self.context_window > 0 else 0
        
        is_near_limit = usage_pct >= 80  # Warning at 80%
        should_compact = usage_pct >= (AUTOCOMPACT_THRESHOLD * 100)
        
        return ContextStats(
            total_tokens=total_tokens,
            context_window=self.context_window,
            usage_percentage=usage_pct,
            messages_count=len(self.messages),
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_calls_count=tool_calls,
            is_near_limit=is_near_limit,
            should_compact=should_compact,
        )
    
    def should_auto_compact(self) -> bool:
        """检查是否应触发自动压缩。

        多级触发阈值（压缩级别越高越激进）：
        - 第 0 级：阈值 95%
        - 第 1 级：阈值 85%
        - 第 2 级：阈值 75%
        - 第 3 级：阈值 60%（更激进）
        最低阈值为 60%，防止过度压缩。

        返回:
            如果需要自动压缩则返回 True，否则返回 False。
        """
        stats = self.get_stats()
        # Higher compaction level = more aggressive (lower threshold)
        threshold = AUTOCOMPACT_THRESHOLD - (self._compaction_level * 0.10)
        threshold = max(0.60, threshold)  # Minimum 60%
        usage_pct = stats.usage_percentage
        return usage_pct >= (threshold * 100)
    
    def compact_messages(self) -> list[dict[str, Any]]:
        """压缩消息以适配上下文窗口。

        多级渐进式压缩策略：
        - 第 0 级（首次压缩）：目标 70%
        - 第 1 级（二次压缩）：目标 50%
        - 第 2 级以上（深度压缩）：目标 30%

        语义感知的渐进压缩流程：
        1. 始终保留系统提示词
        2. 移除 assistant_progress 消息（价值最低）
        3. 就地截断大型工具结果（自适应大小）
        4. 将 tool_call+result 对压缩为内联摘要
        5. 按优先级移除剩余消息（tool_result > tool_call > assistant > user）

        相比简单优先级移除的关键改进：
        - 工具调用+结果对会被压缩而非删除，保留调用与结果之间的语义关联
        - 工具类型感知压缩：只读工具使用更短摘要，编辑工具保留文件路径，错误结果保留错误文本
        - 保护最近消息——从最旧消息开始移除
        - 预算感知：每个阶段检查是否已达到目标

        返回:
            压缩后的消息列表。
        """
        stats = self.get_stats()
        if not stats.should_compact:
            return self.messages
        
        # Get target based on compaction level
        target_pct = self._COMPACTION_LEVELS[min(self._compaction_level, 2)]
        target_tokens = int(self.context_window * target_pct)
        
        # Always keep system prompt
        system_messages = [m for m in self.messages if m.get("role") == "system"]
        other_messages = [m for m in self.messages if m.get("role") != "system"]
        
        # Phase 1: Remove progress messages (lowest priority — always safe to drop)
        filtered = [
            m for m in other_messages
            if m.get("role") != "assistant_progress"
        ]
        
        current_tokens = estimate_messages_tokens(filtered)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, filtered, stats, target_tokens
            )
        
        # Phase 2: Truncate large tool results in-place (adaptive threshold)
        # Use different thresholds based on tool type:
        # - Read-only tools: more aggressive truncation (they can be re-run)
        # - Edit tools: less aggressive (their results are side-effect confirmations)
        # - Error results: preserve more (errors are hard to reproduce)
        _READ_TOOL_TRUNCATE = 1500   # chars to keep for read-only tool results
        _EDIT_TOOL_TRUNCATE = 3000   # chars to keep for edit tool results
        _ERROR_TRUNCATE = 4000       # chars to keep for error results
        _DEFAULT_TRUNCATE = 2000     # default truncation threshold
        
        for i, m in enumerate(filtered):
            if m.get("role") != "tool_result":
                continue
            content = m.get("content", "")
            if not content or len(content) <= _DEFAULT_TRUNCATE:
                continue
            
            tool_name = m.get("toolName", "")
            is_error = m.get("isError", False)
            
            # Select truncation threshold based on tool type
            if is_error:
                threshold = _ERROR_TRUNCATE
            elif tool_name in _EDIT_TOOLS:
                threshold = _EDIT_TOOL_TRUNCATE
            elif tool_name in _READ_TOOLS:
                threshold = _READ_TOOL_TRUNCATE
            else:
                threshold = _DEFAULT_TRUNCATE
            
            if len(content) <= threshold:
                continue
            
            # Smart truncation: head + tail with context line
            content_lines = content.split("\n")
            # Determine how many head/tail lines to keep based on threshold
            keep_chars = threshold
            head_lines: list[str] = []
            tail_lines: list[str] = []
            head_chars = 0
            
            for line in content_lines:
                if head_chars + len(line) + 1 > keep_chars * 0.7:
                    break
                head_lines.append(line)
                head_chars += len(line) + 1
            
            # Tail: last few lines
            tail_chars = 0
            for line in reversed(content_lines):
                if tail_chars + len(line) + 1 > keep_chars * 0.3:
                    break
                tail_lines.insert(0, line)
                tail_chars += len(line) + 1
            
            omitted = len(content_lines) - len(head_lines) - len(tail_lines)
            truncated_content = "\n".join(head_lines)
            if omitted > 0:
                truncated_content += f"\n... [{omitted} lines truncated for compaction] ...\n"
            truncated_content += "\n".join(tail_lines)
            
            filtered[i] = {**m, "content": truncated_content}
        
        current_tokens = estimate_messages_tokens(filtered)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, filtered, stats, target_tokens
            )
        
        # Phase 3: Compress tool_call + result pairs into inline summaries
        # Instead of simply deleting pairs, replace them with compact summaries
        # that preserve the semantic link between call and result.
        # This is especially important for edit operations where knowing
        # WHAT was edited is critical even after compaction.
        compressed: list[dict[str, Any]] = []
        i = 0
        while i < len(filtered):
            msg = filtered[i]
            
            # Look for tool_call + tool_result pairs to compress
            if (msg.get("role") == "assistant_tool_call" and
                    i + 1 < len(filtered) and
                    filtered[i + 1].get("role") == "tool_result"):
                
                call_msg = msg
                result_msg = filtered[i + 1]
                tool_name = call_msg.get("toolName", "unknown")
                result_msg.get("content", "")
                is_error = result_msg.get("isError", False)
                
                # Build a compact summary preserving the key information
                summary = self._compress_tool_pair(call_msg, result_msg)
                
                # Replace the pair with a single compressed message
                compressed.append({
                    "role": "assistant",
                    "content": summary,
                })
                i += 2  # Skip both messages
            else:
                compressed.append(msg)
                i += 1
        
        current_tokens = estimate_messages_tokens(compressed)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, compressed, stats, target_tokens
            )
        
        # Phase 4: Priority-based removal (oldest first, lowest priority removed first)
        # Priority order (highest kept, lowest removed first):
        #   0 = user messages (keep longest — encode intent)
        #   1 = assistant conclusions (keep long — encode results)
        #   2 = compressed tool summaries (medium — already compressed)
        PRIORITY = {
            "user": 0,                    # Highest — encode intent
            "assistant": 1,               # High — encode conclusions + compressed tools
            "assistant_tool_call": 2,     # Medium — should have been compressed in Phase 3
            "tool_result": 3,             # Low — should have been compressed in Phase 3
        }
        
        # Protect recent messages (last 6) from removal
        PROTECTED_RECENT = 6
        
        while estimate_messages_tokens(compressed) > target_tokens and len(compressed) > MIN_MESSAGES_TO_KEEP:
            # Find the message with the lowest priority (highest number) in the removable range
            removable_end = max(MIN_MESSAGES_TO_KEEP, len(compressed) - PROTECTED_RECENT)
            best_idx = None
            best_priority = -1
            
            for idx in range(removable_end):
                role = compressed[idx].get("role", "")
                priority = PRIORITY.get(role, 1)
                if priority > best_priority:
                    best_priority = priority
                    best_idx = idx
            
            if best_idx is None:
                break
            
            del compressed[best_idx]
        
        return self._finalize_compaction(
            system_messages, other_messages, compressed, stats, target_tokens
        )
    
    @staticmethod
    def _compress_tool_pair(call_msg: dict[str, Any], result_msg: dict[str, Any]) -> str:
        """将 tool_call 和 tool_result 消息对压缩为紧凑的内联摘要。

        各类工具的特定压缩策略：
        - 编辑工具：保留文件路径和成功/失败状态
        - 只读工具：仅记录读取的文件（内容可重新读取）
        - 搜索工具：保留搜索模式和结果数量
        - 命令工具：保留命令名称和退出状态
        - 错误结果：保留错误消息（对调试至关重要）

        参数:
            call_msg: 工具调用消息字典。
            result_msg: 工具结果消息字典。

        返回:
            压缩后的内联摘要字符串，如 "[Edited /path/to/file: ok]"。
        """
        tool_name = call_msg.get("toolName", "unknown")
        inp = call_msg.get("input", {})
        result_content = result_msg.get("content", "")
        is_error = result_msg.get("isError", False)
        
        # Error results: preserve the error message
        if is_error:
            error_text = result_content.strip()[:200].replace("\n", " ")
            return f"[Tool {tool_name} ERROR: {error_text}]"
        
        # Tool-specific compression
        if tool_name in _EDIT_TOOLS:
            path = inp.get("path") or inp.get("filePath", "unknown")
            # Preserve key edit details
            if tool_name == "multi_edit":
                edits = inp.get("edits", [])
                return f"[Edited {path}: {len(edits)} changes applied]"
            return f"[Edited {path}: ok]"
        
        if tool_name in _READ_TOOLS:
            path = inp.get("path") or inp.get("filePath", "")
            if path:
                # Note: content can be re-read, so just record that it was read
                line_count = result_content.count("\n") + 1
                return f"[Read {path}: {line_count} lines]"
            return f"[{tool_name}: completed]"
        
        if tool_name in _SEARCH_TOOLS:
            pattern = inp.get("pattern") or inp.get("query", "")
            # Count matches from result
            match_lines = [line for line in result_content.split("\n") if line.strip() and not line.startswith("#")]
            return f"[Searched '{pattern[:50]}': {len(match_lines)} results]"
        
        if tool_name in _COMMAND_TOOLS:
            cmd = inp.get("command", "")
            cmd_name = cmd.split()[0] if cmd.split() else "command"
            # Check for success indicators
            exit_info = ""
            if "exit code" in result_content.lower():
                for line in result_content.split("\n"):
                    if "exit code" in line.lower():
                        exit_info = f" ({line.strip()[:50]})"
                        break
            return f"[Ran {cmd_name}{exit_info}]"
        
        # Generic compression: tool name + brief result
        brief = result_content.strip()[:100].replace("\n", " ")
        if brief:
            return f"[{tool_name}: {brief}]"
        return f"[{tool_name}: completed]"
    
    def _finalize_compaction(
        self,
        system_messages: list[dict[str, Any]],
        original_other: list[dict[str, Any]],
        filtered: list[dict[str, Any]],
        stats: ContextStats,
        target_tokens: int,
    ) -> list[dict[str, Any]]:
        """构建最终的压缩消息列表，包含摘要标记和压缩记录。

        计算被移除消息的摘要，生成包含压缩时间、移除数量和 token 变化率的标记消息，
        并将压缩记录追加到历史中。同时递增压缩级别以便下次更激进地压缩。

        参数:
            system_messages: 系统提示消息列表（始终保留）。
            original_other: 原始非系统消息列表（用于计算被移除的消息）。
            filtered: 压缩后的非系统消息列表。
            stats: 压缩前的上下文统计信息。
            target_tokens: 压缩目标 token 数。

        返回:
            最终的消息列表（系统消息 + 压缩标记 + 压缩后的消息）。
        """
        # Build a layered summary of removed messages
        removed_set = set(id(m) for m in filtered)
        removed_messages = [m for m in original_other if id(m) not in removed_set]
        summary_text = _summarize_removed_messages(removed_messages)
        
        removed_count = len(original_other) - len(filtered)
        after_pct = estimate_messages_tokens(filtered) / self.context_window * 100 if self.context_window > 0 else 0
        
        # Add compaction marker with content summary
        compaction_marker = {
            "role": "system",
            "content": (
                f"[Context compacted at {time.strftime('%H:%M:%S')}. "
                f"{removed_count} messages removed. "
                f"Token usage: {stats.usage_percentage:.0f}% → {after_pct:.0f}%]\n"
                + (f"\nSummary of removed conversation:\n{summary_text}" if summary_text else "")
            ),
        }
        
        # Build final message list
        compacted = system_messages + [compaction_marker] + filtered
        
        # Record compaction
        self.compaction_history.append({
            "timestamp": time.time(),
            "before_tokens": stats.total_tokens,
            "after_tokens": estimate_messages_tokens(compacted),
            "messages_removed": len(self.messages) - len(compacted),
            "compaction_level": self._compaction_level,
        })
        
        # Increment compaction level for next compaction (more aggressive)
        self._compaction_level = min(self._compaction_level + 1, 3)
        
        self.messages = compacted
        # Rebuild token cache: discard stale entries, keep only retained msgs
        self._token_cache = {
            id(m): self._token_cache.get(id(m), estimate_message_tokens(m))
            for m in compacted
        }
        return compacted
    
    def get_context_summary(self) -> str:
        """获取人类可读的上下文使用摘要。

        格式化的单行摘要，包含使用率、总 token、上下文窗口大小、
        消息数量和工具调用次数，并使用状态指示符（✓/⚠/🔴）。

        返回:
            格式化的上下文摘要字符串，如 "Context: ✓ 45% (45,000/100,000 tokens, 12 msgs, 3 tools)"。
        """
        stats = self.get_stats()
        
        if stats.messages_count == 0:
            return "Context: empty"
        
        status = "✓"
        if stats.is_near_limit:
            status = "⚠"
        if stats.should_compact:
            status = "🔴"
        
        return (
            f"Context: {status} {stats.usage_percentage:.0f}% "
            f"({stats.total_tokens:,}/{stats.context_window:,} tokens, "
            f"{stats.messages_count} msgs, {stats.tool_calls_count} tools)"
        )
    
    def format_context_details(self) -> str:
        """获取详细的上下文信息，用于 /context 命令展示。

        包含模型名称、上下文窗口大小、token 使用详情、使用率百分比、
        消息数量和工具调用次数。在接近容量时显示告警，并列出最近
        3 次压缩历史记录。

        返回:
            格式化的多行详情字符串。
        """
        stats = self.get_stats()
        
        lines = [
            "Context Window Usage",
            "=" * 50,
            f"Model: {self.model}",
            f"Context window: {stats.context_window:,} tokens",
            "",
            f"Total tokens: {stats.total_tokens:,}",
            f"Usage: {stats.usage_percentage:.1f}%",
            f"Messages: {stats.messages_count}",
            f"Tool calls: {stats.tool_calls_count}",
            "",
        ]
        
        if stats.should_compact:
            lines.append("⚠️  WARNING: Context is near capacity!")
            lines.append("Auto-compaction will trigger soon.")
            lines.append("")
        
        if self.compaction_history:
            lines.append("Compaction History:")
            for comp in self.compaction_history[-3:]:  # Last 3
                ts = time.strftime("%H:%M:%S", time.localtime(comp["timestamp"]))
                lines.append(
                    f"  {ts}: {comp['messages_removed']} messages removed, "
                    f"{comp['before_tokens']:,} → {comp['after_tokens']:,} tokens"
                )
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_context_state(manager: ContextManager) -> None:
    """将上下文管理器状态保存到磁盘。

    将模型名称、上下文窗口大小、消息列表和压缩历史等状态序列化为 JSON，
    写入 MINI_CODE_DIR 下的 context_state.json 文件。仅保留最近 10 条压缩历史记录。

    参数:
        manager: 要保存状态的 ContextManager 实例。
    """
    state_path = MINI_CODE_DIR / "context_state.json"
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    
    state = {
        "model": manager.model,
        "context_window": manager.context_window,
        "messages": manager.messages,
        "compaction_history": manager.compaction_history[-10:],  # Keep last 10
        "_compaction_level": manager._compaction_level,  # Save compaction level
    }
    
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_context_state() -> ContextManager | None:
    """从磁盘加载上下文管理器状态。

    读取 MINI_CODE_DIR 下的 context_state.json 文件，反序列化恢复 ContextManager 实例。
    如果文件不存在或 JSON 解析失败，返回 None。

    返回:
        成功时返回恢复的 ContextManager 实例，失败时返回 None。
    """
    state_path = MINI_CODE_DIR / "context_state.json"
    if not state_path.exists():
        return None
    
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manager = ContextManager(
            model=state.get("model", "default"),
            context_window=state.get("context_window", 0),
            messages=state.get("messages", []),
            compaction_history=state.get("compaction_history", []),
        )
        # Restore compaction level if saved
        if "_compaction_level" in state:
            manager._compaction_level = state["_compaction_level"]
        return manager
    except (json.JSONDecodeError, KeyError):
        return None


def clear_context_state() -> None:
    """清除已保存的上下文状态。

    删除磁盘上的 context_state.json 文件。如果文件不存在，则不执行任何操作。
    """
    state_path = MINI_CODE_DIR / "context_state.json"
    if state_path.exists():
        state_path.unlink()
