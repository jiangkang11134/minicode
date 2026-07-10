"""命令执行工具。

提供在允许列表中的命令执行功能，支持超时控制、输出截断、后台任务启动，
以及权限提示和 shell 片段风险检测。兼容 Windows 和 Unix 平台。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence

from minicode.background_tasks import register_background_shell_task
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path

# 命令执行超时（秒）- 5 分钟
COMMAND_TIMEOUT = 300

# 最大输出大小（字符）- 防止超大输出撑爆上下文
MAX_OUTPUT_CHARS = 200_000


def _command_output_encoding() -> str:
    """获取命令输出解码使用的编码格式。

    可通过环境变量 ``MINICODE_COMMAND_ENCODING`` 覆盖。在中文 Windows 系统上，
    旧式命令（``dir``、``systeminfo``、打印 CJK 的批处理脚本）使用 OEM 代码页输出，
    可设置为 ``cp936``（或 ``gbk``）以避免 UTF-8 默认编码下的乱码。
    默认为 ``utf-8``（适用于现代工具和设置了 ``PYTHONUTF8=1`` 的 Python）。

    返回:
        编码格式字符串。
    """
    return os.environ.get("MINICODE_COMMAND_ENCODING", "utf-8").strip() or "utf-8"


def _decode_command_output(data: bytes | str | None) -> str:
    """使用配置的命令编码解码子进程输出字节。

    不会抛出异常：未知编码名称或解码失败会自动回退到 UTF-8 并使用替换字符，
    确保错误的 ``MINICODE_COMMAND_ENCODING`` 值不会导致命令执行崩溃。

    参数:
        data: 子进程输出的字节数据、字符串或 None。

    返回:
        解码后的字符串，如果输入为 None 则返回空字符串。
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    encoding = _command_output_encoding()
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")



def _truncate_large_output(output: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """截断过大的命令输出以防止上下文膨胀。

    保留前 60% 和后 40% 的内容，中间部分用省略标记替代，并显示截断的行数和总字符数。

    参数:
        output: 原始命令输出字符串。
        max_chars: 最大字符数阈值，默认为 MAX_OUTPUT_CHARS (200,000)。

    返回:
        截断后的输出字符串；如果未超过阈值则返回原始内容。
    """
    if len(output) <= max_chars:
        return output

    lines = output.split("\n")
    total_lines = len(lines)
    # Keep head (first 60%) and tail (last 40%)
    head_lines = int(total_lines * 0.6)
    tail_lines = total_lines - head_lines
    if tail_lines > int(total_lines * 0.4):
        tail_lines = int(total_lines * 0.4)
        head_lines = total_lines - tail_lines

    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    omitted = total_lines - head_lines - tail_lines
    return f"{head}\n\n... [{omitted} lines omitted, output was {len(output):,} chars] ...\n\n{tail}"

# Read-only commands that never need permission prompts.
# Includes both Unix and Windows equivalents.
READONLY_COMMANDS = {
    # Unix
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "sed",
    "echo",
    "df",
    "du",
    "whoami",
    # Windows equivalents
    "dir",
    "type",
    "where",
    "findstr",
    "more",
    "hostname",
}

# Development commands (write access but commonly allowed).
DEVELOPMENT_COMMANDS = {
    "git",
    "npm",
    "node",
    "python",
    "python3",
    "pytest",
    "bash",
    "sh",
    # Windows-common development tools
    "pip",
    "pip3",
    "cargo",
    "go",
    "make",
    "cmake",
    "dotnet",
    "powershell",
    "pwsh",
    "cmd",
}


def split_command_line(command_line: str) -> list[str]:
    """将命令字符串分割为令牌列表。

    在 Windows 上，``shlex.split(posix=True)`` 可能会在反斜杠路径（如 ``C:\\Users\\foo``）上出错。
    先回退到 ``posix=False`` 保留反斜杠，最后再尝试简单的空白符分割作为最后手段。

    参数:
        command_line: 要分割的完整命令字符串。

    返回:
        命令令牌列表。
    """
    if os.name == "nt":
        try:
            return shlex.split(command_line, posix=False)
        except ValueError:
            # If even non-posix fails, fall back to simple whitespace split
            return command_line.split()
    return shlex.split(command_line, posix=True)


def _is_allowed_command(command: str) -> bool:
    """检查命令是否在允许执行的命令列表中。

    参数:
        command: 命令名称（不含参数）。

    返回:
        如果命令在只读命令或开发命令集合中，返回 True。
    """
    cmd = command.lower() if os.name == "nt" else command
    return cmd in READONLY_COMMANDS or cmd in DEVELOPMENT_COMMANDS


def _is_read_only_command(command: str) -> bool:
    """检查命令是否为只读命令。

    只读命令是指不会修改文件系统的命令（如 ls、dir 等），执行时通常不需要权限提示。

    参数:
        command: 命令名称（不含参数）。

    返回:
        如果命令在只读命令集合中，返回 True。
    """
    cmd = command.lower() if os.name == "nt" else command
    return cmd in READONLY_COMMANDS


def _looks_like_shell_snippet(command: str, args: list[str]) -> bool:
    """判断命令是否看起来像 shell 片段而非简单命令。

    当命令中包含管道、重定向、变量替换等 shell 特殊字符且没有单独指定参数时，
    将其视为 shell 片段，需要通过 shell 来执行。

    参数:
        command: 原始命令字符串。
        args: 单独指定的参数列表。

    返回:
        如果是 shell 片段则返回 True。
    """
    return not args and any(char in command for char in "|&;<>()$`")


def _is_background_shell_snippet(command: str, args: list[str]) -> bool:
    """判断命令是否为后台 shell 片段（以单独的 & 结尾）。

    后台命令会在独立的进程中启动，不阻塞当前会话。

    参数:
        command: 原始命令字符串。
        args: 单独指定的参数列表。

    返回:
        如果是后台 shell 片段则返回 True。
    """
    trimmed = command.strip()
    return not args and trimmed.endswith("&") and not trimmed.endswith("&&")


def _strip_trailing_background_operator(command: str) -> str:
    """移除命令末尾的后台操作符 &。

    去除尾部空白和单独的 & 符号，获取实际要执行的命令内容。

    参数:
        command: 原始命令字符串。

    返回:
        移除了尾部 & 操作符后的命令字符串。
    """
    return command.strip().removesuffix("&").strip()


def _classify_shell_snippet_risk(command: str) -> str | None:
    """对 shell 片段进行风险分类检测。

    检测常见的危险操作模式，包括：递归删除（rm -rf、Windows del/rd）、
    从网络下载并执行脚本、显式调用命令解释器等高风险操作。

    参数:
        command: 要检测的 shell 命令字符串。

    返回:
        如果检测到风险操作，返回描述风险原因的字符串；否则返回 None。
    """
    lowered = command.lower()
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    if re.search(r"\brm\s+-[a-z]*r[a-z]*f\b|\brm\s+-[a-z]*f[a-z]*r\b", collapsed):
        return f"shell snippet contains rm -rf payload: {command}"
    if re.search(r"\b(del|erase)\b.*\s/(s|q)\b", collapsed):
        return f"shell snippet contains recursive Windows delete payload: {command}"
    if re.search(r"\b(rmdir|rd)\b.*\s/s\b", collapsed):
        return f"shell snippet contains recursive Windows directory removal: {command}"
    if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|fish)\b", collapsed):
        return f"shell snippet downloads and executes a shell script: {command}"
    if re.search(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b.*\|\s*(iex|invoke-expression)\b", collapsed):
        return f"shell snippet downloads and executes PowerShell code: {command}"
    if re.search(r"\b(powershell|pwsh)\b.*\b(iex|invoke-expression)\b", collapsed):
        return f"shell snippet invokes PowerShell expression execution: {command}"
    if re.search(r"\b(sh|bash|zsh|fish|cmd|powershell|pwsh)\b\s+(-c|/c|/command)\b", collapsed):
        return f"shell snippet invokes an explicit command interpreter: {command}"
    return None


def _normalize_command_input(input_data: dict) -> tuple[str, list[str]]:
    """规范化命令输入，提取命令名称和参数列表。

    如果 input_data 中提供了 args 字段则直接使用，否则通过 split_command_line 解析命令字符串。

    参数:
        input_data: 包含 command 和 args 字段的输入字典。

    返回:
        (命令名称, 参数列表) 的元组。
    """
    command = str(input_data.get("command", "")).strip()
    raw_args = input_data.get("args") or []
    if raw_args:
        return command, [str(arg) for arg in raw_args]
    parsed = split_command_line(command) if command else []
    return (parsed[0], parsed[1:]) if parsed else ("", [])


def _is_windows_shell_builtin(command: str) -> bool:
    """判断 Windows 命令是否为 shell 内置命令。

    内置命令（如 cd、dir、echo 等）需要通过 cmd.exe 执行，
    无法作为独立的可执行文件运行。

    参数:
        command: 命令名称。

    返回:
        如果当前为 Windows 平台且命令为内置命令，返回 True。
    """
    return os.name == "nt" and command.lower() in {
        "cd",
        "chdir",
        "cls",
        "copy",
        "date",
        "del",
        "dir",
        "echo",
        "erase",
        "md",
        "mkdir",
        "mklink",
        "move",
        "rd",
        "ren",
        "rename",
        "rmdir",
        "time",
        "type",
        "ver",
        "vol",
    }


def _build_execution_command(
    raw_command: str,
    normalized_command: str,
    normalized_args: Sequence[str],
    *,
    use_shell: bool,
    background_shell: bool,
) -> tuple[str, list[str]]:
    """构建最终的执行命令和参数列表。

    根据是否使用 shell、是否为后台执行、是否为 Windows 内置命令等情况，
    构建适合平台和执行模式的命令和参数组合。

    参数:
        raw_command: 原始命令字符串。
        normalized_command: 规范化后的命令名称。
        normalized_args: 规范化后的参数列表。
        use_shell: 是否通过 shell 执行。
        background_shell: 是否为后台执行。

    返回:
        (可执行文件路径, 参数列表) 的元组。
    """
    if use_shell:
        shell_command = _strip_trailing_background_operator(raw_command) if background_shell else raw_command
        if os.name == "nt":
            return "cmd", ["/d", "/s", "/c", shell_command]
        # Use the user's preferred shell (macOS defaults to zsh since
        # Catalina).  Fall back to /bin/sh for maximum POSIX compatibility.
        shell = os.environ.get("SHELL", "/bin/sh")
        return shell, ["-lc", shell_command]
    if _is_windows_shell_builtin(normalized_command):
        quoted_args = subprocess.list2cmdline(list(normalized_args))
        shell_command = normalized_command if not quoted_args else f"{normalized_command} {quoted_args}"
        return "cmd", ["/d", "/s", "/c", shell_command]
    return normalized_command, list(normalized_args)


def _validate(input_data: dict) -> dict:
    """验证并规范化运行命令工具的输入参数。

    检查 command 是否为非空字符串，args 是否为列表，cwd 是否为字符串（可选），
    以及 timeout 是否在 [1, 600] 秒范围内。

    参数:
        input_data: 包含 command、args、cwd、timeout 等字段的原始输入字典。

    返回:
        规范化后的参数字典。

    抛出:
        ValueError: 如果参数类型无效。
    """
    command = input_data.get("command")
    if not isinstance(command, str):
        raise ValueError("command is required")
    args = input_data.get("args") or []
    if not isinstance(args, list):
        raise ValueError("args must be a list")
    cwd = input_data.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    # Optional timeout (seconds), clamped to [1, 600]
    timeout = input_data.get("timeout")
    if timeout is not None:
        try:
            timeout = max(1, min(600, int(timeout)))
        except (ValueError, TypeError):
            timeout = None
    return {"command": command, "args": [str(arg) for arg in args], "cwd": cwd, "timeout": timeout}


def _run(input_data: dict, context) -> ToolResult:
    """执行命令操作。

    主要执行流程：解析工作目录 -> 规范化命令输入 -> 检测 shell 片段和后台执行需求
    -> 检查命令权限 -> 根据平台选择执行方式（Unix 使用 PTY，Windows 使用 subprocess）
    -> 解码输出 -> 截断过大输出 -> 返回结果。

    支持后台命令启动（以 & 结尾的 shell 片段）、超时控制、以及权限提示管理。

    参数:
        input_data: 已验证的输入字典，包含 command、args、cwd、timeout。
        context: 工具执行上下文，用于解析路径和管理权限。

    返回:
        ToolResult，包含命令执行结果和输出内容。
    """
    effective_cwd = str(resolve_tool_path(context, input_data["cwd"], "list")) if input_data.get("cwd") else context.cwd
    normalized_command, normalized_args = _normalize_command_input(input_data)
    if not normalized_command:
        return ToolResult(ok=False, output="Command not allowed: empty command")

    raw_args = input_data.get("args") or []
    use_shell = _looks_like_shell_snippet(input_data["command"], raw_args)
    background_shell = _is_background_shell_snippet(input_data["command"], raw_args)
    known_command = _is_allowed_command(normalized_command)

    command, args = _build_execution_command(
        input_data["command"],
        normalized_command,
        normalized_args,
        use_shell=use_shell,
        background_shell=background_shell,
    )
    shell_prompt_reason = _classify_shell_snippet_risk(input_data["command"]) if use_shell else None
    force_prompt_reason = (
        shell_prompt_reason
        if shell_prompt_reason
        else None if use_shell or known_command else f"Unknown command '{normalized_command}' is not in the built-in read-only/development set"
    )

    if context.permissions is not None:
        if force_prompt_reason:
            context.permissions.ensure_command(command, args, effective_cwd, force_prompt_reason=force_prompt_reason)
        elif use_shell or not _is_read_only_command(normalized_command):
            context.permissions.ensure_command(command, args, effective_cwd)

    if use_shell and background_shell:
        # Platform-specific process isolation flags
        popen_kwargs: dict = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # On Unix, start the background process in its own session so
            # it is not killed when the parent terminal closes.
            popen_kwargs["start_new_session"] = True

        child = subprocess.Popen(  # noqa: S603
            [command, *args],
            cwd=effective_cwd,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )

        if child.pid is None:
            return ToolResult(
                ok=False,
                output="Failed to get PID for background command. Process may have exited immediately.",
            )

        background_task = register_background_shell_task(
            command=_strip_trailing_background_operator(input_data["command"]),
            pid=child.pid,
            cwd=effective_cwd,
        )
        return ToolResult(
            ok=True,
            output=f"Background command started.\nTASK: {background_task.taskId}\nPID: {background_task.pid}",
            backgroundTask=background_task,
        )

    if sys.platform != "win32":
        try:
            import pty
            import select

            master_fd, slave_fd = pty.openpty()
            effective_timeout = input_data.get("timeout") or COMMAND_TIMEOUT

            process = subprocess.Popen(
                [command, *args],
                cwd=effective_cwd,
                env=os.environ.copy(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )

            os.close(slave_fd)
            output_bytes = bytearray()
            timed_out = False

            try:
                while True:
                    r, _, _ = select.select([master_fd], [], [], effective_timeout)
                    if not r:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break

                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        output_bytes.extend(data)
                    except OSError:
                        # EIO happens when child closes the PTY or exits
                        break
            finally:
                os.close(master_fd)
                if not timed_out:
                    process.wait()

            output_str = _decode_command_output(bytes(output_bytes)).strip()
            output_str = output_str.replace("\r\n", "\n")
            output_str = _truncate_large_output(output_str)

            if timed_out:
                return ToolResult(
                    ok=False,
                    output=f"Command timed out after {effective_timeout} seconds (process killed).\nPartial output:\n{output_str}",
                )
            return ToolResult(ok=process.returncode == 0, output=output_str)

        except ImportError:
            pass  # Fallback to subprocess on systems without pty

    try:
        effective_timeout = input_data.get("timeout") or COMMAND_TIMEOUT
        completed = subprocess.run(  # noqa: S603
            [command, *args],
            cwd=effective_cwd,
            env=os.environ.copy(),
            capture_output=True,
            check=False,
            timeout=effective_timeout,
        )
        output = "\n".join(
            part for part in [
                _decode_command_output(completed.stdout).strip(),
                _decode_command_output(completed.stderr).strip(),
            ] if part
        ).strip()
        output = _truncate_large_output(output)
        return ToolResult(ok=completed.returncode == 0, output=output)
    except subprocess.TimeoutExpired as e:
        # Capture partial output from timeout
        partial_stdout = _decode_command_output(e.stdout).strip()
        partial_stderr = _decode_command_output(e.stderr).strip()
        partial = "\n".join(part for part in [partial_stdout, partial_stderr] if part)
        if partial:
            partial = f"\nPartial output:\n{_truncate_large_output(partial)}"
        return ToolResult(
            ok=False,
            output=f"Command timed out after {effective_timeout} seconds (process killed).{partial}",
        )


run_command_tool = ToolDefinition(
    name="run_command",
    description="Run a common development command from an allowlist. Supports optional timeout parameter (1-600 seconds).",
    input_schema={"type": "object", "properties": {"command": {"type": "string", "description": "Command to run"}, "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments"}, "cwd": {"type": "string", "description": "Working directory"}, "timeout": {"type": "integer", "description": "Timeout in seconds (1-600, default 300)"}}, "required": ["command"]},
    validator=_validate,
    run=_run,
)  #
