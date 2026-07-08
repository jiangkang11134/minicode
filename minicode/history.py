"""SmartCode 会话历史记录管理。

提供历史记录的加载与保存功能，使用 JSON 文件持久化存储。
内置 TTL 缓存（5 秒）以减少重复文件读取开销。
"""
from __future__ import annotations

import json
import time

from minicode.config import MINI_CODE_DIR, MINI_CODE_HISTORY_PATH


# Simple TTL cache: stores (timestamp, value)
_history_cache: tuple[float, list[str]] | None = None
_history_cache_ttl: float = 5.0  # seconds


def load_history_entries() -> list[str]:
    """从持久化存储加载历史记录条目。

    使用 TTL 缓存（5 秒）：若在缓存有效期内直接返回缓存的副本，
    否则从 JSON 文件读取并更新缓存。
    文件不存在或 JSON 解析失败时返回空列表。

    返回:
        历史记录字符串列表，按文件中存储的顺序
    """
    global _history_cache
    now = time.time()
    if _history_cache is not None:
        cached_at, cached_entries = _history_cache
        if now - cached_at < _history_cache_ttl:
            return cached_entries.copy()

    if not MINI_CODE_HISTORY_PATH.exists():
        _history_cache = (now, [])
        return []
    try:
        parsed = json.loads(MINI_CODE_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _history_cache = (now, [])
        return []
    entries = parsed.get("entries", [])
    result = [str(entry) for entry in entries] if isinstance(entries, list) else []
    _history_cache = (now, result)
    return result.copy()


def save_history_entries(entries: list[str]) -> None:
    """将历史记录条目持久化存储到 JSON 文件。

    仅保留最近 200 条记录，存入 JSON 文件的 "entries" 字段。
    写入后立即更新 TTL 缓存。

    参数:
        entries: 要保存的历史记录字符串列表（仅保留前 200 条）
    """
    global _history_cache
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    MINI_CODE_HISTORY_PATH.write_text(
        json.dumps({"entries": entries[-200:]}, indent=2) + "\n",
        encoding="utf-8",
    )
    _history_cache = (time.time(), entries[-200:].copy())

