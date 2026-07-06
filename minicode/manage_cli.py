"""MiniCode 管理 CLI — 提供子命令用于管理 MCP 服务器和 Skill。

支持以下管理命令：
  minicode mcp list/add/remove
  minicode skills list/add/remove
  minicode valid-config / help
"""
from __future__ import annotations

from minicode.config import get_mcp_config_path, load_scoped_mcp_servers, save_scoped_mcp_servers
from minicode.skills import discover_skills, install_skill, remove_managed_skill


def _print_usage() -> None:
    """打印管理命令的使用说明到标准输出。"""
    # print(
        "minicode management commands\n\n"
        "minicode mcp list [--project]\n"
        "minicode mcp add <name> [--project] [--protocol <auto|content-length|newline-json>] [--env KEY=VALUE ...] -- <command> [args...]\n"
        "minicode mcp remove <name> [--project]\n\n"
        "minicode skills list\n"
        "minicode skills add <path-to-skill-or-dir> [--name <name>] [--project]\n"
        "minicode skills remove <name> [--project]\n\n"
        "minicode valid-config"
    )


def _parse_scope(args: list[str]) -> tuple[str, list[str]]:
    """解析参数列表中的 --project 选项，确定作用域是 project 还是 user。

    参数:
        args: 原始参数列表

    返回:
        (scope, rest) 元组，scope 为 "project" 或 "user"，
        rest 为移除 --project 后的剩余参数
    """
    # rest = list(args)
    if "--project" in rest:
        rest.remove("--project")
        return "project", rest
    return "user", rest


def _take_option(args: list[str], name: str) -> str | None:
    """从参数列表中取出一个单值选项（如 --name <value>）。

    参数:
        args: 参数列表（会被原地修改，移除选项及值）
        name: 选项名称（如 "--name"）

    返回:
        选项的值，如果选项不存在则返回 None

    抛出:
        RuntimeError: 选项存在但缺少对应的值
    """
    # if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        raise RuntimeError(f"Missing value for {name}")
    value = args[index + 1]
    del args[index : index + 2]
    return value


def _take_repeat_option(args: list[str], name: str) -> list[str]:
    """从参数列表中取出所有出现的重复选项值（如 --env KEY=VALUE）。

    参数:
        args: 参数列表（会被原地修改，移除所有匹配的选项及值）
        name: 选项名称（如 "--env"）

    返回:
        所有选项值的列表

    抛出:
        RuntimeError: 选项存在但缺少对应的值
    """
    # values: list[str] = []
    while name in args:
        index = args.index(name)
        if index + 1 >= len(args):
            raise RuntimeError(f"Missing value for {name}")
        values.append(args[index + 1])
        del args[index : index + 2]
    return values


def _parse_env_pairs(values: list[str]) -> dict[str, str]:
    """将 KEY=VALUE 格式的字符串列表解析为环境变量字典。

    参数:
        values: 包含 KEY=VALUE 格式的字符串列表

    返回:
        解析后的环境变量字典

    抛出:
        RuntimeError: 条目格式不正确（缺少等号或键名为空）
    """
    # env: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise RuntimeError(f"Invalid --env value: {entry}")
        key, value = entry.split("=", 1)
        if not key.strip():
            raise RuntimeError(f"Invalid --env value: {entry}")
        env[key.strip()] = value
    return env


def _handle_mcp_command(cwd: str, args: list[str]) -> bool:
    """处理 mcp 子命令（list / add / remove）。

    参数:
        cwd: 当前工作目录路径
        args: 子命令后的参数列表

    返回:
        始终返回 True，表示命令已处理
    """
    # if not args:
        _print_usage()
        return True
    subcommand, *rest_args = args
    scope, rest = _parse_scope(rest_args)
    if subcommand == "list":
        servers = load_scoped_mcp_servers(scope, cwd)
        if not servers:
            print(f"No MCP servers configured in {get_mcp_config_path(scope, cwd)}.")
            return True
        for name, server in servers.items():
            args_summary = " ".join(server.get("args", []))
            protocol = f" protocol={server['protocol']}" if server.get("protocol") else ""
            print(f"{name}: {server['command']} {args_summary}{protocol}".strip())
        return True
    if subcommand == "add":
        if "--" not in rest:
            raise RuntimeError("Use `--` before the MCP command.")
        separator_index = rest.index("--")
        head = rest[:separator_index]
        command_parts = rest[separator_index + 1 :]
        if not head or not command_parts:
            raise RuntimeError("Missing MCP server name or command.")
        name = head.pop(0)
        protocol = _take_option(head, "--protocol")
        env = _parse_env_pairs(_take_repeat_option(head, "--env"))
        if head:
            raise RuntimeError(f"Unknown arguments: {' '.join(head)}")
        command, *command_args = command_parts
        existing = load_scoped_mcp_servers(scope, cwd)
        existing[name] = {
            "command": command,
            "args": command_args,
            "env": env or None,
            "protocol": protocol,
        }
        save_scoped_mcp_servers(scope, existing, cwd)
        print(f"Added MCP server {name} to {get_mcp_config_path(scope, cwd)}")
        return True
    if subcommand == "remove":
        if not rest:
            raise RuntimeError("Missing MCP server name.")
        name = rest[0]
        existing = load_scoped_mcp_servers(scope, cwd)
        if name not in existing:
            print(f"MCP server {name} not found in {get_mcp_config_path(scope, cwd)}")
            return True
        del existing[name]
        save_scoped_mcp_servers(scope, existing, cwd)
        print(f"Removed MCP server {name} from {get_mcp_config_path(scope, cwd)}")
        return True
    _print_usage()
    return True


def _handle_skills_command(cwd: str, args: list[str]) -> bool:
    """处理 skills 子命令（list / add / remove）。

    参数:
        cwd: 当前工作目录路径
        args: 子命令后的参数列表

    返回:
        始终返回 True，表示命令已处理
    """
    # if not args:
        _print_usage()
        return True
    subcommand, *rest_args = args
    scope, rest = _parse_scope(rest_args)
    if subcommand == "list":
        skills = discover_skills(cwd)
        if not skills:
            print("No skills discovered.")
            return True
        for skill in skills:
            print(f"{skill.name}: {skill.description} ({skill.path})")
        return True
    if subcommand == "add":
        if not rest:
            raise RuntimeError("Missing skill source path.")
        source_path = rest[0]
        name = _take_option(rest, "--name")
        result = install_skill(cwd, source_path, name=name, scope=scope)
        print(f"Installed skill {result['name']} at {result['targetPath']}")
        return True
    if subcommand == "remove":
        if not rest:
            raise RuntimeError("Missing skill name.")
        result = remove_managed_skill(cwd, rest[0], scope=scope)
        if not result["removed"]:
            print(f"Skill {rest[0]} not found at {result['targetPath']}")
            return True
        print(f"Removed skill {rest[0]} from {result['targetPath']}")
        return True
    _print_usage()
    return True


def maybe_handle_management_command(cwd: str, argv: list[str]) -> bool:
    """入口函数：判断 argv 是否匹配管理命令并路由到对应的处理函数。

    支持的命令类别：mcp、skills、valid-config/validate-config、help。

    参数:
        cwd: 当前工作目录路径
        argv: 完整命令行参数列表

    返回:
        如果命令被识别并处理则返回 True，否则返回 False
    """
    # if not argv:
        return False
    category, *rest = argv
    if category == "mcp":
        return _handle_mcp_command(cwd, rest)
    if category == "skills":
        return _handle_skills_command(cwd, rest)
    if category in {"valid-config", "validate-config"}:
        from minicode.config import format_config_diagnostic
        print(format_config_diagnostic(cwd))
        return True
    if category in {"help", "--help", "-h"}:
        _print_usage()
        return True
    return False
