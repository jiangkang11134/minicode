"""严格审查触发判断。

判断条件（优先级从高到低）：
  1. 安全文件路径（auth/ security/ payment/ api/ …）
  2. diff 特征（>200 行、跨 >10 文件、public API 变更）
  3. 新文件（首次创建的代码文件，此前未经审查）
  4. 历史问题率（同文件 90 天内 >60%）
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Any

# 安全文件路径 — 匹配即触发严格
# 使用 PurePosixPath 的 parts 检测目录包含关系
STRICT_PATHS = frozenset({
    "auth",
    "login",
    "security",
    "encrypt",
    "crypto",
    "payment",
    "billing",
    "api",
    "middleware",
    "migration",
    "schema",
    "token",
    "session",
    "oauth",
})


def _match_strict_path(file_path: str) -> bool:
    """检查文件路径是否匹配安全文件目录模式。

    检查文件路径中是否包含 STRICT_PATHS 中定义的目录名或文件名前缀。
    """
    p = PurePosixPath(file_path.replace("\\", "/"))
    # 检查目录层级中是否包含敏感目录名
    for part in p.parts:
        if part in STRICT_PATHS:
            return True
    # 检查文件名是否以敏感前缀开头
    name = p.name.lower()
    for prefix in ("login", "encrypt", "crypto", "token", "session", "oauth", "dockerfile", "docker-compose"):
        if name.startswith(prefix):
            return True
    return False


def _within_days(timestamp: float, days: int) -> bool:
    return time.time() - timestamp < days * 86400


def should_trigger_strict(
    file_path: str,
    diff_stat: dict[str, Any] | None = None,
    review_store=None,
    cwd: str | None = None,
    is_new_file: bool = False,
) -> tuple[bool, str]:
    """判断是否应该触发严格审查（启动审查子 Agent）。

    参数:
        file_path: 变更的文件路径
        diff_stat: git diff 统计（changed_lines, touched_files, public_api_changed）
        review_store: ReviewMemoryStore 实例（用于查历史）
        cwd: 项目根目录，用于判断文件是否存在
        is_new_file: 该文件是否为新创建的（此前不存在）

    返回:
        (True, "原因") 或 (False, "")
    """
    # 1. 安全文件路径
    if _match_strict_path(file_path):
        return True, f"安全路径: {file_path}"

    # 2. diff 特征
    if diff_stat:
        if diff_stat.get("changed_lines", 0) > 200:
            return True, f"大规模变更 ({diff_stat['changed_lines']} 行)"
        if diff_stat.get("touched_files", 0) > 10:
            return True, f"跨文件变更 ({diff_stat['touched_files']} 个文件)"
        if diff_stat.get("public_api_changed"):
            return True, "public API 签名变更"

    # 3. 新文件 — 首次创建的代码文件，此前未经审查，风险更高
    if is_new_file:
        return True, "新文件（首次创建，需审查）"

    # 4. 历史问题率
    if review_store:
        recent = [
            f for f in review_store.find_by_file(file_path)
            if _within_days(getattr(f, "created_at", 0), 90)
        ]
        if len(recent) >= 3:
            open_count = sum(
                1 for f in recent if getattr(f, "status", "") in ("open", "acknowledged")
            )
            rate = open_count / len(recent)
            if rate >= 0.6:
                return True, f"历史问题率 {rate:.0%}（{open_count}/{len(recent)}）"

    return False, ""
