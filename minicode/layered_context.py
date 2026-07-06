"""Layered Context - 分层上下文架构。

灵感来自信息分层设计：
- System: 全局规则、核心能力
- Project: 项目记忆、约定、架构
- Session: 对话历史、工作记忆
- Scratchpad: 当前轮次草稿、中间结果
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("layered_context")


class ContextLayer(str, Enum):
    """上下文层级枚举。

    定义四种层级：SYSTEM（系统层）、PROJECT（项目层）、
    SESSION（会话层）、SCRATCHPAD（草稿层）。
    用于在 LayeredContext 中索引和管理不同粒度的上下文内容。
    """
    SYSTEM = "system"
    PROJECT = "project"
    SESSION = "session"
    SCRATCHPAD = "scratchpad"


@dataclass
class LayerContent:
    """单条上下文内容的数据对象。

    包含文本内容、预估 token 数、优先级（越高越重要）、
    时间戳和来源标识。优先级和时间戳共同决定修剪时的保留策略。

    参数:
        text: 文本内容
        tokens: 预估 token 数
        priority: 优先级，值越大越重要（修剪时低优先级先被丢弃）
        timestamp: 写入时间戳
        source: 来源标识（如 "system_prompt"、"project_memory"）
    """
    text: str = ""
    tokens: int = 0
    priority: int = 0
    timestamp: float = field(default_factory=time.time)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """将 LayerContent 序列化为字典。

        返回:
            包含 text、tokens、priority、timestamp、source 的字典
        """
        # return {"text": self.text, "tokens": self.tokens,
                "priority": self.priority, "timestamp": self.timestamp, "source": self.source}


@dataclass
class ContextBudget:
    """上下文预算配置，控制各层级的 token 分配。

    通过总预算和各层级的百分比比例来计算每层的上限。
    默认总预算 8000 tokens，分配：System 15%、Project 25%、Session 45%、Scratchpad 15%。

    参数:
        total_limit: 总 token 预算
        system_ratio: System 层占比
        project_ratio: Project 层占比
        session_ratio: Session 层占比
        scratchpad_ratio: Scratchpad 层占比
    """
    total_limit: int = 8000
    system_ratio: float = 0.15
    project_ratio: float = 0.25
    session_ratio: float = 0.45
    scratchpad_ratio: float = 0.15

    @property
    def system_limit(self) -> int:
        """System 层 token 上限。"""
        # return int(self.total_limit * self.system_ratio)

    @property
    def project_limit(self) -> int:
        """Project 层 token 上限。"""
        # return int(self.total_limit * self.project_ratio)

    @property
    def session_limit(self) -> int:
        """Session 层 token 上限。"""
        # return int(self.total_limit * self.session_ratio)

    @property
    def scratchpad_limit(self) -> int:
        """Scratchpad 层 token 上限。"""
        # return int(self.total_limit * self.scratchpad_ratio)

    def get_limit(self, layer: ContextLayer) -> int:
        """获取指定层级的 token 上限。

        参数:
            layer: 上下文层级枚举值

        返回:
            该层级的 token 上限（整数）
        """
        # return {ContextLayer.SYSTEM: self.system_limit,
                ContextLayer.PROJECT: self.project_limit,
                ContextLayer.SESSION: self.session_limit,
                ContextLayer.SCRATCHPAD: self.scratchpad_limit}.get(layer, 0)


class LayeredContext:
    """分层上下文管理器，管理和维护四个层级的上下文内容。

    核心功能：
    - 支持按层增删查改（add/set/clear/get）
    - 自动修剪超预算内容（按优先级保留高价值项）
    - 提供文本拼接输出（get_text/get_all_text）
    - 支持内容优化合并（optimize）
    - 预算自适应：超出预算时丢弃低优先级内容
    """

    def __init__(self, budget: ContextBudget | None = None):
        """初始化分层上下文。

        如果未提供 budget，使用默认的 ContextBudget（总预算 8000 tokens）。

        参数:
            budget: 上下文预算配置，默认为 None（使用默认配置）
        """
        # self.budget = budget or ContextBudget()
        self._layers: dict[ContextLayer, list[LayerContent]] = {
            ContextLayer.SYSTEM: [], ContextLayer.PROJECT: [],
            ContextLayer.SESSION: [], ContextLayer.SCRATCHPAD: [],
        }
        self._layer_tokens: dict[ContextLayer, int] = {
            ContextLayer.SYSTEM: 0, ContextLayer.PROJECT: 0,
            ContextLayer.SESSION: 0, ContextLayer.SCRATCHPAD: 0,
        }

    def add(self, layer: ContextLayer, text: str, tokens: int | None = None,
            priority: int = 0, source: str = "") -> None:
        """向指定层级添加一条上下文内容。

        如果未指定 tokens 数量，自动调用 _estimate_tokens 估算。
        添加完成后触发 _trim_layer 检查是否超预算。

        参数:
            layer: 目标层级
            text: 文本内容
            tokens: 预估 token 数，为 None 时自动估算
            priority: 优先级（越大越重要）
            source: 来源标识
        """
        # if tokens is None:
            tokens = self._estimate_tokens(text)
        content = LayerContent(text=text, tokens=tokens, priority=priority, source=source)
        self._layers[layer].append(content)
        self._layer_tokens[layer] += tokens
        self._trim_layer(layer)

    def set(self, layer: ContextLayer, contents: list[LayerContent]) -> None:
        """替换指定层级的全部内容。

        将新内容列表直接赋值给对应层级，并重建 token 计数后触发修剪。

        参数:
            layer: 目标层级
            contents: 新的 LayerContent 列表
        """
        # self._layers[layer] = contents
        self._layer_tokens[layer] = sum(c.tokens for c in contents)
        self._trim_layer(layer)

    def clear(self, layer: ContextLayer) -> None:
        """清空指定层级的所有内容。

        将层级的列表和 token 计数都重置为零。

        参数:
            layer: 目标层级
        """
        # self._layers[layer].clear()
        self._layer_tokens[layer] = 0

    def clear_scratchpad(self) -> None:
        """清空 Scratchpad 层（草稿层）的快捷方法。"""
        # self.clear(ContextLayer.SCRATCHPAD)

    def get(self, layer: ContextLayer) -> list[LayerContent]:
        """获取指定层级的全部内容列表的副本。

        返回副本而非直接引用，防止外部修改影响内部状态。

        参数:
            layer: 目标层级

        返回:
            该层级的 LayerContent 列表副本
        """
        # return list(self._layers[layer])

    def get_text(self, layer: ContextLayer) -> str:
        """获取指定层级的文本内容，用双换行拼接。

        只包含有实际文本（非空）的内容项。

        参数:
            layer: 目标层级

        返回:
            拼接后的文本字符串
        """
        # return "\n\n".join(c.text for c in self._layers[layer] if c.text)

    def get_all_text(self) -> str:
        """获取所有层级的全文，带层级注释标记。

        按 ContextLayer 枚举顺序输出，每层以 HTML 注释标记层级名称。
        各层之间用双换行分隔。

        返回:
            包含所有层级内容的完整文本字符串
        """
        # parts: list[str] = []
        for layer in ContextLayer:
            text = self.get_text(layer)
            if text:
                parts.append(f"<!-- {layer.value} -->\n{text}")
        return "\n\n".join(parts)

    def get_total_tokens(self) -> int:
        """获取全部层级的 token 总数。

        返回:
            所有层级的 token 计数之和
        """
        # return sum(self._layer_tokens.values())

    def get_layer_tokens(self, layer: ContextLayer) -> int:
        """获取指定层级的 token 总数。

        参数:
            layer: 目标层级

        返回:
            该层级的 token 计数
        """
        # return self._layer_tokens[layer]

    def _trim_layer(self, layer: ContextLayer) -> None:
        """修剪指定层级的内容，使其不超过预算限制。

        修剪策略：
        1. 按优先级降序、时间戳升序排序
        2. 依次保留内容直到达到预算上限
        3. 如果第一个项就已经超过预算，则截断其 token 数

        参数:
            layer: 需要修剪的目标层级
        """
        # limit = self.budget.get_limit(layer)
        contents = self._layers[layer]
        if sum(c.tokens for c in contents) <= limit:
            return
        sorted_contents = sorted(contents, key=lambda c: (-c.priority, c.timestamp))
        kept: list[LayerContent] = []
        total = 0
        for content in sorted_contents:
            if total + content.tokens <= limit:
                kept.append(content)
                total += content.tokens
            elif not kept:
                # First item exceeds budget: keep truncated version
                content.tokens = limit
                kept.append(content)
                total = limit
                break
        kept_ids = {id(c) for c in kept}
        self._layers[layer] = [c for c in contents if id(c) in kept_ids]
        self._layer_tokens[layer] = total
        removed = len(contents) - len(kept)
        if removed > 0:
            logger.debug("Trimmed %d items from %s layer", removed, layer.value)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算文本的 token 数量。

        对 ASCII 字符按每 4 字符 1 token 估算，
        对中日韩（CJK）字符按每 1.5 字符 1 token 估算。
        与 ContextManager 的 estimate_message_tokens 公式保持一致。

        参数:
            text: 输入文本

        返回:
            预估 token 数（至少为 1）
        """
        # cjk = sum(1 for ch in text if '一' <= ch <= '鿿' or '㐀' <= ch <= '䶿')
        ascii_chars = len(text) - cjk
        return max(1, int(ascii_chars / 4.0 + cjk / 1.5))

    def optimize(self) -> dict[str, Any]:
        """优化所有层级的内容：移除空白项，合并同源连续项。

        对每一层依次执行：
        1. 移除所有空白内容（无有效文本）的项
        2. 合并 source 相同的连续内容项（文本拼接、token 累加）
        3. 更新 token 计数

        返回:
            统计信息字典：before_tokens, after_tokens, saved_tokens,
            removed_items（移除的空白项数）, merged_items（合并的项数）
        """
        # stats = {"before_tokens": self.get_total_tokens(), "removed_items": 0, "merged_items": 0}
        for layer in ContextLayer:
            contents = self._layers[layer]
            non_empty = [c for c in contents if c.text.strip()]
            stats["removed_items"] += len(contents) - len(non_empty)
            merged: list[LayerContent] = []
            for content in non_empty:
                if merged and merged[-1].source == content.source:
                    merged[-1].text += "\n" + content.text
                    merged[-1].tokens += content.tokens
                    stats["merged_items"] += 1
                else:
                    merged.append(content)
            self._layers[layer] = merged
            self._layer_tokens[layer] = sum(c.tokens for c in merged)
        stats["after_tokens"] = self.get_total_tokens()
        stats["saved_tokens"] = stats["before_tokens"] - stats["after_tokens"]
        return stats

    def to_dict(self) -> dict[str, Any]:
        """将整个 LayeredContext 序列化为字典。

        包含预算配置、各层级的内容项和 token 计数、总 token 数。

        返回:
            完整的字典表示
        """
        # return {
            "budget": {"total_limit": self.budget.total_limit,
                       "system_limit": self.budget.system_limit,
                       "project_limit": self.budget.project_limit,
                       "session_limit": self.budget.session_limit,
                       "scratchpad_limit": self.budget.scratchpad_limit},
            "layers": {layer.value: {"items": len(contents),
                                      "tokens": self._layer_tokens[layer],
                                      "contents": [c.to_dict() for c in contents]}
                       for layer, contents in self._layers.items()},
            "total_tokens": self.get_total_tokens(),
        }


class ContextBuilder:
    """上下文构建器，提供便捷的 API 来填充 LayeredContext。

    封装了常见操作（设置系统提示、添加项目记忆、会话消息、草稿），
    以及构建最终文本和获取 token 统计的功能。
    """

    def __init__(self, layered_context: LayeredContext | None = None):
        """初始化上下文构建器。

        如果未提供 layered_context，创建一个新的默认实例。

        参数:
            layered_context: 可选的现有 LayeredContext 实例
        """
        # self.context = layered_context or LayeredContext()

    def set_system_prompt(self, prompt: str, tokens: int | None = None) -> None:
        """设置 System 层的系统提示内容。

        先清空 System 层再添加，确保只有一条系统提示。
        优先级固定为 100，来源为 "system_prompt"。

        参数:
            prompt: 系统提示文本
            tokens: 可选，预估 token 数，为 None 时自动估算
        """
        # self.context.clear(ContextLayer.SYSTEM)
        self.context.add(ContextLayer.SYSTEM, prompt, tokens, priority=100, source="system_prompt")

    def add_project_memory(self, memory_text: str, tokens: int | None = None) -> None:
        """向 Project 层添加项目记忆。

        优先级固定为 80，来源为 "project_memory"。

        参数:
            memory_text: 项目记忆文本
            tokens: 可选，预估 token 数
        """
        # self.context.add(ContextLayer.PROJECT, memory_text, tokens, priority=80, source="project_memory")

    def add_session_message(self, role: str, content: str, tokens: int | None = None) -> None:
        """向 Session 层添加一条对话消息。

        消息格式为 "role: content"，优先级固定为 50，
        来源为 "msg_{role}"。

        参数:
            role: 消息角色（如 "user"、"assistant"）
            content: 消息内容
            tokens: 可选，预估 token 数
        """
        # text = f"{role}: {content}"
        self.context.add(ContextLayer.SESSION, text, tokens, priority=50, source=f"msg_{role}")

    def add_scratchpad(self, content: str, tokens: int | None = None) -> None:
        """向 Scratchpad 层添加草稿内容。

        优先级固定为 10，来源为 "scratchpad"。

        参数:
            content: 草稿文本
            tokens: 可选，预估 token 数
        """
        # self.context.add(ContextLayer.SCRATCHPAD, content, tokens, priority=10, source="scratchpad")

    def build(self) -> str:
        """构建所有层级的完整文本输出。

        调用 LayeredContext.get_all_text() 生成带有层级标记的全文。

        返回:
            拼接后的完整上下文文本
        """
        # return self.context.get_all_text()

    def get_token_stats(self) -> dict[str, int]:
        """获取各层级的 token 统计。

        返回:
            以层级名称为键、token 数为值的字典
        """
        # return {layer.value: self.context.get_layer_tokens(layer) for layer in ContextLayer}
