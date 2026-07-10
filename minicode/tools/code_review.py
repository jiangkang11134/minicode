"""Python 代码审查工具。

基于 AST 分析对 Python 代码进行静态审查，检查未使用的导入、
硬编码常量、缺失的 docstring 和过长的函数。支持按检查类型
（imports / style / complexity / all）筛选。

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
# Code Review Checks
# ---------------------------------------------------------------------------

def _check_unused_imports(tree: ast.AST, content: str) -> list[dict[str, Any]]:
    """检查文件中是否存在未被使用的导入。

    遍历 AST 收集所有 import/from 语句，然后逐行扫描文件内容
    （跳过导入行自身），判断每个导入的名称是否在后续代码中出现。

    参数:
        tree: 文件的 AST 解析树。
        content: 文件的原始文本内容。

    返回:
        问题字典列表，每个字典包含 type、severity、message 和 line。
        未使用的导入标记为 "warning" 级别。

    重要程度: """
    issues = []

    # Get all imports
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = {"node": node, "type": "import"}
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = {"node": node, "type": "from"}

    # Check if each import is used
    for name, info in imports.items():
        # Simple check: search for name in code (excluding import lines)
        lines = content.split("\n")
        used = False
        for i, line in enumerate(lines):
            # Skip import lines
            if line.strip().startswith(("import ", "from ")):
                continue
            if name in line:
                used = True
                break

        if not used:
            issues.append({
                "type": "unused_import",
                "severity": "warning",
                "message": f"Import '{name}' is imported but never used",
                "line": getattr(info["node"], "lineno", 0),
            })

    return issues


def _check_hardcoded_values(tree: ast.AST) -> list[dict[str, Any]]:
    """检查文件中是否有应提取为常量的硬编码值。

    识别两类问题：
    1. 长度 >= 10 的字符串字面量（排除 docstring 位置）
    2. 魔法数字（排除 0, 1, -1 及其浮点等价形式）
    最多返回 10 条结果。

    参数:
        tree: 文件的 AST 解析树。

    返回:
        问题字典列表，每个字典包含 type、severity、message 和 line。
        硬编码值标记为 "info" 级别。

    重要程度: """
    issues = []

    for node in ast.walk(tree):
        # Check for string literals that look like hardcoded config
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Skip docstrings and short strings
            if len(node.value) < 10:
                continue
            # Skip if it's in a docstring position
            if hasattr(node, "parent") and isinstance(node.parent, ast.Expr):
                continue

        # Check for magic numbers
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            # Skip 0, 1, -1 which are commonly used
            if node.value in (0, 1, -1, 0.0, 1.0, -1.0):
                continue
            # Check if it's used multiple times (likely a constant)
            issues.append({
                "type": "magic_number",
                "severity": "info",
                "message": f"Consider extracting magic number '{node.value}' to a named constant",
                "line": getattr(node, "lineno", 0),
            })

    return issues[:10]  # Limit to 10 issues


def _check_empty_docstrings(tree: ast.AST) -> list[dict[str, Any]]:
    """检查文件中是否有缺少 docstring 的函数或类。

    遍历 AST 中所有 FunctionDef、AsyncFunctionDef 和 ClassDef，
    使用 ast.get_docstring 检查是否包含文档字符串。
    最多返回 10 条结果。

    参数:
        tree: 文件的 AST 解析树。

    返回:
        问题字典列表，每个字典包含 type、severity、message 和 line。
        缺少 docstring 标记为 "info" 级别。

    重要程度: """
    issues = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            docstring = ast.get_docstring(node)
            if not docstring:
                name = getattr(node, "name", "unknown")
                type_label = "Function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "Class"
                issues.append({
                    "type": "missing_docstring",
                    "severity": "info",
                    "message": f"{type_label} '{name}' has no docstring",
                    "line": getattr(node, "lineno", 0),
                })

    return issues[:10]  # Limit to 10 issues


def _check_long_functions(tree: ast.AST) -> list[dict[str, Any]]:
    """检查文件中是否存在过长的函数。

    遍历 AST 中所有 FunctionDef 和 AsyncFunctionDef，
    利用 end_lineno 和 lineno 计算函数行数，超过 50 行则标记。

    参数:
        tree: 文件的 AST 解析树。

    返回:
        问题字典列表，每个字典包含 type、severity、message 和 line。
        过长函数标记为 "warning" 级别。

    重要程度: """
    issues = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Calculate function length
            if hasattr(node, "end_lineno") and hasattr(node, "lineno"):
                length = node.end_lineno - node.lineno
                if length > 50:
                    issues.append({
                        "type": "long_function",
                        "severity": "warning",
                        "message": f"Function '{node.name}' is {length} lines long (consider splitting)",
                        "line": node.lineno,
                    })

    return issues


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

def _validate(input_data: dict) -> dict:
    """验证 code_review 工具的输入参数。

    检查 path 和可选的 checks 参数。checks 必须在
    ("all", "imports", "style", "complexity") 范围内。

    参数:
        input_data: 原始输入字典，包含 "path" 和可选的 "checks"。

    返回:
        清洗后的字典，含 path 和 checks。

    抛出:
        ValueError: 如果 checks 不在允许的枚举值内。

    重要程度: """
    path = input_data.get("path", ".")
    checks = input_data.get("checks", "all")
    if checks not in ("all", "imports", "style", "complexity"):
        raise ValueError("checks must be one of: all, imports, style, complexity")
    return {"path": path, "checks": checks}


def _run(input_data: dict, context) -> ToolResult:
    """执行 Python 代码质量审查。

    递归搜索路径下所有 .py 文件（跳过 .git、__pycache__ 等目录），
    根据 checks 参数选择运行以下检查的组合：
    - imports: 检查未使用的导入
    - style: 检查硬编码值和缺失的 docstring
    - complexity: 检查过长的函数
    所有问题按 severity（error > warning > info）排序后格式化输出。

    参数:
        input_data: 已验证的输入参数，含 "path" 和 "checks"。
        context: 工具执行上下文，用于解析路径。

    返回:
        ToolResult: 审查结果以格式化文本输出。
                    如果有 error 级别问题则 ok=False，否则 ok=True。

    重要程度: """
    try:
        target = resolve_tool_path(context, input_data["path"], "review")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    checks_type = input_data["checks"]

    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {target}")

    # Find Python files
    py_files = []
    if target.is_file():
        py_files = [target]
    else:
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "env", ".tox", "node_modules")]
            for f in files:
                if f.endswith(".py"):
                    py_files.append(Path(root) / f)

    all_issues = []
    files_reviewed = 0

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        files_reviewed += 1

        # Run checks based on type
        if checks_type in ("all", "imports"):
            all_issues.extend([
                {**issue, "file": str(py_file.relative_to(context.cwd))}
                for issue in _check_unused_imports(tree, content)
            ])

        if checks_type in ("all", "style"):
            all_issues.extend([
                {**issue, "file": str(py_file.relative_to(context.cwd))}
                for issue in _check_hardcoded_values(tree)
            ])
            all_issues.extend([
                {**issue, "file": str(py_file.relative_to(context.cwd))}
                for issue in _check_empty_docstrings(tree)
            ])

        if checks_type in ("all", "complexity"):
            all_issues.extend([
                {**issue, "file": str(py_file.relative_to(context.cwd))}
                for issue in _check_long_functions(tree)
            ])

    # Sort by severity
    severity_order = {"error": 0, "warning": 1, "info": 2}
    all_issues.sort(key=lambda x: severity_order.get(x["severity"], 3))

    # Format output
    lines = ["Code Review Result", "=" * 60, ""]

    lines.append(f"Files reviewed: {files_reviewed}")
    lines.append(f"Issues found: {len(all_issues)}")
    lines.append("")

    # Group by severity
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    infos = [i for i in all_issues if i["severity"] == "info"]

    if errors:
        lines.append(f"❌ Errors ({len(errors)}):")
        for issue in errors[:10]:
            lines.append(f"  L{issue.get('line', '?')} {issue['file']}")
            lines.append(f"     {issue['message']}")
        lines.append("")

    if warnings:
        lines.append(f"⚠️  Warnings ({len(warnings)}):")
        for issue in warnings[:10]:
            lines.append(f"  L{issue.get('line', '?')} {issue['file']}")
            lines.append(f"     {issue['message']}")
        lines.append("")

    if infos:
        lines.append(f"ℹ️  Info ({len(infos)}):")
        for issue in infos[:10]:
            lines.append(f"  L{issue.get('line', '?')} {issue['file']}")
            lines.append(f"     {issue['message']}")
        lines.append("")

    if not all_issues:
        lines.append("✓ No issues found! Code looks clean.")

    return ToolResult(
        ok=len(errors) == 0,
        output="\n".join(lines),
    )


code_review_tool = ToolDefinition(
    name="code_review",
    description="Review Python code quality by checking for unused imports, hardcoded values, missing docstrings, and long functions. Use this after making changes to ensure code quality.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or directory path to review (default: current directory)"},
            "checks": {"type": "string", "enum": ["all", "imports", "style", "complexity"], "description": "Types of checks to run (default: all)"},
        },
    },
    validator=_validate,
    run=_run,
)

# ── 审查系统 re-export ──
from minicode.review.hooks import _pre_review_content  # noqa: F401
