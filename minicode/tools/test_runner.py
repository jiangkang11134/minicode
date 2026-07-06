"""test_runner 工具实现——自动发现并运行 Python 测试。

支持 pytest 和 unittest 两种测试框架，提供自动框架检测、
测试文件发现、结果解析和格式化输出等功能。
可选择启用覆盖率报告和详细输出模式。
"""
from __future__ import annotations

import subprocess
import sys
import re
import os
from pathlib import Path
from typing import Any
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


# ---------------------------------------------------------------------------
# Test Discovery
# ---------------------------------------------------------------------------

def _discover_test_files(path: Path, pattern: str = "test_*.py") -> list[Path]:
    """发现匹配模式的测试文件。

    支持文件和目录两种输入路径。如果是文件，检查文件名是否以 test_
    开头或 _test.py 结尾；如果是目录，递归搜索其中所有以 test_ 开头
    且以 .py 结尾的文件，自动跳过常见非测试目录（如 .git、__pycache__ 等）。

    参数:
        path: 要搜索的文件或目录路径。
        pattern: 文件名匹配模式，默认为 test_*.py。

    返回:
        排序后的测试文件路径列表。

    重要程度: """
    test_files = []

    if path.is_file():
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            test_files.append(path)
    elif path.is_dir():
        for root, dirs, files in os.walk(path):
            # Skip common non-test dirs
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", "env", ".tox", "node_modules")]

            for f in files:
                if f.startswith("test_") and f.endswith(".py"):
                    test_files.append(Path(root) / f)

    return sorted(test_files)


# ---------------------------------------------------------------------------
# Test Output Parsers
# ---------------------------------------------------------------------------

def _parse_pytest_output(output: str) -> dict[str, Any]:
    """解析 pytest 的输出为结构化格式。

    使用正则表达式从 pytest 的文本输出中提取通过数、失败数、错误数、
    跳过数、警告数和覆盖率等统计信息。同时解析每个测试用例的状态
    （PASSED/FAILED/ERROR/SKIPPED 等）以及失败详情。

    参数:
        output: pytest 的原始文本输出。

    返回:
        包含 passed、failed、errors、skipped、warnings 等统计键的字典，
        以及 tests（测试用例列表）、failure_details（失败详情）和
        coverage（覆盖率百分比，可能为 None）键。

    重要程度: """
    results = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "warnings": 0,
        "tests": [],
        "failures": [],
        "coverage": None,
    }

    # Parse summary line
    summary_match = re.search(r'(\d+) passed', output)
    if summary_match:
        results["passed"] = int(summary_match.group(1))

    failed_match = re.search(r'(\d+) failed', output)
    if failed_match:
        results["failed"] = int(failed_match.group(1))

    error_match = re.search(r'(\d+) error', output)
    if error_match:
        results["errors"] = int(error_match.group(1))

    skipped_match = re.search(r'(\d+) skipped', output)
    if skipped_match:
        results["skipped"] = int(skipped_match.group(1))

    warning_match = re.search(r'(\d+) warning', output)
    if warning_match:
        results["warnings"] = int(warning_match.group(1))

    # Parse individual test results
    test_pattern = re.compile(r'(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(.+?)(?:::(\w+))?', re.MULTILINE)
    for match in test_pattern.finditer(output):
        status = match.group(1)
        file_path = match.group(2)
        test_name = match.group(3)

        results["tests"].append({
            "file": file_path.strip(),
            "name": test_name or "unknown",
            "status": status.lower(),
        })

    # Parse failure details
    failure_pattern = re.compile(r'FAILURES\s*\n(.*?)(?=\n={50,}|\Z)', re.DOTALL)
    failure_match = failure_pattern.search(output)
    if failure_match:
        results["failure_details"] = failure_match.group(1)[:2000]

    # Parse coverage if present
    coverage_pattern = re.compile(r'TOTAL\s+\d+\s+\d+\s+(\d+)%')
    coverage_match = coverage_pattern.search(output)
    if coverage_match:
        results["coverage"] = int(coverage_match.group(1))

    return results


def _parse_unittest_output(output: str) -> dict[str, Any]:
    """解析 unittest 的输出为结构化格式。

    从 unittest 的文本输出中提取运行总数、通过数、失败数和错误数。
    根据输出中是否包含 "OK" 判断是否全部通过，否则解析 failures 和
    errors 的具体数值。

    参数:
        output: unittest 的原始文本输出。

    返回:
        包含 passed、failed、errors、skipped、tests 和 failures 键的字典。

    重要程度: """
    results = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "tests": [],
        "failures": [],
    }

    # Parse summary
    summary_match = re.search(r'Ran (\d+) test', output)
    if summary_match:
        total = int(summary_match.group(1))
        if "OK" in output:
            results["passed"] = total
        else:
            failed_match = re.search(r'failures=(\d+)', output)
            error_match = re.search(r'errors=(\d+)', output)

            results["failed"] = int(failed_match.group(1)) if failed_match else 0
            results["errors"] = int(error_match.group(1)) if error_match else 0
            results["passed"] = total - results["failed"] - results["errors"]

    return results


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

def _validate(input_data: dict) -> dict:
    """验证 test_runner 工具的输入数据。

    检查 path、framework、verbose、coverage、pattern 和 timeout 等
    参数的类型和取值范围是否符合要求。

    参数:
        input_data: 包含 path、framework、verbose、coverage、pattern
                    和 timeout 的字典。

    返回:
        标准化后的字典，包含所有已验证的参数字段。

    抛出:
        ValueError: 当参数类型或值无效时。

    重要程度: """
    path = input_data.get("path", ".")
    framework = input_data.get("framework", "auto")
    if framework not in ("auto", "pytest", "unittest"):
        raise ValueError("framework must be one of: auto, pytest, unittest")

    verbose = input_data.get("verbose", False)
    if not isinstance(verbose, bool):
        raise ValueError("verbose must be a boolean")

    coverage = input_data.get("coverage", False)
    if not isinstance(coverage, bool):
        raise ValueError("coverage must be a boolean")

    pattern = input_data.get("pattern")
    timeout = int(input_data.get("timeout", 60))
    if timeout < 10 or timeout > 300:
        raise ValueError("timeout must be between 10 and 300 seconds")

    return {
        "path": path,
        "framework": framework,
        "verbose": verbose,
        "coverage": coverage,
        "pattern": pattern,
        "timeout": timeout,
    }


def _run(input_data: dict, context) -> ToolResult:
    """运行测试并返回结构化结果。

    执行完整的测试流程：解析并验证目标路径、自动发现测试文件、
    按名称模式过滤、自动检测或指定测试框架、构建命令行参数并执行、
    解析输出结果，最后格式化输出包含通过/失败统计、失败详情、
    覆盖率信息（如启用）和详细测试列表（如启用 verbose 模式）。

    参数:
        input_data: 包含 path、framework、verbose、coverage、pattern
                    和 timeout 的字典。
        context: 工具运行时上下文，用于路径解析和工作目录设置。

    返回:
        ToolResult 对象。成功时 output 包含格式化的测试结果报告；
        失败时 ok 为 False，output 包含错误描述。

    重要程度: """
    try:
        target = resolve_tool_path(context, input_data["path"], "test")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    framework = input_data["framework"]
    verbose = input_data["verbose"]
    coverage = input_data["coverage"]
    pattern = input_data.get("pattern")
    timeout = input_data["timeout"]

    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {target}")

    # Discover test files
    test_files = _discover_test_files(target)

    if not test_files:
        return ToolResult(
            ok=False,
            output=f"No test files found in {input_data['path']}\n\n"
                   f"Expected files matching: test_*.py or *_test.py",
        )

    # Apply pattern filter if provided
    if pattern:
        test_files = [f for f in test_files if pattern in f.name]
        if not test_files:
            return ToolResult(
                ok=False,
                output=f"No test files match pattern: {pattern}",
            )

    # Determine framework
    if framework == "auto":
        # Check if pytest is available
        try:
            subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                capture_output=True,
                timeout=5,
            )
            framework = "pytest"
        except Exception:
            framework = "unittest"

    # Build command
    if framework == "pytest":
        cmd = [sys.executable, "-m", "pytest"]

        # Add test files
        cmd.extend([str(f) for f in test_files[:10]])  # Limit to 10 files

        if verbose:
            cmd.append("-v")

        if coverage:
            cmd.extend(["--cov", str(target)])

        # Add pattern
        if pattern:
            cmd.extend(["-k", pattern])

    else:  # unittest
        cmd = [sys.executable, "-m", "unittest", "discover"]
        cmd.extend(["-s", str(target)])

        if pattern:
            cmd.extend(["-p", f"*{pattern}*"])
        else:
            cmd.extend(["-p", "test_*.py"])

        if verbose:
            cmd.append("-v")

    # Run tests
    lines = [
        "🧪 Test Runner",
        "=" * 60,
        "",
        f"Framework: {framework}",
        f"Test files: {len(test_files)}",
        f"Pattern: {pattern or 'all'}",
        f"Coverage: {'enabled' if coverage else 'disabled'}",
        "",
        "-" * 60,
        "",
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(context.cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout + "\n" + result.stderr
        success = result.returncode == 0

        # Parse results
        if framework == "pytest":
            parsed = _parse_pytest_output(output)
        else:
            parsed = _parse_unittest_output(output)

        # Format results
        lines.append("📊 Results:")
        lines.append(f"  ✓ Passed:  {parsed.get('passed', 0)}")
        lines.append(f"  ✗ Failed:  {parsed.get('failed', 0)}")
        lines.append(f"  ⚠ Errors:  {parsed.get('errors', 0)}")
        lines.append(f"  ⊘ Skipped: {parsed.get('skipped', 0)}")

        if parsed.get("coverage"):
            lines.append(f"  📈 Coverage: {parsed['coverage']}%")

        lines.append("")

        # Show failures
        if parsed.get("failed", 0) > 0 or parsed.get("errors", 0) > 0:
            lines.append("❌ Failures:")

            if parsed.get("failure_details"):
                lines.append(parsed["failure_details"][:2000])
            else:
                # Extract from output
                failure_pattern = re.compile(r'FAILURES\s*\n(.*?)(?=\n={50,}|\Z)', re.DOTALL)
                failure_match = failure_pattern.search(output)
                if failure_match:
                    lines.append(failure_match.group(1)[:2000])

            lines.append("")

        # Show warnings
        if parsed.get("warnings", 0) > 0:
            lines.append(f"⚠️  {parsed['warnings']} warning(s)")
            lines.append("")

        # Show test list if verbose
        if verbose and parsed.get("tests"):
            lines.append("📝 All Tests:")
            for test in parsed["tests"][:50]:  # Limit to 50
                icon = {"passed": "✓", "failed": "✗", "error": "⚠", "skipped": "⊘"}.get(test["status"], "?")
                lines.append(f"  {icon} {test['file']}::{test['name']}")
            if len(parsed["tests"]) > 50:
                lines.append(f"  ... and {len(parsed['tests']) - 50} more")
            lines.append("")

        # Full output if requested
        if verbose:
            lines.append("-" * 60)
            lines.append("")
            lines.append("Full Output:")
            lines.append(output[:5000])
            if len(output) > 5000:
                lines.append("\n... (output truncated)")

    except subprocess.TimeoutExpired:
        lines.append(f"❌ Tests timed out after {timeout} seconds")
        success = False
    except Exception as e:
        lines.append(f"❌ Test execution error: {e}")
        success = False

    return ToolResult(
        ok=success,
        output="\n".join(lines),
    )


test_runner_tool = ToolDefinition(
    name="test_runner",
    description="Discover and run Python tests automatically. Supports pytest and unittest frameworks. Provides structured results with pass/fail counts, failure details, and optional coverage reporting.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory or file path to test (default: current directory)"},
            "framework": {"type": "string", "enum": ["auto", "pytest", "unittest"], "description": "Test framework to use (default: auto)"},
            "verbose": {"type": "boolean", "description": "Show detailed output (default: false)"},
            "coverage": {"type": "boolean", "description": "Enable coverage reporting (default: false, requires pytest-cov)"},
            "pattern": {"type": "string", "description": "Filter tests by name pattern"},
            "timeout": {"type": "number", "description": "Timeout in seconds (default: 60, max: 300)"},
        },
    },
    validator=_validate,
    run=_run,
)
