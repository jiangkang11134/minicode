"""审查发现持久化存储。

存储路径：.mini-code-import-map/review-findings.json（和 import map 同目录）
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from minicode.review.config import IMPORT_MAP_DIR, REVIEW_FINDINGS_FILE

logger = logging.getLogger("minicode.review.memory")


@dataclass
class ReviewFinding:
    """单条审查发现。"""

    id: str = ""
    severity: str = "info"       # critical | major | minor | suggestion | false_positive
    file_path: str = ""
    line: int = 0
    rule_id: str = ""
    title: str = ""
    description: str = ""
    recommendation: str = ""
    status: str = "open"         # open | acknowledged | fixed | wontfix | false_positive
    created_at: float = 0.0
    bad_example: str = ""
    good_example: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()


class ReviewMemoryStore:
    """审查发现持久化存储。

    用法：
        store = ReviewMemoryStore(cwd)
        store.add_finding(ReviewFinding(severity="critical", file_path="auth.py", ...))
        store.save()
    """

    def __init__(self, cwd: str | None = None):
        self._cwd = Path(cwd or os.getcwd())
        self._findings: dict[str, ReviewFinding] = {}
        self._load()

    def _get_path(self) -> Path:
        return self._cwd / IMPORT_MAP_DIR / REVIEW_FINDINGS_FILE

    def _load(self) -> None:
        path = self._get_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("findings", []):
                f = ReviewFinding(**item)
                self._findings[f.id] = f
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load review findings: %s", exc)

    def save(self) -> None:
        path = self._get_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": time.time(),
            "total": len(self._findings),
            "findings": [asdict(f) for f in self._findings.values()],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- CRUD ----

    def add_finding(self, finding: ReviewFinding) -> None:
        self._findings[finding.id] = finding

    def update_status(self, finding_id: str, status: str, note: str = "") -> bool:
        if finding_id not in self._findings:
            return False
        f = self._findings[finding_id]
        f.status = status
        if note:
            f.description = note
        return True

    # ---- 查询 ----

    def find_by_file(self, file_path: str) -> list[ReviewFinding]:
        return [f for f in self._findings.values() if f.file_path == file_path]

    def find_by_rule(self, rule_id: str) -> list[ReviewFinding]:
        return [f for f in self._findings.values() if f.rule_id == rule_id]

    def find_open(self) -> list[ReviewFinding]:
        return [f for f in self._findings.values() if f.status == "open"]

    def get_stats(self) -> dict[str, Any]:
        all_f = list(self._findings.values())
        total = len(all_f)
        by_severity: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_file: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        for f in all_f:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_status[f.status] = by_status.get(f.status, 0) + 1
            by_file[f.file_path] = by_file.get(f.file_path, 0) + 1
            by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
        fixed = by_status.get("fixed", 0)
        return {
            "total": total,
            "by_severity": by_severity,
            "by_status": by_status,
            "by_file": dict(sorted(by_file.items(), key=lambda x: x[1], reverse=True)[:10]),
            "by_rule": dict(sorted(by_rule.items(), key=lambda x: x[1], reverse=True)[:10]),
            "fix_rate": fixed / total if total > 0 else 0,
        }
