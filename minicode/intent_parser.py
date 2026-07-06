"""Intent Parser - 结构化用户意图解析层。

灵感来自分层处理：原始输入 -> 清洗表达 -> 任务路径 -> 目标技能。
将用户输入转换为稳定的意图对象（ParsedIntent），再进行路由分发。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("intent_parser")


class IntentType(str, Enum):
    """用户意图类型枚举。

    涵盖代码开发、调试、重构、解释、搜索、审查、测试、文档、
    配置、提问、闲聊、记忆和系统命令等所有常见场景。
    """
    CODE = "code"
    DEBUG = "debug"
    REFACTOR = "refactor"
    EXPLAIN = "explain"
    SEARCH = "search"
    REVIEW = "review"
    TEST = "test"
    DOCUMENT = "document"
    CONFIGURE = "configure"
    QUESTION = "question"
    CHAT = "chat"
    MEMORY = "memory"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    """动作类型枚举。

    描述用户期望执行的操作：CRUD（创建/读取/更新/删除）
    以及执行、分析、比较、合并、拆分、移动、重命名等。
    """
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    ANALYZE = "analyze"
    COMPARE = "compare"
    MERGE = "merge"
    SPLIT = "split"
    MOVE = "move"
    RENAME = "rename"
    UNKNOWN = "unknown"


_CODE_PATTERNS = [
    (r"(?:write|create|implement|add|generate)\s+(?:a|an|the)?\s*(?:function|class|method|module|component|page|api)", IntentType.CODE, ActionType.CREATE),
    (r"(?:modify|update|change|fix)\s+(?:code|file|function|class|method)", IntentType.CODE, ActionType.UPDATE),
    (r"(?:implement|complete|develop)\s+(?:feature|task|requirement)", IntentType.CODE, ActionType.CREATE),
]

_DEBUG_PATTERNS = [
    (r"(?:debug|fix|solve|resolve|troubleshoot)\s+(?:error|bug|issue|problem|exception)", IntentType.DEBUG, ActionType.ANALYZE),
    (r"(?:what|why)\s+(?:is|does)\s+(?:wrong|error|fail|broken)", IntentType.DEBUG, ActionType.ANALYZE),
]

_REFACTOR_PATTERNS = [
    (r"(?:refactor|optimize|improve|clean|simplify|restructure)\s+(?:code|structure|logic|design)", IntentType.REFACTOR, ActionType.UPDATE),
]

_EXPLAIN_PATTERNS = [
    (r"(?:explain|describe|tell|what is|how to|how does)", IntentType.EXPLAIN, ActionType.READ),
]

_SEARCH_PATTERNS = [
    (r"(?:search|find|locate|lookup)\s+(?:file|code|function|class|variable|reference)", IntentType.SEARCH, ActionType.READ),
]

_REVIEW_PATTERNS = [
    (r"(?:review|check|audit|inspect)\s+(?:code|file|implementation|design)", IntentType.REVIEW, ActionType.ANALYZE),
]

_TEST_PATTERNS = [
    (r"(?:test|verify|run|execute)\s+(?:test|code|program|script|case)", IntentType.TEST, ActionType.EXECUTE),
]

_DOCUMENT_PATTERNS = [
    (r"(?:document|comment|write)\s+(?:docs?|comment|README|documentation)", IntentType.DOCUMENT, ActionType.CREATE),
]

_CONFIGURE_PATTERNS = [
    (r"(?:configure|setup|install|init)", IntentType.CONFIGURE, ActionType.UPDATE),
]

_MEMORY_PATTERNS = [
    (r"(?:remember|memory|memorize|/memory|# remember)", IntentType.MEMORY, ActionType.CREATE),
]

_SYSTEM_PATTERNS = [
    (r"^(?:/|!)(?:exit|quit|bye|clear|reset|help|settings|config|model|mode)", IntentType.SYSTEM, ActionType.EXECUTE),
]

_ALL_PATTERNS = (
    _SYSTEM_PATTERNS + _MEMORY_PATTERNS + _CODE_PATTERNS + _DEBUG_PATTERNS +
    _REFACTOR_PATTERNS + _EXPLAIN_PATTERNS + _SEARCH_PATTERNS +
    _REVIEW_PATTERNS + _TEST_PATTERNS + _DOCUMENT_PATTERNS + _CONFIGURE_PATTERNS
)


@dataclass
class ParsedIntent:
    """解析后的用户意图数据对象。

    包含原始输入、意图类型、动作类型、置信度分数、
    提取的实体（文件/函数/类等）、关键词、复杂度提示和时间戳。
    使用 dataclass 提供不可变（通过 frozen=False）的结构化存储。

    参数:
        raw_input: 原始用户输入
        intent_type: 推断的意图类型
        action_type: 推断的动作类型
        confidence: 置信度，0.0 到 1.0
        entities: 提取的实体字典（files/functions/classes/languages）
        keywords: 关键词列表
        complexity_hint: 复杂度提示 ("simple"/"moderate"/"complex")
        timestamp: 解析时间戳
    """
    raw_input: str
    intent_type: IntentType
    action_type: ActionType
    confidence: float
    entities: dict[str, list[str]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    complexity_hint: str = "moderate"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """将 ParsedIntent 序列化为字典。

        intent_type 和 action_type 转换为对应的枚举值字符串。

        返回:
            包含所有字段的字典
        """
        # return {
            "raw_input": self.raw_input,
            "intent_type": self.intent_type.value,
            "action_type": self.action_type.value,
            "confidence": self.confidence,
            "entities": self.entities,
            "keywords": self.keywords,
            "complexity_hint": self.complexity_hint,
            "timestamp": self.timestamp,
        }

    def is_code_related(self) -> bool:
        """判断是否与代码操作相关。

        包括 CODE、DEBUG、REFACTOR、REVIEW、TEST 类型的意图。

        返回:
            True 如果意图类型属于代码相关范畴
        """
        # return self.intent_type in {
            IntentType.CODE, IntentType.DEBUG, IntentType.REFACTOR,
            IntentType.REVIEW, IntentType.TEST,
        }

    def is_read_only(self) -> bool:
        """判断是否为只读操作。

        只读操作包括 READ 和 ANALYZE 类型的动作。

        返回:
            True 如果动作类型不会修改任何资源
        """
        # return self.action_type in {ActionType.READ, ActionType.ANALYZE}


class IntentParser:
    """意图解析器，负责将用户输入解析为结构化的 ParsedIntent。

    工作原理：
    1. 编译正则模式缓存（在 __init__ 时一次性编译所有模式）
    2. parse() 依次执行模式匹配、实体提取、关键词提取、复杂度评估和置信度调整
    3. 返回包含完整解析结果的 ParsedIntent 对象
    """

    def __init__(self):
        """初始化解析器，预编译所有正则模式。"""
        # self._pattern_cache: list[tuple[re.Pattern, IntentType, ActionType]] = []
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """编译所有意图匹配正则表达式，存入 _pattern_cache。

        编译失败的正则会记录警告并跳过，不影响其他模式的加载。
        """
        # for pattern, intent, action in _ALL_PATTERNS:
            try:
                self._pattern_cache.append((re.compile(pattern, re.IGNORECASE), intent, action))
            except re.error:
                logger.warning("Invalid pattern: %s", pattern)

    def parse(self, user_input: str) -> ParsedIntent:
        """解析用户输入，返回结构化的意图对象。

        完整解析流程：
        1. 空输入检查
        2. 正则模式匹配确定意图和动作类型
        3. 提取实体（文件路径、函数名、类名、编程语言）
        4. 提取关键词（去除停用词）
        5. 估计复杂度
        6. 调整置信度

        参数:
            user_input: 用户原始输入字符串

        返回:
            解析后的 ParsedIntent 对象
        """
        # if not user_input or not user_input.strip():
            return ParsedIntent(
                raw_input=user_input,
                intent_type=IntentType.UNKNOWN,
                action_type=ActionType.UNKNOWN,
                confidence=0.0,
            )

        text = user_input.strip()
        intent_type, action_type, match_confidence = self._match_patterns(text)
        entities = self._extract_entities(text)
        keywords = self._extract_keywords(text)
        complexity = self._estimate_complexity(text, intent_type, keywords)
        confidence = self._adjust_confidence(match_confidence, entities, keywords)

        return ParsedIntent(
            raw_input=text,
            intent_type=intent_type,
            action_type=action_type,
            confidence=confidence,
            entities=entities,
            keywords=keywords,
            complexity_hint=complexity,
        )

    def _match_patterns(self, text: str) -> tuple[IntentType, ActionType, float]:
        """通过正则模式匹配确定用户意图和动作类型。

        遍历所有预编译模式，按匹配位置加权打分（越靠前匹配得分越高），
        取最高分作为最终判定结果。

        参数:
            text: 清洗后的用户输入文本

        返回:
            (意图类型, 动作类型, 置信度分数) 三元组
        """
        # best_intent = IntentType.UNKNOWN
        best_action = ActionType.UNKNOWN
        best_score = 0.0

        for pattern, intent, action in self._pattern_cache:
            match = pattern.search(text)
            if match:
                score = 1.0 - (match.start() / max(len(text), 1)) * 0.3
                if score > best_score:
                    best_score = score
                    best_intent = intent
                    best_action = action

        return best_intent, best_action, best_score

    def _extract_entities(self, text: str) -> dict[str, list[str]]:
        """从文本中提取命名实体。

        识别的实体类型：
        - files: 文件路径（含常见扩展名）
        - functions: 函数/方法名
        - classes: 类名
        - languages: 编程语言/框架名

        参数:
            text: 用户输入文本

        返回:
            包含 files/functions/classes/languages 四个键的字典
        """
        # entities: dict[str, list[str]] = {"files": [], "functions": [], "classes": [], "languages": []}

        file_pattern = re.compile(r"\b([\w/\\._-]+\.(?:py|js|ts|jsx|tsx|java|go|rs|cpp|c|h|md|json|yaml|yml|toml))\b", re.I)
        for m in file_pattern.finditer(text):
            if m.group(1) not in entities["files"]:
                entities["files"].append(m.group(1))

        func_pattern = re.compile(r"\b(def|fn|func|function)\s+([\w_]+)\b", re.I)
        for m in func_pattern.finditer(text):
            if m.group(2) not in entities["functions"]:
                entities["functions"].append(m.group(2))

        class_pattern = re.compile(r"\bclass\s+([\w_]+)\b", re.I)
        for m in class_pattern.finditer(text):
            if m.group(1) not in entities["classes"]:
                entities["classes"].append(m.group(1))

        lang_pattern = re.compile(r"\b(python|javascript|typescript|java|go|rust|cpp|c\+\+|react|vue)\b", re.I)
        for m in lang_pattern.finditer(text):
            lang = m.group(1).lower()
            if lang not in entities["languages"]:
                entities["languages"].append(lang)

        return entities

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词，过滤停用词。

        支持的文本范围包括英文和中文（CJK 字符），
        保留长度大于 1 的非停用词，结果去重并限制最多 20 个。

        参数:
            text: 用户输入文本

        返回:
            去重后的关键词列表（最多 20 个）
        """
        # stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                     "have", "has", "had", "do", "does", "did", "will", "would", "could",
                     "should", "may", "might", "must", "can", "need", "to", "of", "in",
                     "for", "on", "with", "at", "by", "from", "as", "into", "through",
                     "during", "before", "after", "above", "below", "between", "under",
                     "again", "further", "then", "once", "here", "there", "when", "where",
                     "why", "how", "all", "any", "both", "each", "few", "more", "most",
                     "other", "some", "such", "no", "nor", "not", "only", "own", "same",
                     "so", "than", "too", "very", "just", "and", "but", "if", "or",
                     "because", "until", "while", "this", "that", "these", "those",
                     "i", "me", "my", "we", "our", "you", "your", "he", "him", "his",
                     "she", "her", "it", "its", "they", "them", "their", "what", "which",
                     "who", "whom"}
        words = re.findall(r"[\w一-鿿]+", text.lower())
        keywords = [w for w in words if w not in stopwords and len(w) > 1]
        seen: set[str] = set()
        unique: list[str] = []
        for w in keywords:
            if w not in seen:
                seen.add(w)
                unique.append(w)
        return unique[:20]

    def _estimate_complexity(self, text: str, intent: IntentType, keywords: list[str]) -> str:
        """估计用户输入任务的复杂度等级。

        综合三个维度加权计算：
        - 文本长度（20% 权重）：越长越复杂
        - 意图类型（50% 权重）：不同类型有基础复杂度分数
        - 关键词（30% 权重）：包含架构/设计/优化等复杂关键词加分

        参数:
            text: 用户输入文本
            intent: 推断的意图类型
            keywords: 提取的关键词列表

        返回:
            "simple" / "moderate" / "complex" 三档之一
        """
        # length_score = min(len(text) / 200, 1.0)
        intent_scores = {
            IntentType.CODE: 0.6, IntentType.DEBUG: 0.5, IntentType.REFACTOR: 0.7,
            IntentType.EXPLAIN: 0.3, IntentType.SEARCH: 0.2, IntentType.REVIEW: 0.4,
            IntentType.TEST: 0.4, IntentType.DOCUMENT: 0.3, IntentType.CONFIGURE: 0.3,
            IntentType.QUESTION: 0.2, IntentType.CHAT: 0.1, IntentType.MEMORY: 0.1,
            IntentType.SYSTEM: 0.1, IntentType.UNKNOWN: 0.5,
        }
        intent_score = intent_scores.get(intent, 0.5)
        complex_keywords = {"architect", "design", "framework", "system", "platform",
                            "infrastructure", "orchestrate", "pipeline", "migrate",
                            "integrate", "refactor", "optimize", "performance"}
        keyword_score = sum(1 for k in keywords if k in complex_keywords) / max(len(keywords), 1)
        total = length_score * 0.2 + intent_score * 0.5 + keyword_score * 0.3
        if total < 0.3:
            return "simple"
        elif total < 0.6:
            return "moderate"
        return "complex"

    def _adjust_confidence(self, base: float, entities: dict, keywords: list[str]) -> float:
        """调整最终置信度分数。

        在基础匹配得分之上，根据实体和关键词质量进行微调：
        - 存在实体：+0.1
        - 关键词数量适中（3-15 个）：+0.05

        参数:
            base: 模式匹配的基础得分
            entities: 实体字典
            keywords: 关键词列表

        返回:
            调整后的置信度（上限 1.0）
        """
        # confidence = base
        if any(entities.values()):
            confidence += 0.1
        if 3 <= len(keywords) <= 15:
            confidence += 0.05
        return min(1.0, confidence)


_parser: IntentParser | None = None


def get_intent_parser() -> IntentParser:
    """获取全局单例的 IntentParser 实例。

    使用模块级变量 _parser 缓存解析器，
    避免重复创建带来的编译开销。

    返回:
        IntentParser 单例
    """
    # global _parser
    if _parser is None:
        _parser = IntentParser()
    return _parser


def parse_intent(user_input: str) -> ParsedIntent:
    """便捷函数：解析用户输入并返回意图对象。

    内部调用 get_intent_parser() 获取全局解析器实例。

    参数:
        user_input: 用户原始输入字符串

    返回:
        解析后的 ParsedIntent 对象
    """
    # return get_intent_parser().parse(user_input)
