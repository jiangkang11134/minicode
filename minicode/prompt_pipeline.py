"""MiniCode 动态提示词组装管道。

实现段落级别的提示词组装，包含缓存边界和条件节，遵循 Learn Claude Code 最佳实践。

核心概念：
- SYSTEM_PROMPT_DYNAMIC_BOUNDARY：分隔静态前缀（可缓存）与动态后缀（会话特定），
  实现跨会话的 API 提示词缓存。
- PromptSection：声明式段落注册，包含名称、条件和构建器属性。
- 段落级缓存：避免每轮重新读取 CLAUDE.md、skills 等文件。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 分隔静态部分和动态部分的哨兵字符串。
# API 提供商（Anthropic, OpenAI）利用此标记实现 prompt caching。
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


@dataclass
class PromptSection:
    """声明式提示词段落，支持条件包含和缓存。

    每个段落包含名称、构建函数、可选条件判断以及 TTL 缓存机制。
    evaluate() 方法根据条件返回内容，并利用缓存避免重复构建。
    """

    name: str
    builder: Callable[[], str]
    condition: Callable[[], bool] | None = None
    cache_ttl: float = 300.0  # 默认缓存 5 分钟
    _cached_value: str | None = field(default=None, repr=False)
    _cached_at: float = field(default=0.0, repr=False)

    def evaluate(self) -> str | None:
        """评估段落内容。

        如果条件函数存在且返回 False，返回 None 表示跳过该段落。
        否则优先返回缓存内容（在 TTL 有效期内），
        缓存过期或不存在时调用 builder() 重建并更新缓存。

        返回:
            段落文本字符串，如果条件不满足则返回 None。
        """
        if self.condition is not None and not self.condition():
            return None

        # 检查缓存
        now = time.monotonic()
        if self._cached_value is not None and (now - self._cached_at) < self.cache_ttl:
            return self._cached_value

        # 构建并缓存
        text = self.builder()
        self._cached_value = text
        self._cached_at = now
        return text


class PromptPipeline:
    """管理提示词段落的生命周期，包含缓存边界标记。

    将提示词分为静态前缀（跨会话可缓存）和动态后缀（每轮重新评估），
    中间插入 SYSTEM_PROMPT_DYNAMIC_BOUNDARY 标记供 API 缓存使用。

    用法:
        pipeline = PromptPipeline()
        pipeline.register_static("role", "You are an AI assistant...")
        pipeline.register_dynamic(
            "skills",
            lambda: get_skills_text(skills),
            condition=lambda: len(skills) > 0,
        )
        prompt = pipeline.build()
    """

    def __init__(self) -> None:
        """初始化管道，创建静态和动态段落空列表。"""
        self._static_sections: list[PromptSection] = []
        self._dynamic_sections: list[PromptSection] = []

    def register_static(self, name: str, text: str) -> None:
        """注册一个从不变化的段落（完全可缓存）。

        静态段落的内容在管道生命周期内不变，缓存永不过期。
        适合注册角色定义、全局指令等固定内容。

        参数:
            name: 段落名称，用于标识。
            text: 段落文本内容。
        """
        self._static_sections.append(
            PromptSection(
                name=name,
                builder=lambda: text,
                cache_ttl=float("inf"),  # 永不过期
            )
        )

    def register_dynamic(
        self,
        name: str,
        builder: Callable[[], str],
        condition: Callable[[], bool] | None = None,
        cache_ttl: float = 300.0,
    ) -> None:
        """注册一个可能在轮次之间变化的段落。

        动态段落每次构建时重新评估条件，内容可随上下文变化。
        适合注册 skills 列表、CLAUDE.md 内容等会话特定信息。

        参数:
            name: 段落名称，用于标识。
            builder: 构建段落文本的可调用函数。
            condition: 可选条件函数，返回 True 时该段落才被包含。
            cache_ttl: 缓存有效期（秒），默认 300 秒（5 分钟）。
        """
        self._dynamic_sections.append(
            PromptSection(
                name=name,
                builder=builder,
                condition=condition,
                cache_ttl=cache_ttl,
            )
        )

    def build(self) -> str:
        """组装完整的系统提示词，包含缓存边界标记。

        返回:
            组装后的完整提示词字符串。
        """
        return self._build_cached()

    def _build_cached(self) -> str:
        """内部方法：组装系统提示词并插入缓存边界标记。

        按顺序合并所有静态段落，插入边界标记，再合并所有动态段落。
        段落之间用双换行分隔。

        返回:
            组装后的完整提示词字符串。
        """
        parts: list[str] = []

        # 静态前缀（跨会话/轮次可缓存）
        for section in self._static_sections:
            text = section.evaluate()
            if text:
                parts.append(text)

        # 动态边界标记
        parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)

        # 动态后缀（每轮重新评估）
        for section in self._dynamic_sections:
            text = section.evaluate()
            if text:
                parts.append(text)

        return "\n\n".join(p for p in parts if p)

    def clear_cache(self) -> None:
        """清除所有段落的缓存，强制下次 build() 时重新构建。"""
        for section in self._static_sections + self._dynamic_sections:
            section._cached_value = None
            section._cached_at = 0.0


# ---------------------------------------------------------------------------
# 基于文件的缓存，用于昂贵的段落构建器（CLAUDE.md 等）
# ---------------------------------------------------------------------------

_file_cache: dict[str, tuple[str, float, float]] = {}


def read_file_cached(path: Path, ttl: float = 300.0) -> str | None:
    """读取文件内容，基于 mtime 实现缓存。

    通过文件修改时间（mtime）判断缓存是否过期。
    如果文件不存在或无法读取，返回 None。

    参数:
        path: 要读取的文件路径。
        ttl: 缓存有效期（秒），默认 300 秒。

    返回:
        文件内容的字符串，如果文件不存在则返回 None。
    """
    key = str(path.resolve())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    if key in _file_cache:
        cached_text, cached_mtime, cached_at = _file_cache[key]
        if mtime == cached_mtime and (time.monotonic() - cached_at) < ttl:
            return cached_text

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    _file_cache[key] = (text, mtime, time.monotonic())
    return text


def content_hash(text: str) -> str:
    """计算内容的短哈希值，用于缓存失效判断。

    SHA-256 哈希的前 12 个十六进制字符，兼顾唯一性和可读性。

    参数:
        text: 要计算哈希的文本内容。

    返回:
        12 字符的十六进制哈希字符串。
    """
    return hashlib.sha256(text.encode()).hexdigest()[:12]
