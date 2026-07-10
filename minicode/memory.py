"""Layered memory system for cross-session knowledge retention.

Provides three-tier memory hierarchy:
- User memory (~/.mini-code/memory/) - cross-project, persistent
- Project memory (.mini-code-memory/) - shared across sessions, can be versioned
- Local memory (.mini-code-memory-local/) - project-specific, not checked in

Memory is automatically injected into system prompts to give the agent
context about past decisions, codebase patterns, and project conventions.

Search uses TF-IDF relevance scoring for intelligent retrieval.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 记忆数据校验
# ---------------------------------------------------------------------------


def _validate_memory_data(data: dict) -> tuple[bool, list[str]]:
    """校验记忆 JSON 数据的结构完整性。

    【为什么需要】在加载记忆文件前进行结构校验，防止损坏数据导致运行时崩溃。
    加载记忆文件时自动调用，确保数据符合预期格式。

    参数:
        data: 解析后的 JSON 数据字典

    返回:
        (是否有效, 错误信息列表) 的元组
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return False, ["Root data must be a dictionary"]

    if "entries" not in data:
        errors.append("Missing required field: 'entries'")
        return False, errors

    entries = data.get("entries")
    if not isinstance(entries, list):
        errors.append("'entries' must be a list")
        return False, errors

    for idx, entry_data in enumerate(entries):
        _, entry_errors = _validate_entry(entry_data, idx)
        errors.extend(entry_errors)

    return len(errors) == 0, errors


def _validate_entry(entry: Any, index: int) -> tuple[bool, list[str]]:
    """校验单条记忆条目的字典结构。

    【为什么需要】每条记忆条目必须包含 id 和 content 等必要字段，且类型正确。
    _validate_memory_data 对每条条目循环调用此函数进行细粒度校验。

    参数:
        entry: 待校验的条目字典
        index: 条目在列表中的索引（用于错误定位）

    返回:
        (是否有效, 错误信息列表) 的元组
    """
    errors: list[str] = []
    prefix = f"Entry at index {index}"

    if not isinstance(entry, dict):
        return False, [f"{prefix} is not a dictionary"]

    required_fields = ["id", "content"]
    for field_name in required_fields:
        if field_name not in entry:
            errors.append(f"{prefix} missing required field: '{field_name}'")

    if "id" in entry and not isinstance(entry["id"], str):
        errors.append(f"{prefix} field 'id' must be a string")

    if "scope" in entry:
        scope_val = entry["scope"]
        if not isinstance(scope_val, str):
            errors.append(f"{prefix} field 'scope' must be a string")
        elif scope_val not in _VALID_SCOPES:
            errors.append(
                f"{prefix} has invalid scope value: '{scope_val}'. "
                f"Must be one of: {', '.join(sorted(_VALID_SCOPES))}"
            )

    if "category" in entry and not isinstance(entry["category"], str):
        errors.append(f"{prefix} field 'category' must be a string")

    if "content" in entry and not isinstance(entry["content"], str):
        errors.append(f"{prefix} field 'content' must be a string")

    if "created_at" in entry:
        val = entry["created_at"]
        if not isinstance(val, (int, float)):
            errors.append(f"{prefix} field 'created_at' must be a number")

    if "updated_at" in entry:
        val = entry["updated_at"]
        if not isinstance(val, (int, float)):
            errors.append(f"{prefix} field 'updated_at' must be a number")

    if "tags" in entry:
        val = entry["tags"]
        if not isinstance(val, list):
            errors.append(f"{prefix} field 'tags' must be a list")
        elif not all(isinstance(t, str) for t in val):
            errors.append(f"{prefix} field 'tags' must contain only strings")

    if "usage_count" in entry:
        val = entry["usage_count"]
        if not isinstance(val, int):
            errors.append(f"{prefix} field 'usage_count' must be an integer")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# 损坏数据恢复
# ---------------------------------------------------------------------------

def _recover_entries(data: dict, memory_json_path: Path) -> list[dict]:
    """尝试从损坏的记忆数据中恢复有效条目。

    【为什么需要】当 memory.json 文件部分损坏时，自动备份原文件并过滤出有效条目，
    避免数据完全丢失。在加载记忆文件检测到数据损坏时自动调用。

    参数:
        data: 已解析的 JSON 数据（可能部分损坏）
        memory_json_path: 原始 memory.json 文件路径

    返回:
        有效条目字典的列表
    """
    backup_path = memory_json_path.with_suffix(".json.bak")
    try:
        import shutil
        shutil.copy2(str(memory_json_path), str(backup_path))
        logger.warning(
            "Corrupted memory file backed up to %s", backup_path
        )
    except OSError as e:
        logger.error(
            "Failed to create backup of corrupted memory file: %s", e
        )

    entries = data.get("entries", [])
    valid_entries = []
    recovered_count = 0

    for idx, entry_data in enumerate(entries):
        entry_valid, _ = _validate_entry(entry_data, idx)
        if not entry_valid:
            logger.warning("Skipping corrupted entry at index %d", idx)
        else:
            valid_entries.append(entry_data)
            recovered_count += 1

    total = len(entries)
    logger.info(
        "Recovery complete: %d/%d entries recovered", recovered_count, total
    )
    return valid_entries




# ---------------------------------------------------------------------------
# TF-IDF \u641c\u7d22\u5de5\u5177\u51fd\u6570
# ---------------------------------------------------------------------------

# \u5c06\u6587\u672c\u5206\u8bcd\u4e3a\u5c0f\u5199\u82f1\u6587\u5355\u8bcd\u3001\u5355\u4e2a\u4e2d\u6587\u5b57\u7b26\u548c\u4e2d\u6587\u5b57\u7b26\u4e8c\u5143\u7ec4
_WORD_RE = re.compile(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]')
_CJK_BIGRAM_RE = re.compile(r'[\u4e00-\u9fff]{2}')

# \u5e38\u7528\u7f16\u7a0b\u672f\u8bed\u53cc\u5411\u6269\u5c55\u6620\u5c04\u8868\uff08\u4e2d\u82f1\u6587\u4e92\u67e5\uff09
_CODE_TERM_EXPANSIONS: dict[str, list[str]] = {
    "函数": ["function", "func", "method"],
    "function": ["函数", "func", "method"],
    "func": ["函数", "function", "method"],
    "method": ["函数", "function", "func"],
    "类": ["class", "type"],
    "class": ["类", "type"],
    "type": ["类", "class"],
    "变量": ["variable", "var"],
    "variable": ["变量", "var"],
    "var": ["变量", "variable"],
    "参数": ["parameter", "param", "argument", "arg"],
    "parameter": ["参数", "param", "argument"],
    "param": ["参数", "parameter", "arg"],
    "argument": ["参数", "parameter", "arg"],
    "属性": ["attribute", "attr", "property", "prop"],
    "attribute": ["属性", "attr", "property"],
    "property": ["属性", "attr", "prop"],
    "接口": ["interface"],
    "interface": ["接口"],
    "模块": ["module"],
    "module": ["模块"],
    "包": ["package"],
    "package": ["包"],
    "方法": ["method", "function"],
    "对象": ["object", "obj"],
    "object": ["对象", "obj"],
    "继承": ["inherit", "inheritance", "extends"],
    "inherit": ["继承"],
    "多态": ["polymorphism"],
    "封装": ["encapsulation", "encapsulate"],
    "异常": ["exception", "error"],
    "exception": ["异常"],
    "error": ["错误", "异常"],
    "错误": ["error", "bug"],
    "bug": ["错误", "bug", "缺陷"],
    "循环": ["loop", "iteration", "iterate"],
    "loop": ["循环"],
    "条件": ["condition"],
    "condition": ["条件"],
    "数组": ["array"],
    "array": ["数组"],
    "列表": ["list"],
    "list": ["列表"],
    "字典": ["dict", "dictionary", "map"],
    "dict": ["字典", "dictionary"],
    "dictionary": ["字典", "dict"],
    "map": ["字典", "映射"],
    "映射": ["map"],
    "集合": ["set"],
    "set": ["集合"],
    "字符串": ["string", "str"],
    "string": ["字符串"],
    "整数": ["int", "integer"],
    "integer": ["整数"],
    "浮点": ["float"],
    "float": ["浮点"],
    "布尔": ["bool", "boolean"],
    "boolean": ["布尔"],
    "同步": ["sync", "synchronous"],
    "异步": ["async", "asynchronous"],
    "async": ["异步"],
    "回调": ["callback"],
    "callback": ["回调"],
    "事件": ["event"],
    "event": ["事件"],
    "装饰器": ["decorator"],
    "decorator": ["装饰器"],
    "生成器": ["generator"],
    "generator": ["生成器"],
    "迭代器": ["iterator"],
    "iterator": ["迭代器"],
    "测试": ["test", "testing"],
    "test": ["测试"],
    "调试": ["debug", "debugging"],
    "debug": ["调试"],
    "配置": ["config", "configuration"],
    "config": ["配置"],
    "数据库": ["database", "db"],
    "database": ["数据库", "db"],
    "缓存": ["cache"],
    "cache": ["缓存"],
    "队列": ["queue"],
    "queue": ["队列"],
    "栈": ["stack"],
    "stack": ["栈"],
    "树": ["tree"],
    "tree": ["树"],
    "图": ["graph"],
    "graph": ["图"],
    "搜索": ["search"],
    "search": ["搜索"],
    "排序": ["sort", "sorting"],
    "sort": ["排序"],
    "文件": ["file"],
    "file": ["文件"],
    "路径": ["path"],
    "path": ["路径"],
    "网络": ["network"],
    "network": ["网络"],
    "请求": ["request"],
    "request": ["请求"],
    "响应": ["response"],
    "response": ["响应"],
}


def _expand_query_terms(terms: list[str], active_domains: list[str] | None = None) -> list[str]:
    """使用编程术语和领域字典扩展搜索查询词。

    【为什么需要】用户搜索"函数"时应能匹配到 function/method/def 等英文术语，
    反之亦然。此函数通过中英文术语映射表和领域特定字典，将查询词扩展为同义词集合，
    提升跨语言搜索的召回率。

    执行流程（仅 及以上的方法需要）：
    ╔══ 完整执行流程 ══╗
    第1步: 复制原始查询词列表作为扩展基础
    第2步: 遍历每个查询词，检查通用编程术语扩展表
    第3步: 若 active_domains 存在，遍历每个领域对应的扩展字典追加扩展词
    第4步: 返回扩展后的完整词列表
    ╚══════════════╝

    参数:
        terms: 原始查询词列表
        active_domains: 当前活跃领域列表，用于领域特定扩展

    返回:
        扩展后的查询词列表（含原始词和同义词）
    """
    expanded = list(terms)
    for term in terms:
        if term in _CODE_TERM_EXPANSIONS:
            expanded.extend(_CODE_TERM_EXPANSIONS[term])
    # 领域特定扩展
    if active_domains:
        for domain in active_domains:
            domain_dict = _DOMAIN_TERM_EXPANSIONS.get(domain, {})
            for term in terms:
                if term in domain_dict:
                    expanded.extend(domain_dict[term])
    return expanded


# ── 领域特定术语扩展 ─────────────────────────────────

_DOMAIN_TERM_EXPANSIONS: dict[str, dict[str, list[str]]] = {
    "frontend": {
        "component": ["组件", "widget", "control", "element"],
        "组件": ["component", "widget", "control"],
        "form": ["表单", "input", "field"],
        "表单": ["form", "input", "field"],
        "style": ["样式", "css", "theme", "design"],
        "样式": ["style", "css", "theme"],
        "css": ["样式", "style", "theme", "tailwind"],
        "render": ["渲染", "display", "paint"],
        "渲染": ["render", "display"],
        "state": ["状态", "store", "context"],
        "状态": ["state", "store"],
        "hook": ["hooks", "钩子"],
        "router": ["路由", "navigation"],
        "路由": ["router", "navigation", "route"],
        "button": ["按钮", "btn"],
        "modal": ["弹窗", "dialog", "popup"],
        "layout": ["布局", "grid", "flex"],
        "布局": ["layout", "grid", "flexbox"],
        "animation": ["动画", "transition", "motion"],
        "event": ["事件", "handler", "listener"],
        "props": ["属性", "properties", "parameters"],
        "dom": ["文档", "document", "node", "element"],
        "responsive": ["响应式", "adaptive", "mobile"],
        "typescript": ["ts", "type"],
    },
    "backend": {
        "api": ["端点", "endpoint", "路由", "route", "handler"],
        "endpoint": ["端点", "api", "路由"],
        "route": ["路由", "path", "endpoint", "api"],
        "auth": ["认证", "鉴权", "login", "token", "jwt", "oauth"],
        "认证": ["auth", "authentication", "login"],
        "middleware": ["中间件", "interceptor", "filter"],
        "中间件": ["middleware", "interceptor"],
        "request": ["请求", "req"],
        "response": ["响应", "res", "reply"],
        "server": ["服务器", "服务端", "host"],
        "服务器": ["server", "host"],
        "queue": ["队列", "message", "mq", "worker"],
        "队列": ["queue", "message", "worker"],
        "cache": ["缓存", "redis", "memcache"],
        "缓存": ["cache", "redis"],
        "cron": ["定时", "schedule", "job", "task"],
        "定时": ["cron", "schedule", "timer"],
        "log": ["日志", "logging", "trace"],
        "日志": ["log", "logging"],
        "validate": ["校验", "验证", "sanitize", "check"],
        "校验": ["validate", "validation", "check"],
        "rate limit": ["限流", "throttle", "quota"],
        "限流": ["rate limit", "throttle"],
        "serialize": ["序列化", "marshal", "json"],
        "序列化": ["serialize", "marshal"],
    },
    "database": {
        "migration": ["迁移", "schema change", "ddl", "alembic", "flyway"],
        "迁移": ["migration", "schema change"],
        "schema": ["模式", "结构", "ddl", "table def"],
        "query": ["查询", "select", "sql"],
        "查询": ["query", "select", "read"],
        "index": ["索引", "btree", "hash"],
        "索引": ["index", "lookup"],
        "transaction": ["事务", "commit", "rollback", "acid"],
        "事务": ["transaction", "commit"],
        "connection": ["连接", "pool", "session"],
        "连接": ["connection", "pool"],
        "postgres": ["postgresql", "pg"],
        "orm": ["prisma", "typeorm", "sequelize", "drizzle", "sqlalchemy"],
        "backup": ["备份", "dump", "restore"],
        "备份": ["backup", "dump"],
        "replica": ["副本", "standby", "slave"],
        "partition": ["分区", "shard", "split"],
    },
    "devops": {
        "deploy": ["部署", "release", "ship"],
        "部署": ["deploy", "release"],
        "docker": ["容器", "container", "image"],
        "容器": ["docker", "container"],
        "ci": ["持续集成", "pipeline", "build"],
        "pipeline": ["流水线", "ci/cd", "workflow"],
        "monitor": ["监控", "alert", "observe", "metrics"],
        "监控": ["monitor", "alert", "metrics"],
        "secret": ["密钥", "credentials", "env"],
        "密钥": ["secret", "credentials", "token"],
        "kubernetes": ["k8s", "pod", "cluster"],
        "k8s": ["kubernetes", "cluster"],
        "nginx": ["反向代理", "proxy", "gateway"],
        "terraform": ["基础设施", "infrastructure", "iac"],
        "log": ["日志", "logging", "收集", "aggregate"],
        "backup": ["备份", "snapshot", "restore"],
    },
    "testing": {
        "test": ["测试", "spec", "assert"],
        "mock": ["模拟", "stub", "fake", "spy"],
        "模拟": ["mock", "stub", "fake"],
        "assert": ["断言", "expect", "should"],
        "断言": ["assert", "expect"],
        "coverage": ["覆盖率", "cover"],
        "e2e": ["端到端", "end-to-end", "integration"],
        "unit": ["单元", "unit test"],
        "fixture": ["夹具", "setup", "teardown"],
        "regression": ["回归", "replay"],
    },
}


@functools.lru_cache(maxsize=1024)
def _tokenize(text: str) -> list[str]:
    """将文本分词为词元列表，用于 TF-IDF 相关性评分。

    【为什么需要】BM25/TF-IDF 算法需要将文本拆分为可计数的词元。
    同时处理英文单词和中文（单字 + 二元组），使跨语言搜索更准确。

    执行流程（仅 及以上的方法需要）：
    ╔══ 完整执行流程 ══╗
    第1步: 用正则提取英文单词和单个中文字符，转为小写
    第2步: 用正则提取连续的两个中文字符作为二元组
    第3步: 合并两部分结果并返回
    ╚══════════════╝

    参数:
        text: 待分词的文本

    返回:
        词元字符串列表
    """
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    cjk_bigrams = [match.lower() for match in _CJK_BIGRAM_RE.findall(text)]
    return tokens + cjk_bigrams


# BM25 算法参数
_BM25_K1 = 1.5  # 词频缩放因子
_BM25_B = 0.75  # 文档长度归一化因子


def _compute_tf(tokens: list[str]) -> dict[str, float]:
    """计算词元列表的词频（TF）。

    【为什么需要】TF 是 BM25/TF-IDF 的基础组成部分，衡量一个词在文档中的重要程度。

    参数:
        tokens: 文档词元列表

    返回:
        词元到词频的映射字典（词频 = 出现次数 / 总词元数）
    """
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {term: count / total for term, count in counts.items()}


def _compute_idf(documents: list[list[str]]) -> dict[str, float]:
    """计算词语在文档集合中的逆文档频率（IDF）。

    【为什么需要】IDF 衡量一个词的区分能力——出现范围越广的词区分能力越弱。
    使用平滑公式 log((N + 1) / (df + 1)) + 1 避免除零。

    参数:
        documents: 所有文档的词元列表集合

    返回:
        词语到 IDF 值的映射字典
    """
    n = len(documents)
    if n == 0:
        return {}
    doc_freq: dict[str, int] = {}
    for doc_tokens in documents:
        seen = set(doc_tokens)
        for term in seen:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return {
        term: math.log((n + 1) / (df + 1)) + 1
        for term, df in doc_freq.items()
    }


def _compute_avgdl(documents: list[list[str]]) -> float:
    """计算文档集合的平均长度。

    【为什么需要】BM25 算法需要对长文档进行长度归一化，avgdl 是归一化的基准值。

    参数:
        documents: 所有文档的词元列表集合

    返回:
        平均文档长度（词元数）
    """
    if not documents:
        return 0.0
    return sum(len(doc) for doc in documents) / len(documents)


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    idf: dict[str, float],
    avgdl: float,
    *,
    k1: float = _BM25_K1,
    b: float = _BM25_B,
) -> float:
    """计算查询与文档之间的 Okapi BM25 相关性得分。

    【为什么需要】BM25 是业界最常用的文本相关性排序算法之一，相比 TF-IDF
    引入了词频饱和和文档长度归一化，更适合短文本（如记忆条目）的排序场景。
    搜索结果排序的核心评分函数。

    执行流程（仅 及以上的方法需要）：
    ╔══ 完整执行流程 ══╗
    第1步: 检查输入有效性（空查询/空文档/avgdl 为 0 时返回 0）
    第2步: 计算文档词频 TF 和文档总长度
    第3步: 遍历查询词（去重），跳过不在 IDF 字典中的词
    第4步: 对每个查询词计算 BM25 贡献：IDF * (TF * (k1+1)) / (TF + k1*(1-b+b*|d|/avgdl))
    第5步: 累加所有查询词的得分并返回
    ╚══════════════╝

    参数:
        query_tokens: 查询词元列表
        doc_tokens: 文档词元列表
        idf: 词语到 IDF 值的映射字典
        avgdl: 文档集合平均长度
        k1: BM25 词频饱和参数（默认 1.5）
        b: BM25 文档长度归一化参数（默认 0.75）

    返回:
        BM25 相关性得分
    """
    if not query_tokens or not doc_tokens or avgdl == 0:
        return 0.0

    doc_len = len(doc_tokens)
    tf_doc = _compute_tf(doc_tokens)
    total_tokens = doc_len

    score = 0.0
    for term in set(query_tokens):
        if term not in idf:
            continue
        tf = tf_doc.get(term, 0.0)
        if tf == 0:
            continue
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * (total_tokens / avgdl))
        score += idf[term] * (numerator / denominator)

    return score


def _tfidf_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    idf: dict[str, float],
    avgdl: float = 0.0,
) -> float:
    """计算查询与文档之间的 BM25 得分（兼容旧接口的包装函数）。

    【为什么需要】为了保持向后兼容性，旧代码调用 _tfidf_score 继续工作，
    内部实际委托给 _bm25_score 获得更好的短文本排序效果。

    参数:
        query_tokens: 查询词元列表
        doc_tokens: 文档词元列表
        idf: 词语到 IDF 值的映射字典
        avgdl: 文档集合平均长度

    返回:
        BM25 相关性得分
    """
    return _bm25_score(query_tokens, doc_tokens, idf, avgdl)


def get_tfidf_keywords(text: str, top_n: int = 10) -> list[tuple[str, float]]:
    """从文本中提取 TF 得分最高的前 N 个关键词。

    【为什么需要】用于自动分类和文本主题识别，帮助在没有人工标注的情况下了解
    一段文本的核心主题。在自动分类功能中辅助判断内容类别。

    参数:
        text: 待分析的输入文本
        top_n: 返回的关键词数量上限

    返回:
        按重要性降序排列的 (词元, TF 得分) 列表
    """
    tokens = _tokenize(text)
    if not tokens:
        return []
    tf = _compute_tf(tokens)
    sorted_terms = sorted(tf.items(), key=lambda x: x[1], reverse=True)
    return sorted_terms[:top_n]


# ---------------------------------------------------------------------------
# 自动分类启发式规则
# ---------------------------------------------------------------------------

_CLASSIFICATION_RULES: list[tuple[str, list[str], list[str]]] = [
    ("architecture", ["architecture", "design", "pattern", "api", "rest", "backend", "service", "架构", "设计", "模式"]),
    ("code-pattern", ["function", "method", "def", "class", "函数", "方法", "类"]),
    ("testing", ["test", "assert", "pytest", "unit", "测试", "断言"]),
    ("configuration", ["config", "settings", "env", "配置", "设置", "环境"]),
    ("workflow", ["git", "commit", "branch", "merge", "工作流", "分支", "合并"]),
    ("security", ["security", "auth", "permission", "安全", "认证", "权限"]),
    ("performance", ["performance", "optimization", "benchmark", "性能", "优化", "基准"]),
    ("convention", ["convention", "style", "naming", "规范", "风格", "命名"]),
]


def _auto_classify_content(content: str) -> tuple[str, list[str]]:
    """通过关键词启发式规则分析内容，返回分类和标签。

    【为什么需要】用户添加记忆时可以不指定分类，系统通过分析内容中的关键词
    自动推断条目的分类（如 architecture/testing/security 等），减少用户操作成本。
    同时支持中英文关键词匹配。

    执行流程（仅 及以上的方法需要）：
    ╔══ 完整执行流程 ══╗
    第1步: 将内容转为小写，遍历分类规则表为每个分类打分
    第2步: 若匹配到关键词，累加该分类的得分并收集对应标签
    第3步: 若没有匹配到任何分类，返回 ("general", [])
    第4步: 选择得分最高的分类，连同匹配标签一起返回
    ╚══════════════╝

    参数:
        content: 待分类的文本内容

    返回:
        (分类名称, 标签列表) 的元组，例如 ("architecture", ["design-pattern"])
    """
    content_lower = content.lower()
    category_scores: dict[str, int] = {}
    matched_tags: list[str] = []

    category_to_tags = {
        "architecture": ["design-pattern"],
        "code-pattern": ["function"],
        "testing": ["test"],
        "configuration": ["config"],
        "workflow": ["git"],
        "security": ["security"],
        "performance": ["optimization"],
        "convention": ["style"],
    }

    for category, keywords in (
        (rule[0], rule[1]) for rule in _CLASSIFICATION_RULES
    ):
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            category_scores[category] = score
            matched_tags.extend(category_to_tags.get(category, []))

    if not category_scores:
        return "general", []

    best_category = max(category_scores, key=category_scores.get)
    return best_category, matched_tags


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------

class MemoryScope(str, Enum):
    """记忆作用域级别枚举。

    【为什么需要】区分记忆的作用范围，控制不同层级记忆的可见性和持久性。
    USER 跨项目全局共享，PROJECT 项目内共享（可版本控制），LOCAL 本地私有。
    """
    USER = "user"       # 跨项目全局，存储在 ~/.mini-code/memory/
    PROJECT = "project" # 项目内共享，存储在 .mini-code-memory/
    LOCAL = "local"     # 项目本地私有，存储在 .mini-code-memory-local/


class MemoryTier(str, Enum):
    """记忆层级枚举，实现多级存储架构。

    【为什么需要】受人类记忆模型（Atkinson-Shiffrin）和 Letta/MemGPT 启发，
    将记忆分为工作/短期/长期/归档四个层级，通过自动升降级机制实现智能记忆管理。
    """
    WORKING = "working"       # 当前会话，完整细节，高速访问
    SHORT_TERM = "short_term" # 近期（< 7 天），完整细节
    LONG_TERM = "long_term"   # 已沉淀（< 30 天），压缩
    ARCHIVAL = "archival"     # 永久保存，高度概括


_VALID_SCOPES = {m.value for m in MemoryScope}


@dataclass
class MemoryEntry:
    """单条记忆条目数据结构。

    【为什么需要】封装一条事实/模式/决策等记忆的全部信息，包括内容、分类、
    标签、层级、关联等元数据。是记忆系统中数据的核心载体单元。

    参数:
        id: 唯一标识符
        scope: 记忆作用域
        category: 分类名称（如 architecture/convention/decision/pattern）
        content: 记忆内容文本
        created_at: 创建时间戳
        updated_at: 最后更新时间戳
        tags: 标签列表
        usage_count: 被引用次数
        domains: 所属领域分类
        tier: 记忆层级（默认为短期记忆）
        last_accessed: 最后访问时间
        related_to: 关联的记忆 ID 列表
        _cached_tokens: 缓存的词元列表（不参与序列化）
    """
    id: str
    scope: MemoryScope
    category: str  # 如 "architecture", "convention", "decision", "pattern"
    content: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    usage_count: int = 0  # 被引用次数
    domains: list[str] = field(default_factory=list)  # 领域分类
    # 多级记忆层级架构
    tier: MemoryTier = MemoryTier.SHORT_TERM
    last_accessed: float = field(default_factory=time.time)
    related_to: list[str] = field(default_factory=list)  # 关联的记忆 ID
    _cached_tokens: list[str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """初始化后处理：确保 content 字段始终为字符串类型。

        【为什么需要】content 在搜索/评分/格式化等 8 处以上被调用了
        .lower()/.strip()/[:N] 等字符串方法。如果构造时传入 None 或其他类型，
        会在记忆搜索（注入到每个系统提示词中）时崩溃。此钩子在 dataclass 初始化
        后自动执行类型强制转换。
        """
        if not isinstance(self.content, str):
            self.content = "" if self.content is None else str(self.content)

    def __hash__(self) -> int:
        """基于 id 计算哈希值，支持将条目放入集合或作为字典键。"""
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        """基于 id 判断两个记忆条目是否相等。"""
        if not isinstance(other, MemoryEntry):
            return NotImplemented
        return self.id == other.id

    def get_tokens(self) -> list[str]:
        """获取条目的分词结果（带缓存）。

        【为什么需要】搜索时需要频繁对同一条目分词，缓存可以避免重复计算。
        缓存在内容变更时通过 invalidate_tokens 清空。

        返回:
            条目的词元列表（包括内容、分类、标签的词元）
        """
        if self._cached_tokens is None:
            text = f"{self.content} {self.category} {' '.join(self.tags)}"
            self._cached_tokens = _tokenize(text)
        return self._cached_tokens

    def invalidate_tokens(self) -> None:
        """清空分词缓存，在条目内容变更后调用。"""
        self._cached_tokens = None

    def to_dict(self) -> dict[str, Any]:
        """将记忆条目转换为可序列化的字典。

        【为什么需要】用于将记忆条目保存到 JSON 文件中持久化存储。

        返回:
            包含条目所有字段的字典
        """
        return {
            "id": self.id,
            "scope": self.scope.value,
            "category": self.category,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "usage_count": self.usage_count,
            "domains": self.domains,
            "tier": self.tier.value,
            "last_accessed": self.last_accessed,
            "related_to": self.related_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """从字典创建记忆条目（反序列化）。

        【为什么需要】从 JSON 文件加载记忆数据时，将字典还原为 MemoryEntry 对象。
        使用 .get() 方法提供默认值，以保证向后兼容旧格式数据。

        参数:
            data: 包含条目字段的字典

        返回:
            还原的 MemoryEntry 实例
        """
        return cls(
            id=data["id"],
            scope=MemoryScope(data.get("scope", "user")),
            category=data.get("category", "general"),
            content=data["content"],
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            tags=data.get("tags", []),
            usage_count=data.get("usage_count", 0),
            domains=data.get("domains", []),
            tier=MemoryTier(data.get("tier", "short_term")),
            last_accessed=data.get("last_accessed", time.time()),
            related_to=data.get("related_to", []),
        )


@dataclass
class MemoryFile:
    """内存中的记忆条目容器，带三层索引和 BM25 搜索能力。

    【为什么需要】每条记忆存到 JSON 后，搜索时需要频繁按 id/标签/分类查找，
    以及 BM25 语义评分。每次搜索都重新构建索引太慢，所以 MemoryFile
    在内存中维护了 id/标签/分类 三层索引 + 分词/IDF 缓存，增删后增量更新。

    参数:
        scope: 记忆作用域
        entries: 记忆条目列表
        max_entries: 最大条目数限制（默认 200，适配 Claude Code 上下文窗口）
        max_size_bytes: 最大字节数限制（默认 25KB）
    """
    scope: MemoryScope
    entries: list[MemoryEntry] = field(default_factory=list)
    max_entries: int = 200  # Claude Code 上下文长度限制
    max_size_bytes: int = 25 * 1024  # 25KB 体积限制
    _id_index: dict[str, MemoryEntry] = field(default_factory=dict, repr=False)
    _tag_index: dict[str, set[MemoryEntry]] = field(default_factory=dict, repr=False)
    _category_index: dict[str, list[MemoryEntry]] = field(default_factory=dict, repr=False)
    _tokens_cache: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _idf_cache: dict[str, float] | None = field(default=None, repr=False)
    _avgdl_cache: float | None = field(default=None, repr=False)
    _cache_dirty: bool = field(default=True, repr=False)

    def _rebuild_indices(self) -> None:
        """重建所有索引缓存（id/标签/分类/词元/IDF/平均长度）。

        【为什么需要】当缓存标记为脏或从文件加载数据后，需要重新构建索引以确保
        后续搜索和查找操作的正确性。此方法全量重建而非增量更新。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 清空所有现有索引（id/标签/分类/词元缓存）
        第2步: 遍历所有条目，分别填充 id/标签/分类索引
        第3步: 缓存每个条目的词元
        第4步: 预计算全局 IDF 和平均文档长度
        第5步: 将脏标记置为 False
        ╚══════════════╝
        """
        self._id_index.clear()
        self._tag_index.clear()
        self._category_index.clear()
        self._tokens_cache.clear()
        for entry in self.entries:
            self._id_index[entry.id] = entry
            for tag in entry.tags:
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].add(entry)
            cat = entry.category
            if cat not in self._category_index:
                self._category_index[cat] = []
            self._category_index[cat].append(entry)
            self._tokens_cache[entry.id] = entry.get_tokens()
        # 预计算全局 IDF 和平均文档长度
        if self._tokens_cache:
            all_tokens = list(self._tokens_cache.values())
            self._idf_cache = _compute_idf(all_tokens)
            self._avgdl_cache = _compute_avgdl(all_tokens)
        self._cache_dirty = False

    def _ensure_cache_valid(self) -> None:
        """确保索引缓存有效，若脏则触发重建。"""
        if self._cache_dirty:
            self._rebuild_indices()

    def _invalidate_cache(self) -> None:
        """标记缓存为脏，下次访问时触发重建。"""
        self._cache_dirty = True
        self._idf_cache = None
        self._avgdl_cache = None

    @property
    def size_bytes(self) -> int:
        """估算当前条目内容占用的字节数，用于容量检查。"""
        return sum(len(e.content) for e in self.entries)

    def add_entry(self, entry: MemoryEntry) -> None:
        """添加记忆条目，维护索引并执行容量限制。

        【为什么需要】这是 MemoryFile 写入数据的主要入口。新增条目后需要增量
        更新 id/标签/分类/词元索引以保持搜索高效率，同时检查容量限制确保不
        超过 Claude Code 的上下文长度约束。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 确保索引缓存有效
        第2步: 将条目加入 entries 列表
        第3步: 增量更新 id 索引
        第4步: 增量更新标签索引（每个标签指向该条目）
        第5步: 增量更新分类索引
        第6步: 缓存新条目的词元
        第7步: 检查容量限制，超限时移除最旧条目
        ╚══════════════╝

        参数:
            entry: 待添加的 MemoryEntry 实例
        """
        self._ensure_cache_valid()
        self.entries.append(entry)
        self._id_index[entry.id] = entry
        for tag in entry.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            self._tag_index[tag].add(entry)
        cat = entry.category
        if cat not in self._category_index:
            self._category_index[cat] = []
        self._category_index[cat].append(entry)
        self._tokens_cache[entry.id] = entry.get_tokens()
        self._enforce_limits()

    def update_entry(self, entry_id: str, content: str) -> bool:
        """通过 id 索引更新已有条目的内容。

        【为什么需要】记忆内容可能需要更新（如补充细节、修正错误），使用
        id 索引可以快速定位条目，避免遍历整个列表。更新后自动刷新词元缓存。

        参数:
            entry_id: 目标条目的 id
            content: 新的内容文本

        返回:
            是否成功找到并更新
        """
        self._ensure_cache_valid()
        entry = self._id_index.get(entry_id)
        if entry is None:
            return False
        entry.content = content
        entry.updated_at = time.time()
        entry.invalidate_tokens()
        self._tokens_cache[entry.id] = entry.get_tokens()
        return True

    def delete_entry(self, entry_id: str) -> bool:
        """通过 id 索引删除条目。

        【为什么需要】用户可能需要删除不再需要的记忆条目。使用索引快速定位
        并从所有索引结构中同步移除，保持数据一致性。

        参数:
            entry_id: 目标条目的 id

        返回:
            是否成功找到并删除
        """
        self._ensure_cache_valid()
        entry = self._id_index.get(entry_id)
        if entry is None:
            return False
        self.entries.remove(entry)
        del self._id_index[entry_id]
        for tag in entry.tags:
            if tag in self._tag_index:
                self._tag_index[tag].discard(entry)
        cat = entry.category
        if cat in self._category_index and entry in self._category_index[cat]:
            self._category_index[cat].remove(entry)
        self._tokens_cache.pop(entry_id, None)
        return True

    def get_entries_by_category(self, category: str) -> list[MemoryEntry]:
        """通过分类索引获取指定分类的条目列表。

        参数:
            category: 分类名称

        返回:
            该分类下的条目列表副本
        """
        self._ensure_cache_valid()
        return list(self._category_index.get(category, []))

    def search(self, query: str, active_domains: list[str] | None = None) -> list[MemoryEntry]:
        """使用 BM25 + 领域相关性对条目进行搜索排序。

        【为什么需要】核心搜索方法，融合 BM25 语义相关性、使用频率、
        领域匹配度和时间新近度进行综合排序。领域得分使用软混合而非硬过滤，
        确保即使没有领域匹配的条目也不会被完全排除。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 快照 entries 列表，防止并发操作导致索引偏移
        第2步: 对查询分词并用术语扩展表扩展同义词
        第3步: 对所有条目分词并计算全局 IDF 和平均文档长度
        第4步: 遍历每个条目，分别计算 BM25 得分、子串匹配得分、标签匹配得分
        第5步: 计算领域 Jaccard 相似度得分和综合相关性
        第6步: 叠加使用频率奖励和时间新近度奖励
        第7步: 按总得分降序排序，前 10 名增加使用计数
        第8步: 返回排序后的条目列表
        ╚══════════════╝

        参数:
            query: 搜索查询字符串
            active_domains: 当前活跃领域列表，用于领域得分软提升

        返回:
            按相关性降序排列的记忆条目列表
        """
        if not self.entries:
            return []

        # 快照 entries 防止并发 add_entry/_enforce_limits 导致索引偏移
        # （曾因此出现 "list index out of range" 错误：在构建 entry_tokens
        #  和评分循环之间另一个线程追加了条目）
        entries = list(self.entries)

        query_tokens = _tokenize(query)
        query_tokens = _expand_query_terms(query_tokens, active_domains=active_domains)
        if not query_tokens:
            return []

        query_lower = query.lower()
        query_terms = query_lower.split()

        entry_tokens = []
        for entry in entries:
            text = f"{entry.content} {entry.category} {' '.join(entry.tags)}"
            entry_tokens.append(_tokenize(text))

        idf = _compute_idf(entry_tokens)
        avgdl = _compute_avgdl(entry_tokens)

        scored: list[tuple[float, MemoryEntry]] = []
        for i, entry in enumerate(entries):
            bm25 = _bm25_score(query_tokens, entry_tokens[i], idf, avgdl)

            substring_score = 0.0
            content_lower = entry.content.lower()
            if query_lower in content_lower:
                substring_score = 2.0
            elif any(q in content_lower for q in query_terms):
                substring_score = 1.0

            tag_score = 0.0
            exact_tag_match = any(
                tag.lower() == query_lower for tag in entry.tags
            )
            partial_tag_match = any(
                query_lower in tag.lower() for tag in entry.tags
            )
            if exact_tag_match:
                tag_score = 5.0
            elif partial_tag_match:
                tag_score = 1.5
            if query_lower in entry.category.lower():
                tag_score += 1.0

            match_score = bm25 + substring_score + tag_score
            if match_score <= 0:
                continue

            # 领域得分：计算 entry.domains 与 active_domains 的 Jaccard 相似度
            domain_score = 0.0
            if active_domains and entry.domains:
                entry_set = set(entry.domains)
                active_set = set(active_domains)
                intersection = entry_set & active_set
                union = entry_set | active_set
                domain_score = len(intersection) / len(union) if union else 0.0

            # 软混合：BM25 主导，领域得分轻度引导
            final_relevance = match_score * 0.7 + domain_score * 0.3

            usage_bonus = math.log1p(entry.usage_count) * 0.3
            age_hours = (time.time() - entry.updated_at) / 3600
            recency_bonus = 1.0 / (1.0 + age_hours / 24.0) * 0.5

            total_score = final_relevance + usage_bonus + recency_bonus
            scored.append((total_score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        # 增加前 10 名结果的使用计数，反馈到未来搜索排序中
        for _, entry in scored[:10]:
            entry.usage_count += 1
        return [entry for _, entry in scored]

    def _enforce_limits(self) -> None:
        """超过容量限制时移除最旧条目。

        【为什么需要】Claude Code 对记忆文件有大小限制（条目数 ≤ 200，体积 ≤ 25KB），
        防止记忆文件过大导致系统提示词超限。每次新增条目后自动检查并清理。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 检查条目数量，超过 max_entries 则从列表头部移除最旧条目
        第2步: 检查总体积，超过 max_size_bytes 则从列表头部继续移除
        ╚══════════════╝
        """
        # 检查条目数限制
        while len(self.entries) > self.max_entries:
            self.entries.pop(0)  # 移除最旧条目

        # 检查体积限制
        while self.size_bytes > self.max_size_bytes and self.entries:
            self.entries.pop(0)

    def format_as_markdown(self, include_header: bool = True) -> str:
        """将记忆文件格式化为 MEMORY.md 内容文本。

        【为什么需要】人类可读的 Markdown 格式方便用户直接查看和编辑记忆文件。
        MEMORY.md 与 memory.json 同时维护，既支持程序化操作也支持手动编辑。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 若要求头部，写入作用域标题和更新时间
        第2步: 将条目按分类分组
        第3步: 遍历每个分类，写入分类标题和条目列表
        第4步: 每条格式化为 "- 内容 `标签`" 的形式
        第5步: 返回拼接后的完整 Markdown 字符串
        ╚══════════════╝

        参数:
            include_header: 是否包含 # 标题和更新时间

        返回:
            MEMORY.md 格式的文本内容
        """
        lines = []

        if include_header:
            scope_names = {
                MemoryScope.USER: "User Memory",
                MemoryScope.PROJECT: "Project Memory",
                MemoryScope.LOCAL: "Local Memory",
            }
            lines.append(f"# {scope_names[self.scope]}")
            lines.append("")
            lines.append(f"*Last updated: {time.strftime('%Y-%m-%d %H:%M')}*")
            lines.append("")

        # 按分类分组
        categories: dict[str, list[MemoryEntry]] = {}
        for entry in self.entries:
            if entry.category not in categories:
                categories[entry.category] = []
            categories[entry.category].append(entry)

        for category, entries in categories.items():
            lines.append(f"## {category.title()}")
            lines.append("")
            for entry in entries:
                tags_str = f" `{' '.join(entry.tags)}`" if entry.tags else ""
                lines.append(f"- {entry.content}{tags_str}")
            lines.append("")

        return "\n".join(lines)




# ---------------------------------------------------------------------------
# 记忆管理器
# ---------------------------------------------------------------------------

@dataclass
class MemoryPaths:
    """存储不同作用域记忆文件的路径。

    【为什么需要】将路径管理集中在此类中，避免路径字符串散落在 MemoryManager
    各处。便于统一管理和修改文件存储位置。
    """
    user_memory: Path
    project_memory: Path
    local_memory: Path

    @classmethod
    def for_workspace(cls, workspace: str) -> MemoryPaths:
        """根据工作区路径创建各作用域的 MemoryPaths 实例。

        参数:
            workspace: 工作区路径字符串

        返回:
            MemoryPaths 实例，包含三个作用域的路径
        """
        workspace_path = Path(workspace)

        return cls(
            user_memory=MINI_CODE_DIR / "memory",
            project_memory=workspace_path / ".mini-code-memory",
            local_memory=workspace_path / ".mini-code-memory-local",
        )


class MemoryManager:
    """分层记忆系统管理器，核心入口类。

    【为什么需要】统一管理三层记忆（用户/项目/本地）的加载、保存、搜索、更新、
    分类、压缩、衰退、关联等全生命周期操作。通过 MemoryManager 对外提供统一的
    记忆读写接口，对内向系统提示词注入相关上下文。

    参数:
        workspace: 工作区路径（可选，用于确定项目级和本地级记忆目录）
        project_root: 已废弃的旧参数名，仅用于向后兼容
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
    ):
        """初始化记忆管理器，加载所有作用域的记忆数据。

        【为什么需要】构造函数不仅初始化路径和内存索引，还立即执行 _load_all
        将磁盘上的记忆文件加载到内存中，使 Manager 创建后即可使用。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 解析 workspace 参数（兼容旧的 project_root 参数）
        第2步: 为三个 MemoryScope 分别创建 MemoryFile 实例
        第3步: 调用 _load_all 从磁盘加载所有记忆数据
        ╚══════════════╝

        参数:
            workspace: 工作区路径
            project_root: 已废弃，为向后兼容保留
        """
        # 向后兼容：早期调用方使用 project_root 参数
        resolved_workspace = workspace if workspace is not None else project_root
        if resolved_workspace is None:
            resolved_workspace = Path.cwd()

        self.workspace = str(resolved_workspace)
        self.paths = MemoryPaths.for_workspace(self.workspace)
        self.memories: dict[MemoryScope, MemoryFile] = {
            MemoryScope.USER: MemoryFile(scope=MemoryScope.USER),
            MemoryScope.PROJECT: MemoryFile(scope=MemoryScope.PROJECT),
            MemoryScope.LOCAL: MemoryFile(scope=MemoryScope.LOCAL),
        }
        self._load_all()

    def _load_all(self) -> None:
        """加载所有作用域的记忆文件。

        依次为 USER/PROJECT/LOCAL 三个作用域加载记忆数据，
        加载后自动检查完整性并尝试修复。
        """
        for scope in MemoryScope:
            self._load_scope(scope)
            self._auto_recover_scope(scope)

    def _auto_recover_scope(self, scope: MemoryScope) -> None:
        """检查作用域的完整性，发现问题时自动修复。

        【为什么需要】记忆文件可能因写入中断、并发操作等导致数据损坏，
        自动检测并修复可以减少用户手动干预的成本。

        参数:
            scope: 待检查的作用域
        """
        result = self.check_integrity(scope)
        if not result["is_valid"]:
            logger.warning(
                "Integrity check failed for scope %s: %d issues found. "
                "Attempting auto-recovery...",
                scope.value,
                len(result["issues"]),
            )
            self._recover_scope(scope)

    def _recover_scope(self, scope: MemoryScope) -> None:
        """对存在完整性问题的作用域执行恢复。

        【为什么需要】当自动检测到数据问题后，需要实际执行修复操作：
        删除无效 id 的条目、去重、修复空 content/category 的条目。

        参数:
            scope: 待恢复的作用域
        """
        entries = self.memories[scope].entries
        seen_ids: set[str] = set()
        recovered: list[MemoryEntry] = []
        removed_count = 0
        fixed_count = 0

        for entry in entries:
            if not entry.id or not isinstance(entry.id, str):
                logger.warning(
                    "Removing entry with invalid ID during recovery"
                )
                removed_count += 1
                continue

            if entry.id in seen_ids:
                logger.warning(
                    "Removing duplicate entry with ID '%s'", entry.id
                )
                removed_count += 1
                continue

            if not entry.category or not isinstance(entry.category, str):
                entry.category = "general"
                fixed_count += 1

            if not entry.content or not isinstance(entry.content, str):
                logger.warning(
                    "Removing entry '%s' with empty content", entry.id
                )
                removed_count += 1
                continue

            seen_ids.add(entry.id)
            recovered.append(entry)

        self.memories[scope].entries = recovered
        self._save_scope(scope)

        logger.info(
            "Recovery complete for scope %s: %d entries recovered, "
            "%d removed, %d fixed",
            scope.value,
            len(recovered),
            removed_count,
            fixed_count,
        )

    def _load_scope(self, scope: MemoryScope) -> None:
        """从磁盘加载指定作用域的记忆文件。

        【为什么需要】程序启动时需要将持久化的记忆数据加载到内存中。
        优先加载 memory.json（含完整元数据），若损坏则回退解析 MEMORY.md。

        参数:
            scope: 待加载的作用域
        """
        path = self._get_scope_path(scope)
        memory_md = path / "MEMORY.md"
        memory_json = path / "memory.json"

        if not memory_md.exists() and not memory_json.exists():
            return

        # 若存在则加载 JSON 元数据
        if memory_json.exists():
            try:
                raw_text = memory_json.read_text(encoding="utf-8")
                data = json.loads(raw_text)

                is_valid, errors = _validate_memory_data(data)
                if is_valid:
                    for entry_data in data.get("entries", []):
                        entry = MemoryEntry.from_dict(entry_data)
                        self.memories[scope].entries.append(entry)
                    self.memories[scope]._rebuild_indices()
                    return
                else:
                    logger.warning(
                        "Memory data validation failed for scope %s: %s",
                        scope.value,
                        "; ".join(errors[:5]),
                    )
                    valid_entries = _recover_entries(data, memory_json)
                    for entry_data in valid_entries:
                        entry = MemoryEntry.from_dict(entry_data)
                        self.memories[scope].entries.append(entry)
                    if valid_entries:
                        self._save_scope(scope)
                    self.memories[scope]._rebuild_indices()
                    return
            except json.JSONDecodeError as e:
                logger.error(
                    "JSON decode error in scope %s: %s", scope.value, e
                )
            except KeyError as e:
                logger.error(
                    "Missing key in scope %s data: %s", scope.value, e
                )

        # 若存在则从 MEMORY.md 加载
        if memory_md.exists():
            content = memory_md.read_text(encoding="utf-8")
            self._parse_memory_md(content, scope)

    def _parse_memory_md(self, content: str, scope: MemoryScope) -> None:
        """解析 MEMORY.md 文件内容为 MemoryEntry 对象列表。

        【为什么需要】支持用户直接编辑 MEMORY.md 文件来管理记忆，
        解析器将 Markdown 格式的行（- 内容 `标签`）转换为结构化条目。

        参数:
            content: MEMORY.md 的文本内容
            scope: 所属作用域
        """
        lines = content.split("\n")
        current_category = "general"
        entry_counter = 0

        for line in lines:
            line = line.strip()

            # 跳过标题行和元数据行
            if line.startswith("#") or line.startswith("*") or not line:
                if line.startswith("## "):
                    current_category = line[3:].strip().lower()
                continue

            # 解析列表项
            if line.startswith("- "):
                entry_content = line[2:]

                # 提取标签
                tags = []
                if "`" in entry_content:
                    import re
                    tag_matches = re.findall(r"`([^`]+)`", entry_content)
                    for tag_match in tag_matches:
                        tags.extend(tag_match.split())
                    entry_content = re.sub(r"`[^`]+`", "", entry_content).strip()

                entry_counter += 1
                entry = MemoryEntry(
                    id=f"{scope.value}-{entry_counter}",
                    scope=scope,
                    category=current_category,
                    content=entry_content,
                    tags=tags,
                )
                self.memories[scope].entries.append(entry)
        # 基于 Markdown 加载完成后重建索引
        if self.memories[scope].entries:
            self.memories[scope]._rebuild_indices()

    def _get_scope_path(self, scope: MemoryScope) -> Path:
        """获取指定作用域的磁盘路径。"""
        if scope == MemoryScope.USER:
            return self.paths.user_memory
        elif scope == MemoryScope.PROJECT:
            return self.paths.project_memory
        else:
            return self.paths.local_memory

    def _ensure_scope_path(self, scope: MemoryScope) -> None:
        """确保作用域的目录存在，不存在则创建。"""
        path = self._get_scope_path(scope)
        path.mkdir(parents=True, exist_ok=True)

    def add_entry(
        self,
        scope: MemoryScope,
        category: str = "auto",
        content: str = "",
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """添加新的记忆条目（核心写入口）。

        【为什么需要】外部代码通过此方法写入记忆。支持自动分类（category="auto"），
        条目创建后自动保存到磁盘，确保数据持久化。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 确保作用域目录存在
        第2步: 若 category 为 "auto" 且 content 非空，调用 _auto_classify_content 自动分类
        第3步: 基于时间戳和条目计数生成唯一 id
        第4步: 构造 MemoryEntry 实例并添加到对应 MemoryFile
        第5步: 调用 _save_scope 持久化到磁盘
        第6步: 返回创建的条目
        ╚══════════════╝

        参数:
            scope: 记忆作用域
            category: 分类名称，传入 "auto" 时自动分类
            content: 记忆内容文本
            tags: 可选的标签列表

        返回:
            创建的 MemoryEntry 实例
        """
        self._ensure_scope_path(scope)

        final_category = category
        final_tags = tags or []

        if category == "auto" and content:
            auto_category, auto_tags = _auto_classify_content(content)
            final_category = auto_category
            final_tags = list(dict.fromkeys(final_tags + auto_tags))

        entry_id = f"{scope.value}-{int(time.time())}-{len(self.memories[scope].entries)}"
        entry = MemoryEntry(
            id=entry_id,
            scope=scope,
            category=final_category,
            content=content,
            tags=final_tags,
        )

        self.memories[scope].add_entry(entry)
        self._save_scope(scope)
        return entry

    def update_entry(self, scope: MemoryScope, entry_id: str, content: str) -> bool:
        """更新指定作用域中的一条记忆条目。

        【为什么需要】提供间接更新接口，委托给 MemoryFile.update_entry 后
        自动保存到磁盘，确保数据一致性。

        参数:
            scope: 条目所在的作用域
            entry_id: 条目标识符
            content: 新的内容文本

        返回:
            是否成功更新
        """
        if self.memories[scope].update_entry(entry_id, content):
            self._save_scope(scope)
            return True
        return False

    def delete_entry(self, scope: MemoryScope, entry_id: str) -> bool:
        """删除指定作用域中的一条记忆条目。

        【为什么需要】删除后自动同步保存到磁盘，防止删除操作因未保存而丢失。

        参数:
            scope: 条目所在的作用域
            entry_id: 条目标识符

        返回:
            是否成功删除
        """
        if self.memories[scope].delete_entry(entry_id):
            self._save_scope(scope)
            return True
        return False

    def add_tag(self, scope: MemoryScope, entry_id: str, tag: str) -> bool:
        """为条目添加标签。"""
        for entry in self.memories[scope].entries:
            if entry.id == entry_id:
                if tag not in entry.tags:
                    entry.tags.append(tag)
                    self._save_scope(scope)
                return True
        return False

    def remove_tag(self, scope: MemoryScope, entry_id: str, tag: str) -> bool:
        """移除条目的一个标签。"""
        for entry in self.memories[scope].entries:
            if entry.id == entry_id:
                if tag in entry.tags:
                    entry.tags.remove(tag)
                    self._save_scope(scope)
                return True
        return False

    def search_by_tag(self, scope: MemoryScope, tag: str) -> list[MemoryEntry]:
        """按标签搜索条目。

        参数:
            scope: 搜索的作用域
            tag: 标签名

        返回:
            包含该标签的条目列表
        """
        return [
            entry for entry in self.memories[scope].entries
            if tag in entry.tags
        ]

    def get_all_tags(self, scope: MemoryScope) -> set[str]:
        """获取作用域中所有不重复的标签。

        参数:
            scope: 作用域

        返回:
            标签集合
        """
        tags: set[str] = set()
        for entry in self.memories[scope].entries:
            tags.update(entry.tags)
        return tags

    def get_tags_by_category(self, scope: MemoryScope) -> dict[str, list[str]]:
        """按分类分组的标签列表。

        参数:
            scope: 作用域

        返回:
            分类到标签列表的映射字典
        """
        category_tags: dict[str, set[str]] = {}
        for entry in self.memories[scope].entries:
            if entry.category not in category_tags:
                category_tags[entry.category] = set()
            category_tags[entry.category].update(entry.tags)
        return {cat: sorted(list(tags)) for cat, tags in category_tags.items()}

    def search(
        self,
        query: str,
        scope: MemoryScope | None = None,
        limit: int = 20,
        min_relevance: float = 0.1,
        active_domains: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """跨作用域搜索记忆条目，综合排序。

        【为什么需要】核心搜索接口，聚合三个作用域的结果，经过最小相关性过滤、
        内容去重后返回最相关的结果。是 get_relevant_context 的下层搜索基础。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 确定搜索范围（指定作用域或全部三个作用域）
        第2步: 在每个作用域上调用 MemoryFile.search 执行 BM25 搜索
        第3步: 若指定了 min_relevance，归一化得分后过滤低相关结果
        第4步: 内容去重（取前 100 字符作为键，保留得分高的）
        第5步: 截取前 limit 条返回
        ╚══════════════╝

        参数:
            query: 搜索查询字符串
            scope: 可选的作用域限制
            limit: 最大返回结果数
            min_relevance: 最小相关性阈值（0.0-1.0）
            active_domains: 当前活跃领域列表，用于软提升

        返回:
            按相关性降序排列的记忆条目列表
        """
        results = []

        scopes_to_search = [scope] if scope else list(MemoryScope)

        for s in scopes_to_search:
            results.extend(self.memories[s].search(query, active_domains=active_domains))

        # 应用最小相关性阈值过滤
        # （条目已经由 MemoryFile.search 评过分）
        if min_relevance > 0:
            # 将得分归一化到 0-1 范围再进行阈值比较
            if results:
                max_score = max(
                    self._score_entry(e, _tokenize(query)) for e in results
                )
                if max_score > 0:
                    results = [
                        e for e in results
                        if self._score_entry(e, _tokenize(query)) / max_score >= min_relevance
                    ]

        # 结果已由 MemoryFile.search() 排序
        # 按内容去重（保留得分最高的）
        seen_content: set[str] = set()
        deduped = []
        for entry in results:
            content_key = entry.content[:100].strip().lower()
            if content_key not in seen_content:
                seen_content.add(content_key)
                deduped.append(entry)

        return deduped[:limit]

    def _score_entry(self, entry: MemoryEntry, query_tokens: list[str]) -> float:
        """计算单条记忆条目与查询之间的综合相关性得分。

        【为什么需要】在 min_relevance 阈值过滤时，需要独立于 MemoryFile.search
        对单一条目重新评分（因为跨作用域搜索时不能直接用原始得分跨域比较）。
        融合 BM25、子串匹配、标签匹配、使用频率和新近度五维信号。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 扩展查询词并计算条目 BM25 得分
        第2步: 若查询字符串在内容中则加 2.0，有词匹配加 1.0
        第3步: 精确标签匹配加 5.0，部分匹配加 1.5，分类匹配加 1.0
        第4步: 使用频率奖励 log1p(usage_count) * 0.3
        第5步: 时间新近度奖励 1/(1+age_hours/24) * 0.5
        第6步: 返回所有得分的加权和
        ╚══════════════╝

        参数:
            entry: 待评分的记忆条目
            query_tokens: 查询词元列表

        返回:
            综合相关性得分数值
        """
        if not query_tokens:
            return 0.0

        query_tokens_expanded = _expand_query_terms(query_tokens)
        entry_tokens = _tokenize(
            f"{entry.content} {entry.category} {' '.join(entry.tags)}"
        )
        idf = _compute_idf([entry_tokens])
        avgdl = len(entry_tokens)
        bm25 = _bm25_score(query_tokens_expanded, entry_tokens, idf, avgdl)

        query_lower = " ".join(query_tokens).lower()
        content_lower = entry.content.lower()
        substring_score = 0.0
        if query_lower in content_lower:
            substring_score = 2.0
        elif any(q in content_lower for q in query_tokens):
            substring_score = 1.0

        tag_score = 0.0
        exact_tag_match = any(tag.lower() == query_lower for tag in entry.tags)
        partial_tag_match = any(query_lower in tag.lower() for tag in entry.tags)
        if exact_tag_match:
            tag_score = 5.0
        elif partial_tag_match:
            tag_score = 1.5
        if query_lower in entry.category.lower():
            tag_score += 1.0

        usage_bonus = math.log1p(entry.usage_count) * 0.3

        age_hours = (time.time() - entry.updated_at) / 3600
        recency_bonus = 1.0 / (1.0 + age_hours / 24.0) * 0.5

        return bm25 + substring_score + tag_score + usage_bonus + recency_bonus

    def get_relevant_context(
        self,
        max_entries: int = 20,
        max_tokens: int = 8000,
        query: str | None = None,
    ) -> str:
        """获取相关记忆上下文，用于注入到系统提示词中。

        【为什么需要】每次 AI 对话前需要将相关记忆拼接为格式化文本注入系统提示词，
        让 AI 了解之前会话的决策和约定。此方法在 token 预算内智能选择最相关的内容。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 若有查询，对每个作用域执行 search 搜索，按 token 预算逐步添加
        第2步: 若无查询，按 LOCAL > PROJECT > USER 优先级依次添加完整记忆文件
        第3步: 每个部分按 token 预算检查，超限时截取最近的条目
        第4步: 返回所有作用域 Markdown 格式文本的拼接结果
        ╚══════════════╝

        参数:
            max_entries: 每个作用域最大条目数
            max_tokens: 总 token 预算上限（默认 8000）
            query: 可选的搜索查询，用于检索相关内容而非全部记忆

        返回:
            格式化后的记忆上下文 Markdown 字符串，若无内容则返回空字符串
        """
        from minicode.context_manager import estimate_tokens

        query = (query or "").strip()
        if query:
            scoped_parts = []
            total_tokens = 0
            for scope in [MemoryScope.LOCAL, MemoryScope.PROJECT, MemoryScope.USER]:
                entries = self.search(query, scope=scope, limit=max_entries, min_relevance=0.0)
                if not entries:
                    continue
                accepted_entries: list[MemoryEntry] = []
                for entry in entries[:max_entries]:
                    candidate_memory = MemoryFile(scope=scope, entries=[*accepted_entries, entry])
                    candidate = candidate_memory.format_as_markdown(include_header=True)
                    candidate_tokens = estimate_tokens(candidate)
                    if total_tokens + candidate_tokens <= max_tokens:
                        accepted_entries.append(entry)
                        continue
                    if not accepted_entries:
                        # 跳过过大的结果，而不是阻塞可能含有紧凑相关上下文的低优先级作用域
                        continue
                    break
                if not accepted_entries:
                    continue
                formatted = MemoryFile(scope=scope, entries=accepted_entries).format_as_markdown(include_header=True)
                scoped_parts.append(formatted)
                total_tokens += estimate_tokens(formatted)
            if scoped_parts:
                return "\n\n".join(scoped_parts)
            return ""

        parts = []
        total_tokens = 0

        # 优先级顺序：本地 > 项目 > 用户
        for scope in [MemoryScope.LOCAL, MemoryScope.PROJECT, MemoryScope.USER]:
            memory = self.memories[scope]
            if not memory.entries:
                continue

            formatted = memory.format_as_markdown(include_header=True)
            tokens = estimate_tokens(formatted)

            if total_tokens + tokens <= max_tokens:
                parts.append(formatted)
                total_tokens += tokens
            else:
                # 部分加载：只包含最近的条目
                remaining_tokens = max_tokens - total_tokens
                partial_entries = memory.entries[-max_entries:]
                partial_memory = MemoryFile(scope=scope, entries=partial_entries)
                formatted = partial_memory.format_as_markdown(include_header=True)

                if estimate_tokens(formatted) <= remaining_tokens:
                    parts.append(formatted)
                break

        if not parts:
            return ""

        return "\n\n".join(parts)

    def _save_scope(self, scope: MemoryScope) -> None:
        """将作用域记忆持久化到磁盘（原子写入防损坏）。

        【为什么需要】每次增删改操作后同步写入磁盘，确保数据持久化。
        同时保存 JSON（程序读取）和 Markdown（人类阅读）两种格式，
        使用原子写入防止写入中断导致文件损坏。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 确保作用域目录存在
        第2步: 将条目序列化为 JSON 并用原子写入保存到 memory.json
        第3步: 将条目格式化为 Markdown 并用原子写入保存到 MEMORY.md
        ╚══════════════╝

        参数:
            scope: 待保存的作用域
        """
        path = self._get_scope_path(scope)
        self._ensure_scope_path(scope)

        # 保存 JSON 元数据（原子写入：先写临时文件再替换）
        memory_json = path / "memory.json"
        data = {
            "scope": scope.value,
            "last_updated": time.time(),
            "entries": [e.to_dict() for e in self.memories[scope].entries],
        }
        self._atomic_write(memory_json, json.dumps(data, indent=2, ensure_ascii=False))

        # 同时更新 MEMORY.md 便于人类阅读（原子写入）
        memory_md = path / "MEMORY.md"
        self._atomic_write(memory_md, self.memories[scope].format_as_markdown())

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        """原子写入文件：先写临时文件，再通过 os.replace() 替换。

        【为什么需要】防止写入过程中进程被杀死导致文件损坏，也防止多个实例
        同时写入同一个文件造成数据竞争。临时文件写在目标文件同目录下以确保
        跨设备原子替换有效。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 在目标文件同目录创建临时文件
        第2步: 写入内容到临时文件
        第3步: 用 os.replace 原子地替换目标文件
        第4步: 若失败，清理临时文件后重新抛出异常
        ╚══════════════╝

        参数:
            target: 目标文件路径
            content: 待写入的文本内容
        """
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(target))
        except BaseException:
            # 任何失败都要清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_stats(self) -> dict[str, Any]:
        """获取各作用域的记忆统计信息。

        返回:
            作用域名到统计信息的映射字典
        """
        return {
            scope.value: {
                "entries": len(memory.entries),
                "size_bytes": memory.size_bytes,
                "categories": list(set(e.category for e in memory.entries)),
            }
            for scope, memory in self.memories.items()
        }

    def format_stats(self) -> str:
        """格式化记忆统计信息为可读字符串，含层级和领域分布。"""
        from collections import Counter

        lines = ["Memory System Status", "=" * 50, ""]
        tiers: Counter[str] = Counter()
        domains: Counter[str] = Counter()
        total_entries = 0
        total_size = 0
        insight_count = 0

        for scope_name, scope_stats in self.get_stats().items():
            lines.append(f"{scope_name.title()}: {scope_stats['entries']} entries, "
                        f"{scope_stats['size_bytes'] / 1024:.1f} KB")
            total_entries += scope_stats["entries"]
            total_size += scope_stats["size_bytes"]

            # 收集层级和领域统计
            scope = MemoryScope(scope_name)
            if scope in self.memories:
                for e in self.memories[scope].entries:
                    tiers[e.tier.value] += 1
                    for d in e.domains:
                        domains[d] += 1
                    if e.category == "insight":
                        insight_count += 1

        lines.append("")
        lines.append(f"Total: {total_entries} entries ({total_size / 1024:.1f} KB)")
        lines.append("")

        if tiers:
            lines.append("Tier Distribution:")
            for tier_name in ["working", "short_term", "long_term", "archival"]:
                count = tiers.get(tier_name, 0)
                bar = "#" * (count // max(1, total_entries // 20))
                lines.append(f"  {tier_name:<12} {count:>4} {bar}")
            lines.append("")

        if domains:
            lines.append("Domain Distribution:")
            for domain, count in domains.most_common(6):
                lines.append(f"  {domain:<15} {count:>3}")
            lines.append("")

        if insight_count:
            lines.append(f"Curator Insights: {insight_count} synthesized")

        return "\n".join(lines)

    def clear_scope(self, scope: MemoryScope) -> None:
        """清空指定作用域的所有条目。

        参数:
            scope: 待清空的作用域
        """
        self.memories[scope] = MemoryFile(scope=scope)
        self._save_scope(scope)

    def handle_user_memory_input(self, user_input: str) -> str | None:
        """处理来自主聊天界面的显式记忆输入指令。

        【为什么需要】用户可以通过聊天直接输入指令来创建记忆，无需通过编程接口。
        支持多种格式：以 # 开头的指令、/memory add 命令、以及带作用域前缀的变体。

        支持格式:
        - "# remember this project convention"
        - "/memory add remember this project convention"
        - "/memory add project: remember this shared project convention"
        - "/memory add local: remember this local-only note"
        - "/memory add user: remember this cross-project preference"

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 判断输入格式（# 开头或 /memory add 开头），否则返回 None
        第2步: 解析作用域（默认为 PROJECT）和内容
        第3步: 调用 add_entry 保存记忆
        第4步: 返回操作结果提示字符串
        ╚══════════════╝

        参数:
            user_input: 用户输入的原始文本

        返回:
            操作结果提示字符串，非记忆指令则返回 None
        """
        raw = user_input.strip()
        if not raw:
            return None

        content = ""
        scope = MemoryScope.PROJECT
        category = "note"

        if raw.startswith("#"):
            content = raw[1:].strip()
            category = "directive"
        elif raw.startswith("/memory add "):
            content = raw[len("/memory add ") :].strip()
            scope_match = re.match(r"^(user|project|local)\s*:\s*(.+)$", content, flags=re.I)
            if scope_match:
                scope = MemoryScope(scope_match.group(1).lower())
                content = scope_match.group(2).strip()
        else:
            return None

        if not content:
            return "Usage: # <memory> or /memory add [user|project|local:] <memory>"

        entry = self.add_entry(scope, category, content, tags=["chat"])
        return f"Saved memory ({entry.scope.value}): {entry.content}"

    def check_integrity(self, scope: MemoryScope) -> dict[str, Any]:
        """校验指定作用域所有条目的完整性。

        【为什么需要】定期或加载后检查条目数据完整性，检测无效 id、空内容、
        重复 id 等问题，确保记忆系统数据的可靠性。

        参数:
            scope: 待检查的作用域

        返回:
            含 {is_valid: bool, issues: list[str]} 的字典
        """
        issues: list[str] = []
        seen_ids: set[str] = set()
        entries = self.memories[scope].entries

        for idx, entry in enumerate(entries):
            if not entry.id or not isinstance(entry.id, str):
                issues.append(
                    f"Entry at index {idx} has invalid or empty ID"
                )

            if entry.id in seen_ids:
                issues.append(
                    f"Duplicate ID found: '{entry.id}' "
                    f"(entries {list(self._find_entry_indices(scope, entry.id))})"
                )
            else:
                seen_ids.add(entry.id)

            if not entry.category or not isinstance(entry.category, str):
                issues.append(
                    f"Entry '{entry.id}' has invalid or empty category"
                )

            if not entry.content or not isinstance(entry.content, str):
                issues.append(
                    f"Entry '{entry.id}' has empty or invalid content"
                )

        return {
            "is_valid": len(issues) == 0,
            "issues": issues,
        }

    def compress_scope(
        self, scope: MemoryScope, similarity_threshold: float = 0.8
    ) -> dict[str, int]:
        """通过合并相似内容压缩记忆条目。

        【为什么需要】随着时间推移，记忆文件可能积累类似或重复的条目，
        压缩可以节省空间并减少噪音。合并超过相似度阈值的条目，删除完全重复的条目。

        参数:
            scope: 待压缩的作用域
            similarity_threshold: Jaccard 相似度阈值（默认 0.8）

        返回:
            含 {merged_count, removed_count, remaining_count} 的统计字典
        """
        entries = self.memories[scope].entries
        if len(entries) <= 1:
            return {"merged_count": 0, "removed_count": 0, "remaining_count": len(entries)}

        seen_content: dict[str, int] = {}
        duplicates_removed = 0

        unique_entries = []
        for entry in entries:
            content_key = entry.content.strip().lower()
            if content_key in seen_content:
                master_idx = seen_content[content_key]
                master = unique_entries[master_idx]
                master.usage_count += entry.usage_count
                master.updated_at = max(master.updated_at, entry.updated_at)
                master.tags = sorted(
                    list(set(master.tags + entry.tags))
                )
                duplicates_removed += 1
            else:
                seen_content[content_key] = len(unique_entries)
                unique_entries.append(entry)

        merged_count = 0
        final_entries: list[MemoryEntry] = []
        merged_indices: set[int] = set()

        for i, entry_a in enumerate(unique_entries):
            if i in merged_indices:
                continue

            best_match_idx = None
            best_similarity = 0.0

            for j, entry_b in enumerate(unique_entries):
                if i == j or j in merged_indices:
                    continue

                similarity = self._jaccard_similarity(
                    entry_a.content, entry_b.content
                )
                if similarity >= similarity_threshold and similarity > best_similarity:
                    best_similarity = similarity
                    best_match_idx = j

            if best_match_idx is not None:
                entry_b = unique_entries[best_match_idx]
                merged_content = self._merge_entry_content(
                    entry_a.content, entry_b.content
                )
                entry_a.content = merged_content
                entry_a.usage_count += entry_b.usage_count
                entry_a.updated_at = max(
                    entry_a.updated_at, entry_b.updated_at
                )
                entry_a.tags = sorted(
                    list(set(entry_a.tags + entry_b.tags))
                )
                merged_indices.add(best_match_idx)
                merged_count += 1

            final_entries.append(entry_a)

        self.memories[scope].entries = final_entries
        self._save_scope(scope)

        return {
            "merged_count": merged_count,
            "removed_count": duplicates_removed,
            "remaining_count": len(final_entries),
        }

    @staticmethod
    def _jaccard_similarity(text_a: str, text_b: str) -> float:
        """计算两段文本之间的 Jaccard 相似度。

        【为什么需要】在压缩和冲突检测中需要量化两段文本的相似程度，
        Jaccard 相似度 = |A ∩ B| / |A ∪ B| 简单有效。

        参数:
            text_a: 第一段文本
            text_b: 第二段文本

        返回:
            0.0 到 1.0 之间的相似度得分
        """
        tokens_a = set(_tokenize(text_a))
        tokens_b = set(_tokenize(text_b))

        if not tokens_a and not tokens_b:
            return 1.0
        if not tokens_a or not tokens_b:
            return 0.0

        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b

        return len(intersection) / len(union)

    @staticmethod
    def _merge_entry_content(content_a: str, content_b: str) -> str:
        """合并两段相似的内容文本。

        【为什么需要】压缩时合并相似条目，保留较长版本以保留更多细节。

        参数:
            content_a: 第一段内容
            content_b: 第二段内容

        返回:
            合并后的内容文本
        """
        if len(content_a) >= len(content_b):
            return content_a
        return content_b

    def detect_conflicts(self, content: str, scope: MemoryScope | None = None, threshold: float = 0.6) -> list[tuple[MemoryEntry, float]]:
        """检测新内容与现有记忆之间的潜在冲突。

        【为什么需要】添加新记忆时，如果与现有记忆内容高度相似，可能是重复
        或矛盾的。通过 Jaccard 相似度识别需要合并或关注的条目。

        参数:
            content: 待检查的新记忆内容
            scope: 检查范围（None 表示全部作用域）
            threshold: 冲突判定阈值（0.0-1.0）

        返回:
            按相似度降序排列的 (条目, 相似度) 列表
        """
        new_tokens = set(_tokenize(content))
        if not new_tokens:
            return []

        conflicts: list[tuple[MemoryEntry, float]] = []
        scopes = [scope] if scope else list(MemoryScope)

        for s in scopes:
            if s not in self.memories:
                continue
            for entry in self.memories[s].entries:
                old_tokens = set(entry.get_tokens())
                if not old_tokens:
                    continue
                intersection = new_tokens & old_tokens
                union = new_tokens | old_tokens
                similarity = len(intersection) / len(union) if union else 0.0
                if similarity >= threshold:
                    conflicts.append((entry, similarity))

        conflicts.sort(key=lambda x: x[1], reverse=True)
        return conflicts

    def decay_memories(self, max_age_days: float = 30.0, decay_factor: float = 0.5) -> int:
        """对记忆使用计数应用基于时间的衰退。

        【为什么需要】超过一定天数的条目说明不再活跃，降低其使用计数
        可以减少其在搜索结果中的权重，实现"遗忘"机制。

        参数:
            max_age_days: 最大保留天数（默认 30 天）
            decay_factor: 衰退系数（默认 0.5，即减半）

        返回:
            被衰退的条目数量
        """
        now = time.time()
        decayed = 0
        for scope in MemoryScope:
            if scope not in self.memories:
                continue
            for entry in self.memories[scope].entries:
                age_days = (now - entry.updated_at) / 86400.0
                if age_days > max_age_days and entry.usage_count > 0:
                    entry.usage_count = max(0, int(entry.usage_count * decay_factor))
                    decayed += 1
        if decayed:
            for scope in MemoryScope:
                self._save_scope(scope)
        return decayed

    def promote_memories(self) -> dict[str, int]:
        """根据使用频率和年龄对记忆进行升降级。

        【为什么需要】实现记忆的自动化生命周期管理：高频使用的短期记忆升级为
        长期记忆；长期未访问的长期记忆降级为归档记忆；归档中被重新访问的记忆
        重新激活为短期记忆。WORKING → SHORT_TERM → LONG_TERM → ARCHIVAL

        返回:
            各操作计数 {promoted_to_long, demoted_to_archival, reactivated}
        """
        now = time.time()
        stats = {"promoted_to_long": 0, "demoted_to_archival": 0, "reactivated": 0}
        for scope in MemoryScope:
            if scope not in self.memories:
                continue
            for entry in self.memories[scope].entries:
                age_days = (now - entry.updated_at) / 86400.0
                accessed_days = (now - entry.last_accessed) / 86400.0
                if entry.tier == MemoryTier.SHORT_TERM and entry.usage_count >= 5 and age_days > 7:
                    entry.tier = MemoryTier.LONG_TERM
                    stats["promoted_to_long"] += 1
                if entry.tier == MemoryTier.LONG_TERM and accessed_days > 30:
                    entry.tier = MemoryTier.ARCHIVAL
                    entry.content = self._summarize_content(entry.content)
                    stats["demoted_to_archival"] += 1
                if entry.tier in (MemoryTier.LONG_TERM, MemoryTier.ARCHIVAL) and accessed_days < 7:
                    entry.tier = MemoryTier.SHORT_TERM
                    stats["reactivated"] += 1
        if any(stats.values()):
            for scope in MemoryScope:
                self._save_scope(scope)
        return stats

    def link_memories(self, similarity_threshold: float = 0.4) -> int:
        """通过内容相似度自动关联相关记忆。

        【为什么需要】自动发现并建立记忆之间的关联关系，使得后续可以通过
        get_linked_memories 查找相关联的记忆，构建记忆网络。

        参数:
            similarity_threshold: 关联判定的相似度阈值（默认 0.4）

        返回:
            创建的关联数量
        """
        links = 0
        for scope in MemoryScope:
            if scope not in self.memories:
                continue
            entries = self.memories[scope].entries
            for i, a in enumerate(entries):
                for j, b in enumerate(entries):
                    if i >= j:
                        continue
                    if b.id in a.related_to:
                        continue
                    if self._jaccard_similarity(a.content, b.content) >= similarity_threshold:
                        a.related_to.append(b.id)
                        b.related_to.append(a.id)
                        links += 2
        if links:
            for scope in MemoryScope:
                self._save_scope(scope)
        return links

    def get_linked_memories(self, entry_id: str, depth: int = 1) -> list[MemoryEntry]:
        """通过 related_to 关联图获取与指定条目相关联的记忆（BFS，限深度）。

        【为什么需要】当用户查看一条记忆时，可以同时展示与之相关的其他记忆，
        构建知识网络。使用广度优先搜索遍历关联图。

        参数:
            entry_id: 起始条目的 id
            depth: BFS 搜索深度（默认 1）

        返回:
            关联的记忆条目列表
        """
        entry = None
        found_scope = None
        for s in MemoryScope:
            if s in self.memories:
                entry = self.memories[s]._id_index.get(entry_id)
                if entry:
                    found_scope = s
                    break
        if not entry or not entry.related_to or not found_scope:
            return []
        visited = {entry_id}
        frontier = list(entry.related_to)
        results = []
        for _ in range(depth):
            nxt = []
            for rid in frontier:
                if rid in visited:
                    continue
                visited.add(rid)
                linked = self.memories[found_scope]._id_index.get(rid)
                if linked:
                    results.append(linked)
                    nxt.extend(linked.related_to)
            frontier = nxt
            if not frontier:
                break
        return results

    @staticmethod
    def _summarize_content(content: str, max_len: int = 150) -> str:
        """对长内容进行摘要，保留第一个完整句子的语义。

        【为什么需要】归档层级的记忆需要压缩以节省空间，摘要策略是在指定长度内
        寻找第一个语义完整的句子结束位置（句号/分号/换行），保留关键信息。

        执行流程（仅 及以上的方法需要）：
        ╔══ 完整执行流程 ══╗
        第1步: 若内容长度不超过 max_len，直接返回原文
        第2步: 依次查找句号、分号、换行等句子结束标记
        第3步: 若结束标记在 20~max_len 范围内，截取到标记处
        第4步: 若未找到合适的结束标记，在 max_len 处截断并追加 "..."
        ╚══════════════╝

        参数:
            content: 原始内容文本
            max_len: 摘要最大长度（默认 150）

        返回:
            摘要文本
        """
        if len(content) <= max_len:
            return content
        for sep in [". ", ".\n", "; ", ";\n", "\n"]:
            idx = content.find(sep)
            if 20 < idx < max_len:
                return content[:idx + 1]
        return content[:max_len] + "..."

    def _find_entry_indices(self, scope: MemoryScope, entry_id: str) -> list[int]:
        """查找指定 id 的所有条目索引位置（用于检测重复 id）。"""
        indices = []
        for idx, entry in enumerate(self.memories[scope].entries):
            if entry.id == entry_id:
                indices.append(idx)
        return indices


# ---------------------------------------------------------------------------
# 系统提示词集成
# ---------------------------------------------------------------------------

def inject_memory_into_prompt(
    system_prompt: str,
    memory_manager: MemoryManager,
    max_tokens: int = 8000,
) -> str:
    """将记忆上下文注入到系统提示词中。

    【为什么需要】每次 AI 对话前将相关记忆拼接到系统提示词末尾，
    让 AI 获得之前会话的上下文信息，实现跨会话持续学习。

    执行流程（仅 及以上的方法需要）：
    ╔══ 完整执行流程 ══╗
    第1步: 调用 memory_manager.get_relevant_context 获取记忆上下文
    第2步: 若没有记忆内容，直接返回原系统提示词
    第3步: 将记忆上下文作为 ## Project Memory & Context 追加到提示词后
    ╚══════════════╝

    参数:
        system_prompt: 原始系统提示词
        memory_manager: MemoryManager 实例
        max_tokens: 记忆上下文的最大 token 预算

    返回:
        注入了记忆上下文的系统提示词
    """
    memory_context = memory_manager.get_relevant_context(max_tokens=max_tokens)

    if not memory_context:
        return system_prompt

    return f"""{system_prompt}

## Project Memory & Context

The following information has been accumulated from previous sessions:

{memory_context}

Use this context to inform your decisions and follow established patterns."""


# ---------------------------------------------------------------------------
# CLI 命令
# ---------------------------------------------------------------------------

def format_memory_list(memory_manager=None, scope: MemoryScope | None = None, category: str | None = None) -> str:
    """将记忆条目格式化为 CLI 显示的文本。

    【为什么需要】提供命令行界面查看记忆的格式化输出，支持按作用域和分类过滤。

    参数:
        memory_manager: MemoryManager 实例
        scope: 可选的作用域过滤
        category: 可选的分类过滤

    返回:
        格式化后的记忆条目文本
    """
    if memory_manager is None:
        return "No MemoryManager available."

    # 收集指定作用域的条目
    scopes = [scope] if scope else list(MemoryScope)
    all_entries: list[MemoryEntry] = []
    for s in scopes:
        if s in memory_manager.memories:
            entries = memory_manager.memories[s].entries
            if category:
                entries = [e for e in entries if e.category == category]
            all_entries.extend(entries)

    if not all_entries:
        return "No memories found."

    lines = [f"{'=' * 60}"]
    for entry in all_entries[:20]:  # 最多显示 20 条
        scope_tag = f"[{entry.scope.value if hasattr(entry, 'scope') else '?'}]"
        cat_tag = f"[{entry.category}]"
        content_preview = entry.content[:100].replace('\n', ' ')
        lines.append(f"{scope_tag} {cat_tag} {content_preview}")
        if entry.tags:
            lines.append(f"     Tags: {', '.join(entry.tags[:5])}")
        lines.append(f"     Used: {entry.usage_count}x | Updated: {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.updated_at))}")
        lines.append("")
    lines.append(f"{'=' * 60}")
    lines.append(f"Total: {len(all_entries)} entries")
    return "\n".join(lines)
