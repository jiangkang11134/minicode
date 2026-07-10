"""工具注册与初始化模块。

负责收集和注册 SmartCode 的所有内置工具，
包括核心工具、实用工具包装器（utility wrappers）以及 MCP 工具。
根据运行时配置选择加载的工具集。
"""

import os
from dataclasses import asdict

from minicode.mcp import create_mcp_backed_tools
from minicode.skills import discover_skills
from minicode.tooling import ToolRegistry
from minicode.tools.ask_user import ask_user_tool
from minicode.tools.batch_ops import batch_copy_tool, batch_delete_tool, batch_move_tool
from minicode.tools.code_nav import find_references_tool, find_symbols_tool, get_ast_info_tool
from minicode.tools.code_review import code_review_tool
from minicode.tools.diff_viewer import diff_viewer_tool
from minicode.tools.edit_file import edit_file_tool
from minicode.tools.file_tree import file_tree_tool
from minicode.tools.git import git_tool
from minicode.tools.grep_files import grep_files_tool
from minicode.tools.list_files import list_files_tool
from minicode.tools.load_skill import create_load_skill_tool
from minicode.tools.patch_file import patch_file_tool
from minicode.tools.read_file import read_file_tool
from minicode.tools.run_command import run_command_tool
from minicode.tools.sandbox_test import sandbox_test_tool
from minicode.tools.task import task_tool
from minicode.tools.test_runner import test_runner_tool
from minicode.tools.todo_write import todo_write_tool
from minicode.tools.web_fetch import web_fetch_tool
from minicode.tools.web_search import web_search_tool
from minicode.tools.write_file import write_file_tool

_CORE_TOOLS = [
    # User interaction
    ask_user_tool,
    # File operations
    list_files_tool,
    grep_files_tool,
    read_file_tool,
    write_file_tool,
    # modify_file_tool removed: identical to write_file (same _run/_validate)
    edit_file_tool,
    patch_file_tool,
    # Batch operations
    batch_copy_tool,
    batch_move_tool,
    batch_delete_tool,
    # Command execution
    run_command_tool,
    # Web tools
    web_fetch_tool,
    web_search_tool,
    # Task management
    todo_write_tool,
    # Sub-agent
    task_tool,
    # Git workflow
    git_tool,
    # Code intelligence
    find_symbols_tool,
    find_references_tool,
    get_ast_info_tool,
    code_review_tool,
    # Visualization
    file_tree_tool,
    diff_viewer_tool,
    # Testing
    sandbox_test_tool,
    test_runner_tool,
]

def _resolve_tool_profile(runtime: dict | None) -> str:
    """解析工具集配置档（profile）。

    优先级：环境变量 MINI_CODE_TOOL_PROFILE > runtime 参数 > 默认值 "core"。

    参数:
        runtime: 运行时配置字典，可选

    返回:
        小写化并去除首尾空格的工具集配置档名称
    """
    configured = (
        os.environ.get("MINI_CODE_TOOL_PROFILE")
        or (runtime or {}).get("toolProfile")
        or "core"
    )
    return str(configured).strip().lower()


def _is_full_tool_profile(profile: str) -> bool:
    """判断配置档是否为"完整工具集"模式。

    参数:
        profile: 工具集配置档名称

    返回:
        如果 profile 在 {"full", "utility", "utilities", "all"} 中返回 True
    """
    return profile in {"full", "utility", "utilities", "all"}


def _load_utility_wrapper_tools():
    """延迟加载实用工具包装器（utility wrappers）。

    惰性导入避免普通编码会话支付不常用的工具包装器的启动/导入开销，
    并保持默认模型工具表面简洁。

    返回:
        实用工具工具列表，包括 HTTP、JSON、正则、编码、时间、
        哈希、压缩、CSV、文本处理等功能
    """  # # Lazy import keeps normal coding sessions from paying startup/import cost
    # for rarely used wrappers and keeps the default model tool surface small.
    from minicode.tools.archive_utils import (
        gzip_compress_tool,
        gzip_decompress_tool,
        tar_create_tool,
        tar_extract_tool,
        zip_create_tool,
        zip_extract_tool,
    )
    from minicode.tools.crypto_utils import current_time_tool, hash_tool, hmac_tool, timestamp_tool
    from minicode.tools.csv_utils import csv_create_tool, csv_parse_tool
    from minicode.tools.encoding_utils import base64_decode_tool, base64_encode_tool, url_decode_tool, url_encode_tool
    from minicode.tools.http_utils import http_request_tool
    from minicode.tools.json_utils import json_format_tool, json_parse_tool
    from minicode.tools.regex_utils import regex_replace_tool, regex_test_tool
    from minicode.tools.text_utils import (
        line_count_tool,
        random_string_tool,
        text_dedupe_tool,
        text_join_tool,
        text_sort_tool,
        uuid_generate_tool,
    )

    return [
        http_request_tool,
        json_format_tool,
        json_parse_tool,
        regex_test_tool,
        regex_replace_tool,
        base64_encode_tool,
        base64_decode_tool,
        url_encode_tool,
        url_decode_tool,
        current_time_tool,
        timestamp_tool,
        hash_tool,
        hmac_tool,
        gzip_compress_tool,
        gzip_decompress_tool,
        tar_create_tool,
        tar_extract_tool,
        zip_create_tool,
        zip_extract_tool,
        csv_parse_tool,
        csv_create_tool,
        uuid_generate_tool,
        text_sort_tool,
        text_dedupe_tool,
        text_join_tool,
        line_count_tool,
        random_string_tool,
    ]


def create_default_tool_registry(cwd: str, runtime: dict | None = None) -> ToolRegistry:
    """创建默认的工具注册表。

    合并核心工具集、可选实用工具（根据 profile 配置）、
    技能加载工具和 MCP 工具，构成完整的工具注册表。

    参数:
        cwd: 当前工作目录，用于发现技能工具
        runtime: 运行时配置字典（可选），可包含 toolProfile 和 mcpServers

    返回:
        配置好的 ToolRegistry 实例
    """
    skills = [asdict(skill) for skill in discover_skills(cwd)]
    mcp = create_mcp_backed_tools(cwd=cwd, mcp_servers=dict(runtime.get("mcpServers", {})) if runtime else {})
    profile = _resolve_tool_profile(runtime)
    tools = list(_CORE_TOOLS)
    if _is_full_tool_profile(profile):
        tools.extend(_load_utility_wrapper_tools())
    tools.extend(
        [
            create_load_skill_tool(cwd),
            *mcp["tools"],
        ]
    )
    return ToolRegistry(
        tools,
        skills=skills,
        mcp_servers=mcp["servers"],
        disposer=mcp["dispose"],
    )
