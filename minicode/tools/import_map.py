"""Import map — 纯 Python 跨平台符号索引。

全量建表 + 增量更新，不依赖 grep/ripgrep 等外部命令。
审查子 Agent 通过查表而非 grep 全项目来找受影响文件。
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import time
from pathlib import Path

# ---- 常量 ----

SKIP_DIRS = frozenset({
    "__pycache__", ".git", "venv", ".venv", "env", ".env",
    "node_modules", "site-packages", ".mini-code-import-map",
    ".mini-code-tool-results", ".claude",
})
IMPORT_MAP_REL = ".mini-code-import-map/import-map.json"


# ---- 符号提取 ----

def _classify_symbol(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "constant"


def _extract_symbols(file_path: str, source: str) -> dict[str, dict]:
    """AST 解析源代码，提取所有顶层符号。"""
    symbols: dict[str, dict] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols

    for node in ast.iter_child_nodes(tree):
        name: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.ClassDef):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            if isinstance(target, ast.Name):
                name = target.id
            else:
                continue
        else:
            continue
        if name is None:
            continue
        symbols[name] = {
            "file": file_path,
            "type": _classify_symbol(node),
            "is_public": not name.startswith("_"),
            "referenced_by": [],
        }
    return symbols


# ---- 引用搜索 ----

def _walk_py_files(project_root: Path) -> list[Path]:
    results: list[Path] = []
    for py_file in project_root.rglob("*.py"):
        rel = py_file.relative_to(project_root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        results.append(py_file)
    return sorted(results)


def _find_references(symbol_name: str, project_root: Path, exclude_file: str) -> list[str]:
    """单词边界正则搜索引用。"""
    if symbol_name.startswith("_"):
        return []
    pattern = re.compile(r"\b" + re.escape(symbol_name) + r"\b")
    refs: list[str] = []
    for py_file in _walk_py_files(project_root):
        rel_path = str(py_file.relative_to(project_root)).replace("\\", "/")
        if rel_path == exclude_file:
            continue
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            if pattern.search(source):
                refs.append(rel_path)
        except (OSError, UnicodeDecodeError):
            continue
    return refs


# ---- 全量建表 ----

def build_import_map(project_root: str) -> dict:
    """全量扫描项目 -> 提取符号 -> 搜索引用 -> 写 JSON。"""
    root = Path(project_root).resolve()
    py_files = _walk_py_files(root)

    # 第一遍：AST 提取符号
    all_symbols: dict[str, dict] = {}
    for py_file in py_files:
        rel_path = str(py_file.relative_to(root)).replace("\\", "/")
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            all_symbols.update(_extract_symbols(rel_path, source))
        except (OSError, UnicodeDecodeError):
            continue

    # 第二遍：逐符号搜索引用（只搜公开符号）
    for sym_name, sym_data in all_symbols.items():
        if sym_data["is_public"]:
            sym_data["referenced_by"] = _find_references(sym_name, root, sym_data["file"])

    data = {"version": 1, "updated_at": time.time(), "symbols": all_symbols}
    _save_import_map(str(root), data)
    return data


# ---- 增量更新 ----

def update_import_map_for_file(project_root: str, file_path: str) -> None:
    """单文件增量更新：移除旧符号 -> 提取新符号 -> 更新引用 -> 写文件。"""
    root = Path(project_root).resolve()
    file_path = file_path.replace("\\", "/")

    data = _load_import_map(project_root)
    if data is None:
        data = {"version": 1, "updated_at": 0, "symbols": {}}
    symbols = data["symbols"]

    # 删除旧符号
    keys_to_delete = [k for k, v in symbols.items() if v.get("file") == file_path]
    for k in keys_to_delete:
        del symbols[k]

    # 从其他文件的 referenced_by 中移除此文件
    for sym_data in symbols.values():
        if file_path in sym_data.get("referenced_by", []):
            sym_data["referenced_by"].remove(file_path)

    # 提取新符号
    abs_path = root / file_path
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        new_symbols = _extract_symbols(file_path, source)
    except (OSError, UnicodeDecodeError):
        new_symbols = {}

    # 搜索新符号的引用
    for sym_name, sym_data in new_symbols.items():
        if sym_data["is_public"]:
            sym_data["referenced_by"] = _find_references(sym_name, root, file_path)

    # 检查当前文件是否引用了其他符号
    for sym_name, sym_data in symbols.items():
        if not sym_data["is_public"]:
            continue
        try:
            if re.search(r"\b" + re.escape(sym_name) + r"\b", source):
                if file_path not in sym_data["referenced_by"]:
                    sym_data["referenced_by"].append(file_path)
        except re.error:
            continue

    symbols.update(new_symbols)
    data["updated_at"] = time.time()
    _save_import_map(project_root, data)


# ---- I/O ----

def _get_import_map_path(project_root: str) -> Path:
    return Path(project_root).resolve() / IMPORT_MAP_REL


def _load_import_map(project_root: str) -> dict | None:
    path = _get_import_map_path(project_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_import_map(project_root: str, data: dict) -> None:
    path = _get_import_map_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(tmp_path).replace(path)
    except Exception:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---- 工具函数 ----

def get_affected_files(project_root: str, symbol_names: list[str]) -> dict[str, list[str]]:
    """查询一组符号的受影响文件列表（O(1) 查表）。"""
    data = _load_import_map(project_root)
    if data is None:
        return {}
    symbols = data.get("symbols", {})
    result: dict[str, list[str]] = {}
    for name in symbol_names:
        sym = symbols.get(name)
        if sym:
            result[name] = sym.get("referenced_by", [])
    return result
