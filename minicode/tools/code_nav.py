"""Python 代码导航工具集。

提供基于 AST 的符号分析功能，包括查找符号（类/函数/变量）、
查找符号引用以及获取文件的 AST 统计信息。
帮助理解代码结构和在修改前评估影响范围。

依赖:
    ast: Python 标准库抽象语法树
    minicode.tooling: ToolDefinition、ToolResult
    minicode.workspace: resolve_tool_path
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


# ---------------------------------------------------------------------------
# AST Analysis Helpers
# ---------------------------------------------------------------------------

def _get_symbol_type(node: ast.AST) -> str | None:
    """从 AST 节点获取符号类型。

    识别 ClassDef 返回 "class"，FunctionDef/AsyncFunctionDef 返回 "function"，
    Assign/AnnAssign 返回 "variable"，其他返回 None。

    参数:
        node: AST 节点对象。

    返回:
        符号类型字符串 ("class" / "function" / "variable")，无法识别时返回 None。

    重要程度: """
    if isinstance(node, ast.ClassDef):
        return "class"
    elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
        return "function"
    elif isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
        return "variable"
    return None


def _extract_symbols_from_file(file_path: Path) -> list[dict[str, Any]]:
    """从 Python 文件中提取所有符号信息（AST 方式）。

    解析文件为 AST，遍历所有节点提取类、函数和变量的定义信息，
    包括名称、行号、docstring 预览、函数参数、装饰器和类基类。

    参数:
        file_path: Python 文件的 Path 对象。

    返回:
        符号字典列表，每个字典包含 type、name、line、docstring、
        args、decorators、bases 等字段。文件不存在或解析失败返回空列表。

    重要程度: """
    if not file_path.exists():
        return []

    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    symbols = []

    for node in ast.walk(tree):
        symbol_type = _get_symbol_type(node)
        if symbol_type is None:
            continue

        name = getattr(node, "name", None)
        if not name:
            continue

        # Get line number
        lineno = getattr(node, "lineno", 0)

        # Get docstring if exists
        docstring = ast.get_docstring(node)
        docstring_preview = docstring[:100] if docstring else ""

        # Get arguments for functions
        args = []
        if symbol_type == "function" and hasattr(node, "args"):
            for arg in node.args.args:
                arg_name = arg.arg
                arg_type = ""
                if arg.annotation:
                    try:
                        arg_type = ast.unparse(arg.annotation)
                    except Exception:
                        arg_type = "?"
                args.append(f"{arg_name}: {arg_type}" if arg_type else arg_name)

        # Get decorators
        decorators = []
        for dec in getattr(node, "decorator_list", []):
            try:
                decorators.append(ast.unparse(dec))
            except Exception:
                decorators.append("?")

        # Get class bases
        bases = []
        if symbol_type == "class" and hasattr(node, "bases"):
            for base in node.bases:
                try:
                    bases.append(ast.unparse(base))
                except Exception:
                    bases.append("?")

        symbols.append({
            "type": symbol_type,
            "name": name,
            "line": lineno,
            "docstring": docstring_preview,
            "args": args if symbol_type == "function" else [],
            "decorators": decorators,
            "bases": bases if symbol_type == "class" else [],
        })

    return symbols


def _find_symbol_references(file_path: Path, symbol_name: str) -> list[dict[str, Any]]:
    """在文件中查找指定符号的所有引用位置（基于文本搜索）。

    逐行扫描文件内容，忽略以 # 开头的注释行，查找包含符号名称的行。
    返回匹配位置及附近上下文（前后共 5 行）。

    参数:
        file_path: Python 文件的 Path 对象。
        symbol_name: 要查找的符号名称。

    返回:
        引用字典列表，每个字典包含 file、line、code（截断至 100 字符）、
        context（上下文字段）。文件不存在或读取失败返回空列表。

    重要程度: """
    if not file_path.exists():
        return []

    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.split("\n")
    except (OSError, UnicodeDecodeError):
        return []

    references = []
    for i, line in enumerate(lines, 1):
        # Skip comments and strings (simple check)
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        if symbol_name in line:
            # Get context (surrounding lines)
            start = max(0, i - 3)
            end = min(len(lines), i + 2)
            context = "\n".join(lines[start:end])

            references.append({
                "file": str(file_path),
                "line": i,
                "code": line.strip()[:100],
                "context": context,
            })

    return references


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def _validate_find_symbols(input_data: dict) -> dict:
    """验证 find_symbols 工具的输入参数。

    检查 path 和可选的 symbol_type 参数。symbol_type 必须在
    ("all", "class", "function", "variable") 范围内。

    参数:
        input_data: 原始输入字典，包含 "path" 和可选的 "symbol_type"。

    返回:
        清洗后的字典，含 path 和 symbol_type。

    抛出:
        ValueError: 如果 symbol_type 不在允许的枚举值内。

    重要程度: """
    path = input_data.get("path", ".")
    symbol_type = input_data.get("symbol_type", "all")
    if symbol_type not in ("all", "class", "function", "variable"):
        raise ValueError("symbol_type must be one of: all, class, function, variable")
    return {"path": path, "symbol_type": symbol_type}


def _run_find_symbols(input_data: dict, context) -> ToolResult:
    """在指定路径下查找所有 Python 符号并格式化输出。

    递归搜索路径下所有 .py 文件（跳过 .git、__pycache__ 等目录），
    提取每个文件的符号信息，按类型过滤后按文件分组输出。
    输出包含符号名称、类型、参数/基类和 docstring 预览。

    参数:
        input_data: 已验证的输入参数，含 "path" 和 "symbol_type"。
        context: 工具执行上下文，用于解析路径。

    返回:
        ToolResult: 查找结果以格式化文本形式输出。
                    未找到符号时也返回 ok=True。

    重要程度: """
    try:
        search_path = resolve_tool_path(context, input_data["path"], "analyze")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    symbol_type = input_data["symbol_type"]

    if not search_path.exists():
        return ToolResult(ok=False, output=f"Path not found: {search_path}")

    # Find all Python files
    py_files = []
    if search_path.is_file():
        py_files = [search_path]
    else:
        for root, dirs, files in os.walk(search_path):
            # Skip common non-source dirs
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "env", ".tox", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(Path(root) / f)

    all_symbols = []
    for py_file in py_files:
        symbols = _extract_symbols_from_file(py_file)
        for sym in symbols:
            sym["file"] = str(py_file.relative_to(context.cwd))
            all_symbols.append(sym)

    # Filter by type
    if symbol_type != "all":
        all_symbols = [s for s in all_symbols if s["type"] == symbol_type]

    if not all_symbols:
        return ToolResult(
            ok=True,
            output=f"No symbols found in {input_data['path']}",
        )

    # Format output
    lines = [f"Found {len(all_symbols)} symbol(s) in {input_data['path']}:", ""]

    by_file: dict[str, list] = {}
    for sym in all_symbols:
        by_file.setdefault(sym["file"], []).append(sym)

    for file, symbols in by_file.items():
        lines.append(f"\U0001f4c4 {file}")
        for sym in symbols:
            icon = {"class": "\U0001f3db️", "function": "⚙️", "variable": "\U0001f4e6"}.get(sym["type"], "❓")
            type_label = sym["type"][:3].upper()

            extra = ""
            if sym["type"] == "function" and sym["args"]:
                extra = f"({', '.join(sym['args'])})"
            elif sym["type"] == "class" and sym["bases"]:
                extra = f"({', '.join(sym['bases'])})"

            lines.append(f"  {icon} [{type_label}] {sym['name']}{extra} (line {sym['line']})")
            if sym["docstring"]:
                lines.append(f"      \U0001f4ac {sym['docstring']}")
        lines.append("")

    return ToolResult(ok=True, output="\n".join(lines))


def _validate_find_references(input_data: dict) -> dict:
    """验证 find_references 工具的输入参数。

    检查 symbol_name 是否为非空字符串，path 为可选参数默认 "."。

    参数:
        input_data: 原始输入字典，包含 "symbol_name" 和可选的 "path"。

    返回:
        清洗后的字典，含 symbol_name 和 path。

    抛出:
        ValueError: 如果 symbol_name 缺失或为空。

    重要程度: """
    symbol_name = input_data.get("symbol_name")
    if not isinstance(symbol_name, str) or not symbol_name.strip():
        raise ValueError("symbol_name is required")
    path = input_data.get("path", ".")
    return {"symbol_name": symbol_name.strip(), "path": path}


def _run_find_references(input_data: dict, context) -> ToolResult:
    """在指定路径下查找指定符号的所有引用位置。

    递归搜索所有 Python 文件，对每个文件进行逐行文本匹配，
    找到包含符号名称的代码行及其上下文。按文件分组输出，
    每个文件最多显示 20 条引用，超出时显示省略提示。

    参数:
        input_data: 已验证的输入参数，含 "symbol_name" 和 "path"。
        context: 工具执行上下文，用于解析路径。

    返回:
        ToolResult: 引用结果以格式化文本形式输出。
                    未找到引用时也返回 ok=True。

    重要程度: """
    symbol_name = input_data["symbol_name"]
    try:
        search_path = resolve_tool_path(context, input_data["path"], "analyze")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))

    if not search_path.exists():
        return ToolResult(ok=False, output=f"Path not found: {search_path}")

    # Find all Python files
    py_files = []
    if search_path.is_file():
        py_files = [search_path]
    else:
        for root, dirs, files in os.walk(search_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "env", ".tox", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(Path(root) / f)

    all_refs = []
    for py_file in py_files:
        refs = _find_symbol_references(py_file, symbol_name)
        all_refs.extend(refs)

    if not all_refs:
        return ToolResult(
            ok=True,
            output=f"No references found for '{symbol_name}' in {input_data['path']}",
        )

    # Format output
    lines = [f"Found {len(all_refs)} reference(s) for '{symbol_name}':", ""]

    by_file: dict[str, list] = {}
    for ref in all_refs:
        rel_path = Path(ref["file"]).relative_to(context.cwd)
        by_file.setdefault(str(rel_path), []).append(ref)

    for file, refs in by_file.items():
        lines.append(f"\U0001f4c4 {file} ({len(refs)} refs)")
        for ref in refs[:20]:  # Limit to 20 per file
            lines.append(f"  L{ref['line']}: {ref['code']}")
        if len(refs) > 20:
            lines.append(f"  ... and {len(refs) - 20} more")
        lines.append("")

    return ToolResult(ok=True, output="\n".join(lines))


def _validate_get_ast_info(input_data: dict) -> dict:
    """验证 get_ast_info 工具的输入参数。

    检查 file_path 是否为非空字符串。

    参数:
        input_data: 原始输入字典，包含 "file_path" 键。

    返回:
        清洗后的字典，含 file_path。

    抛出:
        ValueError: 如果 file_path 缺失或为空。

    重要程度: """
    file_path = input_data.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("file_path is required")
    return {"file_path": file_path}


def _run_get_ast_info(input_data: dict, context) -> ToolResult:
    """获取指定 Python 文件的 AST 结构信息。

    解析目标 Python 文件为 AST，统计类、函数和导入的数量，
    列出所有导入语句（最多 20 条）。

    参数:
        input_data: 已验证的输入参数，含 "file_path"。
        context: 工具执行上下文，用于解析路径。

    返回:
        ToolResult: 包含文件行数、类数、函数数、导入数及导入列表的统计信息。
                    解析错误时返回 ok=False。

    重要程度: """
    try:
        target = resolve_tool_path(context, input_data["file_path"], "analyze")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))

    if not target.exists():
        return ToolResult(ok=False, output=f"File not found: {target}")

    try:
        content = target.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(target))
    except SyntaxError as e:
        return ToolResult(ok=False, output=f"Syntax error: {e}")
    except UnicodeDecodeError as e:
        return ToolResult(ok=False, output=f"Encoding error: {e}")

    # Count statistics
    classes = sum(1 for _ in ast.walk(tree) if isinstance(_, ast.ClassDef))
    functions = sum(1 for _ in ast.walk(tree) if isinstance(_, (ast.FunctionDef, ast.AsyncFunctionDef)))
    imports = sum(1 for _ in ast.walk(tree) if isinstance(_, (ast.Import, ast.ImportFrom)))

    # Get imports
    import_list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_list.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                import_list.append(f"from {module} import {alias.name}")

    # Format output
    lines = [
        f"AST Info for {input_data['file_path']}",
        "=" * 50,
        "",
        f"Lines: {len(content.splitlines())}",
        f"Classes: {classes}",
        f"Functions: {functions}",
        f"Imports: {imports}",
        "",
        "Imports:",
    ]

    for imp in import_list[:20]:
        lines.append(f"  {imp}")

    if len(import_list) > 20:
        lines.append(f"  ... and {len(import_list) - 20} more")

    return ToolResult(ok=True, output="\n".join(lines))


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

find_symbols_tool = ToolDefinition(
    name="find_symbols",
    description="Find all Python symbols (classes, functions, variables) in files or directories. Use this to understand code structure before making changes.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path to search (default: current directory)"},
            "symbol_type": {"type": "string", "enum": ["all", "class", "function", "variable"], "description": "Filter by symbol type (default: all)"},
        },
    },
    validator=_validate_find_symbols,
    run=_run_find_symbols,
)

find_references_tool = ToolDefinition(
    name="find_references",
    description="Find all references to a Python symbol (class, function, variable) across files. Use this before renaming to see impact.",
    input_schema={
        "type": "object",
        "properties": {
            "symbol_name": {"type": "string", "description": "Name of the symbol to find references for"},
            "path": {"type": "string", "description": "File or directory path to search (default: current directory)"},
        },
        "required": ["symbol_name"],
    },
    validator=_validate_find_references,
    run=_run_find_references,
)

get_ast_info_tool = ToolDefinition(
    name="get_ast_info",
    description="Get AST information for a Python file including structure, imports, and statistics. Use this to understand file organization.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to Python file"},
        },
        "required": ["file_path"],
    },
    validator=_validate_get_ast_info,
    run=_run_get_ast_info,
)
