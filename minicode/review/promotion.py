"""审查发现 → 全局记忆 / Skill 的沉淀。

核心流程：
  审查子 Agent 输出报告 → 入库 → Coda 阶段判断重要性
    → 重要：沉淀为 skill（.opencode/skills/）或全局记忆（MemoryScope.USER）
    → 不重要：留存在项目级 review-findings.json 中

什么重要（满足任一）：
  1. critical 安全发现（硬编码密钥/SQL 注入/eval）
  2. 同规则在本次 session 出现 ≥3 次
  3. 架构级规则（rule_id 以 arch- 开头）
  4. 跨项目通用模式（如 "不要用 os.path 用 pathlib"）
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("minicode.review.promotion")

# ---------------------------------------------------------------------------
# 重要性判断
# ---------------------------------------------------------------------------

# 安全关键 rule_id — 命中即沉淀
_CRITICAL_SECURITY_RULES = frozenset({
    "hardcoded-secret",
    "sqli",
    "unsafe-eval",
    "hardcoded-password",
})

# 跨项目通用模式 — 命中即沉淀
_CROSS_PROJECT_PATTERNS = [
    # 使用 pathlib 代替 os.path
    (r"\bos\.path\.(join|exists|isdir|isfile|abspath)\b",
     "用 pathlib.Path 替代 os.path 操作"),
    # 使用 f-string 代替 %
    (r"['\"].*%[sdr].*['\"]\s*%",
     "用 f-string 替代 % 格式化"),
    # 使用 context manager 打开文件
    (r"(?:open|file)\(.*\)\.(?:read|write|close)\s*\(",
     "用 with open(...) as f: 替代手动 open/close"),
    # 使用 try/except 做流程控制
    (r"try\s*:\s*\n\s+(?:return|pass)\s*\n\s*except\b",
     "不要用异常做流程控制"),
    # 硬编码路径
    (r"['\"]/(?:tmp|var|etc|usr|home)/['\"]",
     "用 Path.tempdir() / Path.home() 替代硬编码路径"),
]

_SKILL_DIR = Path.home() / ".opencode" / "skills"


def _line_hash(file_path: str, line: str) -> str:
    """基于文件路径 + 行内容的稳定 hash，不依赖行号（行号会变）。

    用于 false_positive 的 id 生成。
    """
    sig = f"{file_path}:{line.strip()[:60]}"
    return hashlib.md5(sig.encode()).hexdigest()[:12]


def _is_cross_project_pattern(rule_id: str, description: str) -> tuple[bool, str]:
    """判断是否跨项目通用模式。

    返回 (是否匹配, 建议标题)
    """
    for pattern, title in _CROSS_PROJECT_PATTERNS:
        if re.search(pattern, description, re.IGNORECASE):
            return True, title
    return False, ""


def is_important_finding(
    finding: dict[str, Any],
    rule_counts: Counter[str] | None = None,
) -> str | None:
    """判断一条审查发现是否值得沉淀。

    参数:
        finding: 审查发现 dict，含 severity, rule_id, description, file_path 等
        rule_counts: 本次 session 中各 rule_id 出现次数统计

    返回:
        None  — 不沉淀
        "skill"          — 沉淀为 skill（.opencode/skills/<rule_id>.md）
        "global_memory"  — 沉淀到全局记忆（MemoryScope.USER）
    """
    severity = finding.get("severity", "")
    rule_id = finding.get("rule_id", "")
    description = finding.get("description", "")

    # 1. 已经标记为 false_positive 的跳过
    if finding.get("status") == "false_positive":
        return None

    # 2. 风格类 skip（lint 已覆盖）
    if severity in ("style", "info") and rule_id not in _CRITICAL_SECURITY_RULES:
        return None

    # 3. critical 安全发现 → skill（任何项目都适用）
    if severity == "critical" and rule_id in _CRITICAL_SECURITY_RULES:
        return "skill"

    # 4. 架构级规则 → 全局记忆
    if rule_id.startswith("arch-"):
        return "global_memory"

    # 5. 高频重复 ≥3 次 → skill
    if rule_counts and rule_counts.get(rule_id, 0) >= 3:
        return "skill"

    # 6. 跨项目通用模式 → skill
    is_cross, _ = _is_cross_project_pattern(rule_id, description)
    if is_cross:
        return "skill"

    return None


# ---------------------------------------------------------------------------
# 沉淀到 Skill
# ---------------------------------------------------------------------------

_SKILL_TEMPLATE = """# {title}

**Severity:** {severity}
**Rule:** {rule_id}
**Discovered:** {date}

## 问题描述

{description}

## 推荐做法

{recommendation}

## 示例

```python
# Bad — {rule_id} 违规模式
{bad_example}

# Good — 推荐写法
{good_example}
```
"""


def _format_skill_content(
    finding: dict[str, Any],
) -> str:
    """生成 skill markdown 内容。"""
    return _SKILL_TEMPLATE.format(
        title=finding.get("title") or finding.get("rule_id", "unknown"),
        severity=finding.get("severity", "warning"),
        rule_id=finding.get("rule_id", "unknown"),
        date=time.strftime("%Y-%m-%d"),
        description=finding.get("description", ""),
        recommendation=finding.get("recommendation", ""),
        bad_example=finding.get("bad_example", "# 待补充"),
        good_example=finding.get("good_example", "# 待补充"),
    )


def promote_to_skill(finding: dict[str, Any]) -> bool:
    """将审查发现沉淀为可复用的 skill 文件。

    写入 ~/.opencode/skills/<rule_id>.md，跨项目生效。
    已存在的 skill 不会覆写（防止用户自定义内容被覆盖）。
    """
    rule_id = finding.get("rule_id", "unknown")
    skill_path = _SKILL_DIR / f"{rule_id}.md"

    if skill_path.exists():
        logger.debug("Skill %s already exists, skipping", rule_id)
        return True  # 不覆盖

    _SKILL_DIR.mkdir(parents=True, exist_ok=True)
    content = _format_skill_content(finding)
    skill_path.write_text(content, encoding="utf-8")

    logger.info("Promoted finding %s to skill: %s", rule_id, skill_path)
    return True


# ---------------------------------------------------------------------------
# 沉淀到全局记忆
# ---------------------------------------------------------------------------

def promote_to_global_memory(finding: dict[str, Any], cwd: str) -> bool:
    """将审查发现写入全局记忆（跨项目共享）。

    使用 MemoryScope.USER，项目无关的通用知识。
    """
    try:
        from minicode.memory import MemoryManager, MemoryScope

        memory = MemoryManager(project_root=Path(cwd))

        if finding.get("severity") == "critical":
            category = "security-issue"
        else:
            category = "code-smell"

        content = (
            f"[Review Pattern] {finding.get('file_path', '')}: "
            f"{finding.get('description', '')}"
        )
        if finding.get("recommendation"):
            content += f"\nRecommendation: {finding['recommendation']}"

        memory.add_entry(
            scope=MemoryScope.USER,
            category=category,
            content=content,
            tags=["auto-review", finding.get("severity", "info"), finding.get("rule_id", "unknown")],
        )
        logger.info("Promoted finding to global memory: %s", finding.get("rule_id"))
        return True

    except ImportError:
        logger.debug("Memory module not available, skipping global memory promotion")
        return False
    except Exception as exc:
        logger.warning("Failed to promote to global memory: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 批量处理（Coda 阶段调用）
# ---------------------------------------------------------------------------

def promote_findings(
    findings: list[dict[str, Any]],
    cwd: str,
    max_promotions: int = 5,
) -> dict[str, list[str]]:
    """批量处理审查发现的沉淀。

    在 Coda 阶段调用。沉淀失败不影响主流程。

    参数:
        findings: 审查发现列表
        cwd: 当前工作目录
        max_promotions: 单次最多沉淀数（防止大量误沉淀）

    返回:
        {"skills": ["hardcoded-secret"], "global_memories": ["arch-service-layer"]}
    """
    if not findings:
        return {"skills": [], "global_memories": []}

    # 统计同一 rule_id 出现次数
    rule_counts = Counter(f.get("rule_id", "") for f in findings)

    promoted_skills: list[str] = []
    promoted_memories: list[str] = []
    promo_count = 0

    for finding in findings:
        if promo_count >= max_promotions:
            break

        action = is_important_finding(finding, rule_counts)
        if action is None:
            continue

        try:
            if action == "skill":
                if promote_to_skill(finding):
                    promoted_skills.append(finding.get("rule_id", "unknown"))
                    promo_count += 1
            elif action == "global_memory":
                if promote_to_global_memory(finding, cwd):
                    promoted_memories.append(finding.get("rule_id", "unknown"))
                    promo_count += 1
        except Exception as exc:
            logger.warning("Promotion failed for %s: %s", finding.get("rule_id"), exc)

    return {
        "skills": promoted_skills,
        "global_memories": promoted_memories,
    }
