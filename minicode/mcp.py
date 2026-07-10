"""MCP（Model Context Protocol）客户端实现 — 管理 MCP 服务器的生命周期和通信。

提供 StdioMcpClient 类通过标准输入/输出与 MCP 服务器进程通信，
支持 Content-Length 和 Newline-JSON 两种协议格式。包含安全验证、
懒初始化、自动重试和资源管理功能。

核心功能：
  - 命令和参数的安全白名单验证
  - 懒启动：服务器进程在首次请求时才启动
  - 双协议支持：自动检测 Content-Length 或 Newline-JSON
  - 工具/资源/Prompt 的自动发现与缓存
  - 结果格式化为统一的 ToolResult
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult

# 安全常量：禁止在命令参数中出现的危险字符
DANGEROUS_SHELL_CHARS = set('|&;`$(){}<>\n\r')

# MCP payload 大小上限（防止恶意服务端制造 OOM）
MAX_MCP_PAYLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

# 允许的命令白名单（常见的 MCP 服务器命令）
ALLOWED_COMMANDS = {
    'node', 'npm', 'npx', 'python', 'python3', 'pip', 'pip3',
    'uv', 'deno', 'bun', 'cargo', 'go', 'java', 'javac',
    'ruby', 'gem', 'dotnet', 'curl', 'wget',
}

# Windows 可执行后缀。node 工具链的 npx/npm 等以 .cmd 批处理包装器形式存在，
# 校验白名单前需要剥离这些后缀，否则 "npx.cmd" 会被误判为不在白名单中。
_WIN_EXEC_EXTS = (".exe", ".cmd", ".bat")


JsonRpcProtocol = str


@dataclass(slots=True)
class McpServerSummary:
    """MCP 服务器的状态摘要。

    用于在 UI 或日志中展示服务器的基本信息、运行状态和工具/资源/Prompt 数量。
    """
    name: str
    command: str
    status: str
    toolCount: int
    error: str | None = None
    protocol: str | None = None
    resourceCount: int | None = None
    promptCount: int | None = None


def _sanitize_tool_segment(value: str) -> str:
    """将字符串清理为安全的工具名片段（仅保留字母数字、下划线和连字符）。

    参数:
        value: 原始字符串

    返回:
        清理后的安全工具名片段，空结果时回退为 "tool"
    """
    normalized = "".join(char.lower() if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    normalized = normalized.strip("_")
    return normalized or "tool"


def _validate_mcp_command(command: str) -> None:
    """验证 MCP 命令的合法性"""
    # from pathlib import Path

    normalized = Path(command).resolve().as_posix()

    # 不允许路径遍历字符
    if '..' in normalized or '~' in normalized:
        raise RuntimeError("Invalid MCP command: contains path traversal characters")

    # 提取命令的基本名称，剥离 Windows 可执行后缀（.exe/.cmd/.bat）
    base_command = Path(command).name.lower()
    for _exec_ext in _WIN_EXEC_EXTS:
        if base_command.endswith(_exec_ext):
            base_command = base_command[: -len(_exec_ext)]
            break

    # 如果是绝对路径，需要额外验证
    if Path(command).is_absolute():
        # 检查是否在常见的系统目录中
        home_posix = str(Path.home().as_posix())
        allowed_system_dirs = [
            '/usr/bin', '/usr/local/bin', '/usr/local/sbin', '/usr/sbin', '/opt',
            # macOS Homebrew
            '/opt/homebrew/bin', '/opt/homebrew/sbin',  # Apple Silicon
            '/usr/local/Cellar',  # Intel
            # Linux extras
            '/snap/bin',  # Ubuntu Snap
            '/home/linuxbrew/.linuxbrew/bin',  # Homebrew on Linux
            # User-level tool directories (pip --user, pipx, cargo, nvm, etc.)
            f'{home_posix}/.local/bin',
            f'{home_posix}/.cargo/bin',
            f'{home_posix}/.nvm',
        ]
        if os.name == 'nt':
            allowed_system_dirs.extend([
                'C:\\Program Files',
                'C:\\Program Files (x86)',
                'C:\\Windows\\System32',
            ])

        is_in_allowed_dir = any(normalized.lower().startswith(d.lower()) for d in allowed_system_dirs)

        # 不在允许的系统目录且不在白名单中
        if not is_in_allowed_dir and base_command not in ALLOWED_COMMANDS:
            raise RuntimeError(
                f"MCP command \"{command}\" is not in the allowed list. "
                f"Use a whitelisted command or place the executable in a standard system directory."
            )

        # 禁止危险的系统 shell
        dangerous_shells = ['cmd.exe', 'command.com', 'powershell.exe', 'pwsh.exe']
        if any(normalized.lower().endswith(d) for d in dangerous_shells):
            raise RuntimeError(
                f"MCP command \"{command}\" is a dangerous system shell. "
                f"Direct execution of shells is not allowed for security reasons."
            )
        return

    # 相对路径必须在白名单中
    if base_command not in ALLOWED_COMMANDS:
        raise RuntimeError(
            f"MCP command \"{command}\" is not in the allowed list. "
            f"Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}. "
            f"Use absolute paths for custom commands."
        )


def _validate_mcp_args(args: list[str]) -> None:
    """验证 MCP 参数不包含危险的 shell 元字符"""
    for arg in args:
        for char in arg:
            if char in DANGEROUS_SHELL_CHARS:
                raise RuntimeError(
                    f"Invalid MCP argument: contains dangerous shell character '{char}'. "
                    f"MCP server arguments cannot contain shell metacharacters for security reasons."
                )


def _prepare_spawn(command: str, args: list[str]) -> tuple[list[str] | str, dict]:
    """把 MCP 命令解析成可被 subprocess 启动的形式。

    Windows 上 npx/npm 等是 ``.cmd`` 批处理包装器。``subprocess`` 在 ``shell=False``
    时底层走 ``CreateProcess``，它既不能直接运行 ``.cmd``/``.bat``，又不会按 PATHEXT
    搜索，于是裸命令 ``npx`` 会被当成不存在的 ``npx.exe``，报“命令未找到: npx”
    （GitHub issue #7）。这里用 :func:`shutil.which`（遵循 PATHEXT）解析出真正的
    可执行文件，并把批处理包装器通过 ``cmd.exe``（即 ``shell=True``）启动。参数已经
    过 :func:`_validate_mcp_args` 校验不含 shell 元字符，因此 ``shell=True`` 是安全的。

    返回 ``(spawn_exec, extra_popen_kwargs)``：``spawn_exec`` 在 ``shell=False`` 时为
    列表，在 ``shell=True`` 时为单条命令行字符串。
    """
    if os.name == "nt":
        resolved = shutil.which(command)
        if resolved:
            if resolved.lower().endswith((".cmd", ".bat")):
                # 批处理包装器需要 cmd.exe 解释执行
                return subprocess.list2cmdline([resolved, *args]), {"shell": True}
            return [resolved, *args], {}
    return [command, *args], {}


def _normalize_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """标准化 JSON Schema，确保输入 schema 始终为合法的 dict。"""
    if not isinstance(schema, dict):
        return {"type": "object", "additionalProperties": True}
    return schema


def _format_tool_call_result(result: Any) -> ToolResult:
    """将 MCP 工具调用返回结果格式化为统一的 ToolResult。

    解析 content 列表和 structuredContent 字段，自动判断是否包含错误。

    参数:
        result: MCP 工具调用返回的原始结果

    返回:
        格式化的 ToolResult 对象
    """
    if not isinstance(result, dict):
        return ToolResult(ok=True, output=json.dumps(result, indent=2, ensure_ascii=False))
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list) and content:
        parts.append("\n\n".join(_format_content_block(block) for block in content))
    if "structuredContent" in result:
        parts.append("STRUCTURED_CONTENT:\n" + json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    if not parts:
        parts.append(json.dumps(result, indent=2, ensure_ascii=False))
    return ToolResult(ok=not bool(result.get("isError")), output="\n\n".join(parts).strip())


def _format_read_resource_result(result: Any) -> ToolResult:
    """将 MCP 资源读取结果格式化为统一的 ToolResult。

    解析 contents 列表，支持 text 和 blob 两种资源格式。

    参数:
        result: MCP resources/read 返回的原始结果

    返回:
        格式化的 ToolResult 对象
    """
    if not isinstance(result, dict):
        return ToolResult(ok=False, output=json.dumps(result, indent=2, ensure_ascii=False))
    contents = result.get("contents", [])
    if not contents:
        return ToolResult(ok=True, output="No resource contents returned.")
    rendered = []
    for item in contents:
        header_lines = [f"URI: {item.get('uri', '(unknown)')}"]
        if item.get("mimeType"):
            header_lines.append(f"MIME: {item['mimeType']}")
        header = "\n".join(header_lines) + "\n\n"
        if isinstance(item.get("text"), str):
            rendered.append(header + item["text"])
        elif isinstance(item.get("blob"), str):
            rendered.append(header + "BLOB:\n" + item["blob"])
        else:
            rendered.append(header + json.dumps(item, indent=2, ensure_ascii=False))
    return ToolResult(ok=True, output="\n\n".join(rendered))


def _format_prompt_result(result: Any) -> ToolResult:
    """将 MCP Prompt 获取结果格式化为统一的 ToolResult。

    解析 description、messages 列表，按角色（role）组织文本内容。

    参数:
        result: MCP prompts/get 返回的原始结果

    返回:
        格式化的 ToolResult 对象
    """
    if not isinstance(result, dict):
        return ToolResult(ok=False, output=json.dumps(result, indent=2, ensure_ascii=False))
    header = f"DESCRIPTION: {result['description']}\n\n" if result.get("description") else ""
    body_parts = []
    for message in result.get("messages", []):
        role = message.get("role", "unknown")
        content = message.get("content")
        if isinstance(content, str):
            rendered = content
        elif isinstance(content, list):
            rendered = "\n".join(
                str(part["text"]) if isinstance(part, dict) and "text" in part else json.dumps(part, indent=2, ensure_ascii=False)
                for part in content
            )
        else:
            rendered = json.dumps(content, indent=2, ensure_ascii=False)
        body_parts.append(f"[{role}]\n{rendered}")
    output = (header + "\n\n".join(body_parts)).strip()
    return ToolResult(ok=True, output=output or json.dumps(result, indent=2, ensure_ascii=False))


class StdioMcpClient:
    """通过标准输入/输出与 MCP 服务器进程通信的客户端。

    采用懒初始化策略：服务器进程在首次请求时才启动，减少启动时间和资源消耗。
    支持 Content-Length 和 Newline-JSON 两种协议格式，自动检测协议类型。
    提供工具、资源和 Prompt 的列表查询与调用功能，结果带有缓存。

    使用方式:
        client = StdioMcpClient("my-server", config, cwd)
        client.start()
        tools = client.list_tools()
        result = client.call_tool("tool_name", {"arg": "value"})
        client.close()
    """
    def __init__(self, server_name: str, config: dict[str, Any], cwd: str) -> None:
        """初始化 MCP 客户端。

        参数:
            server_name: MCP 服务器名称
            config: 服务器配置字典，包含 command、args、env、protocol 等
            cwd: 当前工作目录路径
        """
        # self.server_name = server_name
        self.config = config
        self.cwd = cwd
        self.process: subprocess.Popen[bytes] | None = None
        self.protocol: JsonRpcProtocol | None = None
        self.next_id = 1
        self._pending: dict[int, Queue[Any]] = {}
        self._lock = threading.Lock()
        self.stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        # Lazy init state
        self._started = False
        self._start_error: str | None = None
        self._tools_cache: list[dict[str, Any]] | None = None
        self._resources_cache: list[dict[str, Any]] | None = None
        self._prompts_cache: list[dict[str, Any]] | None = None

    @property
    def is_started(self) -> bool:
        """检查服务器是否已成功启动。"""
        # return self._started

    @property
    def start_error(self) -> str | None:
        """获取上次启动失败的错误信息，未失败则返回 None。"""
        # def _protocol_candidates(self) -> list[JsonRpcProtocol]:
        """返回待尝试的协议候选列表。

        根据配置的 protocol 参数决定：
        - "content-length" → 只尝试 Content-Length 协议
        - "newline-json" → 只尝试 Newline-JSON 协议
        - 未配置 → 按 [content-length, newline-json] 顺序自动探测
        """
        configured = self.config.get("protocol")
        if configured == "content-length":
            return ["content-length"]
        if configured == "newline-json":
            return ["newline-json"]
        return ["content-length", "newline-json"]

    def start(self) -> None:
        """启动 MCP 服务器进程（幂等操作）。

        如果已启动则直接返回。如果上次启动失败，重置错误状态后重试。
        会依次尝试候选协议进行连接和初始化握手，成功后设置 _started 标志。

        抛出:
            RuntimeError: 所有协议都尝试失败时抛出
        """
        if self._started:
            return

        if self._start_error is not None and self.process is None:
            # Previous attempt failed — reset for retry
            self._start_error = None

        last_error: Exception | None = None
        for protocol in self._protocol_candidates():
            try:
                self._spawn_process()
                self.protocol = protocol
                self.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mini-code", "version": "0.1.0"},
                    },
                    timeout_seconds=2.0,
                )
                self.notify("notifications/initialized", {})
                self._started = True
                self._start_error = None
                return
            except Exception as error:  # noqa: BLE001
                last_error = error
                self.close()

        self._start_error = str(last_error or f'Failed to connect MCP server "{self.server_name}".')
        raise RuntimeError(self._start_error)

    def _ensure_started(self) -> None:
        """确保在发起请求前服务器已启动。

        如果服务器标记为已启动但进程已退出，则先清理再重新启动。
        """
        if self._started and not self._is_process_alive():
            self.close()
        if not self._started:
            self.start()

    def _is_process_alive(self) -> bool:
        """检查子进程是否仍在运行。"""
        # return self.process is not None and self.process.poll() is None

    def _spawn_process(self) -> None:
        """实际启动 MCP 服务器子进程。

        执行步骤：
        1. 验证命令和参数的安全性（白名单 + shell 元字符检查）
        2. 解析工作目录和环境变量
        3. 通过 subprocess.Popen 启动进程
        4. 启动后台线程读取 stderr

        抛出:
            RuntimeError: 命令为空、验证失败或命令不存在时抛出
        """
        command = str(self.config.get("command", "")).strip()
        if not command:
            raise RuntimeError(f'MCP server "{self.server_name}" has no command configured.')

        # 安全验证：检查命令和参数的合法性
        _validate_mcp_command(command)
        _validate_mcp_args(list(self.config.get("args", []) or []))

        process_cwd = Path(self.cwd)
        if self.config.get("cwd"):
            process_cwd = (process_cwd / str(self.config["cwd"])).resolve()
        env = os.environ.copy()
        for key, value in dict(self.config.get("env", {}) or {}).items():
            env[str(key)] = str(value)

        popen_kwargs: dict = {}
        if os.name == "nt":
            # Prevent a console window from popping up for the child process
            CREATE_NO_WINDOW = 0x08000000
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW

        spawn_args = list(self.config.get("args", []) or [])
        spawn_exec, spawn_extra = _prepare_spawn(command, spawn_args)
        popen_kwargs.update(spawn_extra)
        try:
            self.process = subprocess.Popen(  # noqa: S603
                spawn_exec,
                cwd=str(process_cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **popen_kwargs,
            )
        except FileNotFoundError:
            raise RuntimeError(f"Command not found: {command}. Install it first and ensure it is available in PATH.") from None

        self.stderr_lines = []
        with self._lock:
            self._pending = {}
        self._stderr_thread = threading.Thread(target=self._consume_stderr, daemon=True)
        self._stderr_thread.start()

    def _consume_stderr(self) -> None:
        """后台线程：持续读取进程 stderr 输出，最多保留最近 8 行。"""
        # assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            try:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.stderr_lines.append(text)
                    self.stderr_lines = self.stderr_lines[-8:]
            except Exception:
                continue

    def _ensure_stdout_thread(self) -> None:
        """确保 stdout 读取线程已启动（单例模式）。"""
        if self._stdout_thread is not None:
            return
        self._stdout_thread = threading.Thread(target=self._consume_stdout, daemon=True)
        self._stdout_thread.start()

    def _consume_stdout(self) -> None:
        """后台线程：持续读取进程 stdout 并分派 JSON-RPC 消息。

        支持两种协议：
        - Newline-JSON：每行一个 JSON 对象
        - Content-Length：HTTP 风格的头部 + 消息体

        当子进程退出时，会通知所有待处理的请求。
        """
        # assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line_bytes = self.process.stdout.readline()
                if not line_bytes:
                    break

                try:
                    line = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                stripped = line.strip()
                if not stripped:
                    continue

                if len(line_bytes) > MAX_MCP_PAYLOAD_BYTES:
                    self.stderr_lines.append(
                        f"MCP payload too large: {len(line_bytes)} bytes (limit {MAX_MCP_PAYLOAD_BYTES})"
                    )
                    continue

                # Auto-detect protocol if not determined yet
                if self.protocol is None:
                    if line.lower().startswith("content-length:"):
                        self.protocol = "content-length"
                    else:
                        self.protocol = "newline-json"

                if self.protocol == "newline-json":
                    try:
                        self._handle_message(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
                else:
                    # Content-length protocol
                    # The current 'line' is the first header line
                    header_lines = [line.rstrip("\r\n")]
                    while True:
                        next_line_bytes = self.process.stdout.readline()
                        if not next_line_bytes:
                            return
                        try:
                            next_line = next_line_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            return
                        h_stripped = next_line.rstrip("\r\n")
                        if h_stripped == "":
                            break
                        header_lines.append(h_stripped)

                    content_length = 0
                    for header in header_lines:
                        if header.lower().startswith("content-length:"):
                            try:
                                content_length = int(header.split(":", 1)[1].strip())
                            except ValueError:
                                pass
                            break

                    if content_length > MAX_MCP_PAYLOAD_BYTES:
                        self.stderr_lines.append(
                            f"MCP payload too large: {content_length} bytes (limit {MAX_MCP_PAYLOAD_BYTES})"
                        )
                        continue

                    if content_length > 0:
                        body_bytes = self.process.stdout.read(content_length)
                        if len(body_bytes) < content_length:
                            return
                        try:
                            self._handle_message(json.loads(body_bytes.decode("utf-8")))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
        finally:
            # Bug 2: Notify pending requests when process exits
            if self.process:
                exit_code = self.process.poll()
                error_msg = {"error": {"code": -1, "message": f"MCP server process exited (code={exit_code})"}}
                with self._lock:
                    for req_id, q in list(self._pending.items()):
                        q.put(error_msg)
                    self._pending.clear()

    def _handle_message(self, message: dict[str, Any]) -> None:
        """处理收到的 JSON-RPC 消息，将响应分发给对应的等待队列。

        参数:
            message: JSON-RPC 消息字典，需包含整型 id 字段
        """
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        with self._lock:
            queue = self._pending.pop(message_id, None)
            if queue is not None:
                queue.put(message)

    def send(self, message: dict[str, Any]) -> None:
        """发送 JSON-RPC 消息到 MCP 服务器进程。

        根据当前协议选择编码方式：
        - Newline-JSON：消息 + 换行符
        - Content-Length：HTTP 风格头部 + JSON 消息体

        参数:
            message: 要发送的 JSON-RPC 消息字典

        抛出:
            RuntimeError: 服务器进程未运行或 stdin 不可用时抛出
        """
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f'MCP server "{self.server_name}" is not running.')

        payload_bytes = json.dumps(message, ensure_ascii=False).encode("utf-8")

        if self.protocol == "newline-json":
            self.process.stdin.write(payload_bytes + b"\n")
            self.process.stdin.flush()
            self._ensure_stdout_thread()
            return

        header = f"Content-Length: {len(payload_bytes)}\r\n\r\n".encode()
        self.process.stdin.write(header + payload_bytes)
        self.process.stdin.flush()
        self._ensure_stdout_thread()

    def notify(self, method: str, params: Any) -> None:
        """发送 JSON-RPC 通知（不需要响应的请求）。

        参数:
            method: 方法名
            params: 方法参数
        """
        # self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout_seconds: float = 5.0) -> Any:
        """发送 JSON-RPC 请求并等待响应。

        为请求分配自增 ID，通过队列等待响应。超时或收到错误响应时抛出异常。

        参数:
            method: 方法名
            params: 方法参数
            timeout_seconds: 等待响应的超时时间（秒），默认 5.0

        返回:
            响应的 result 字段

        抛出:
            RuntimeError: 请求超时或服务端返回错误时抛出
        """
        message_id = self.next_id
        self.next_id += 1
        response_queue: Queue[Any] = Queue(maxsize=1)
        with self._lock:
            self._pending[message_id] = response_queue
        self.send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        try:
            message = response_queue.get(timeout=timeout_seconds)
        except Empty as error:
            with self._lock:
                self._pending.pop(message_id, None)
            stderr = "\n".join(self.stderr_lines)
            raise RuntimeError(
                f"MCP {self.server_name}: request timed out for {method}" + (f"\n{stderr}" if stderr else "")
            ) from error
        if message.get("error"):
            details = message["error"].get("data")
            suffix = f"\n{json.dumps(details, indent=2, ensure_ascii=False)}" if details else ""
            raise RuntimeError(f"MCP {self.server_name}: {message['error']['message']}{suffix}")
        return message.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        """列出服务器提供的工具（带缓存，懒启动服务器）。"""
        if self._tools_cache is not None:
            return self._tools_cache
        self._ensure_started()
        result = self.request("tools/list", {})
        self._tools_cache = list(result.get("tools", []) if isinstance(result, dict) else [])
        return self._tools_cache

    def list_resources(self) -> list[dict[str, Any]]:
        """列出服务器提供的资源（带缓存，懒启动服务器）。"""
        if self._resources_cache is not None:
            return self._resources_cache
        self._ensure_started()
        result = self.request("resources/list", {}, timeout_seconds=3.0)
        self._resources_cache = list(result.get("resources", []) if isinstance(result, dict) else [])
        return self._resources_cache

    def read_resource(self, uri: str) -> ToolResult:
        """读取指定 URI 的资源内容（懒启动服务器）。

        参数:
            uri: 资源 URI

        返回:
            格式化的 ToolResult
        """
        # self._ensure_started()
        return _format_read_resource_result(self.request("resources/read", {"uri": uri}, timeout_seconds=5.0))

    def list_prompts(self) -> list[dict[str, Any]]:
        """列出服务器提供的 Prompt（带缓存，懒启动服务器）。"""
        if self._prompts_cache is not None:
            return self._prompts_cache
        self._ensure_started()
        result = self.request("prompts/list", {}, timeout_seconds=3.0)
        self._prompts_cache = list(result.get("prompts", []) if isinstance(result, dict) else [])
        return self._prompts_cache

    def get_prompt(self, name: str, args: dict[str, str] | None = None) -> ToolResult:
        """获取并渲染指定的 Prompt（懒启动服务器）。

        参数:
            name: Prompt 名称
            args: Prompt 参数（可选）

        返回:
            格式化的 ToolResult
        """
        # self._ensure_started()
        return _format_prompt_result(
            self.request("prompts/get", {"name": name, "arguments": args or {}}, timeout_seconds=5.0)
        )

    def call_tool(self, name: str, input_data: Any) -> ToolResult:
        """调用 MCP 服务器上的工具（懒启动服务器）。

        参数:
            name: 工具名称
            input_data: 工具输入参数

        返回:
            格式化的 ToolResult，包含工具执行结果
        """
        # self._ensure_started()
        return _format_tool_call_result(self.request("tools/call", {"name": name, "arguments": input_data or {}}))

    def close(self) -> None:
        """关闭 MCP 服务器连接。

        执行步骤：
        1. 通知所有待处理的请求连接已关闭
        2. 跨平台终止子进程（Windows 用 taskkill，Unix 用 SIGTERM/SIGKILL）
        3. 清理所有状态缓存，重置为未启动状态
        """
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            for queue in pending:
                queue.put({"error": {"message": f'MCP server "{self.server_name}" closed before completing the request.'}})

        if self.process is not None:
            try:
                # 跨平台进程终止
                if os.name == "nt":
                    # Windows: 使用 taskkill 终止进程树
                    try:
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                            capture_output=True,
                            timeout=5
                        )
                    except subprocess.TimeoutExpired:
                        # taskkill 本身超时，强制 kill
                        try:
                            self.process.kill()
                        except OSError:
                            pass
                    except Exception:
                        try:
                            self.process.kill()
                        except OSError:
                            pass
                else:
                    # Unix: 先 SIGTERM，超时后 SIGKILL
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            self.process.kill()
                        except OSError:
                            pass

                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass  # 进程可能已经退出
            finally:
                self.process = None

        self.protocol = None
        self._stdout_thread = None
        self._stderr_thread = None
        # Reset lazy init state
        self._started = False
        self._tools_cache = None
        self._resources_cache = None
        self._prompts_cache = None


def create_mcp_backed_tools(*, cwd: str, mcp_servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """创建基于 MCP 的工具列表，采用懒启动策略初始化服务器。

    不在启动时启动所有 MCP 服务器，而是创建轻量级客户端对象，
    在首次调用工具时再启动对应的服务器进程。

    优点：
    - 启动更快：不等待 MCP 服务器进程初始化
    - 资源占用更低：不需要的服务器永远不会启动
    - 容错：单个服务器失败不影响其他服务器
    - 自动重试：首次使用的失败服务器会在下次自动重试

    参数:
        cwd: 当前工作目录
        mcp_servers: MCP 服务器配置字典，格式为 {server_name: config}

    返回:
        包含 tools（ToolDefinition 列表）、servers（状态摘要列表）
        和 dispose（清理函数）的字典
    """
    clients: list[StdioMcpClient] = []
    tools: list[ToolDefinition] = []
    servers: list[dict[str, Any]] = []
    resource_index: dict[str, dict[str, Any]] = {}
    prompt_index: dict[str, dict[str, Any]] = {}

    for server_name, config in mcp_servers.items():
        if config.get("enabled") is False:
            servers.append(asdict(McpServerSummary(name=server_name, command=config.get("command", ""), status="disabled", toolCount=0, protocol=config.get("protocol"))))
            continue

        client = StdioMcpClient(server_name, config, cwd)
        clients.append(client)

        # Register server with "pending" status — will be connected lazily
        servers.append(
            asdict(
                McpServerSummary(
                    name=server_name,
                    command=config.get("command", ""),
                    status="pending",
                    toolCount=0,
                    protocol=config.get("protocol"),
                )
            )
        )

        # Eagerly discover tools/resources/prompts on first use via
        # the lazy client. Register placeholder tools now that will
        # resolve to the actual MCP tool on first call.
        #
        # We register a single "gateway" tool per server that triggers
        # lazy init, plus we'll discover and register actual tools
        # after the first successful connection.
        #
        # For simplicity, we still try to discover tools at creation
        # time but don't fail if the server can't start yet.
        try:
            descriptors = client.list_tools()
            try:
                resources = client.list_resources()
            except Exception:  # noqa: BLE001
                resources = []
            try:
                prompts = client.list_prompts()
            except Exception:  # noqa: BLE001
                prompts = []

            for resource in resources:
                resource_index[f"{server_name}:{resource.get('uri')}"] = {"serverName": server_name, "resource": resource}
            for prompt in prompts:
                prompt_index[f"{server_name}:{prompt.get('name')}"] = {"serverName": server_name, "prompt": prompt}

            for descriptor in descriptors:
                wrapped_name = f"mcp__{_sanitize_tool_segment(server_name)}__{_sanitize_tool_segment(str(descriptor.get('name', 'tool')))}"
                descriptor_name = str(descriptor.get("name", "tool"))
                input_schema = _normalize_input_schema(descriptor.get("inputSchema"))

                def _validator(value: Any) -> Any:
                    return value

                def _run(input_data: Any, _context, *, _client=client, _descriptor_name=descriptor_name):
                    return _client.call_tool(_descriptor_name, input_data)

                tools.append(
                    ToolDefinition(
                        name=wrapped_name,
                        description=str(descriptor.get("description") or f"Call MCP tool {descriptor_name} from server {server_name}."),
                        input_schema=input_schema,
                        validator=_validator,
                        run=_run,
                    )
                )

            # Update server status to connected
            for i, s in enumerate(servers):
                if s["name"] == server_name:
                    servers[i] = asdict(
                        McpServerSummary(
                            name=server_name,
                            command=config.get("command", ""),
                            status="connected",
                            toolCount=len(descriptors),
                            protocol=client.protocol,
                            resourceCount=len(resources),
                            promptCount=len(prompts),
                        )
                    )
                    break
        except Exception as error:  # noqa: BLE001
            # Lazy init: don't fail — server will be retried on first tool call
            # Just update status to reflect the error
            for i, s in enumerate(servers):
                if s["name"] == server_name:
                    servers[i] = asdict(
                        McpServerSummary(
                            name=server_name,
                            command=config.get("command", ""),
                            status="error",
                            toolCount=0,
                            error=str(error)[:200],
                            protocol=config.get("protocol"),
                        )
                    )
                    break

    if resource_index:
        tools.append(
            ToolDefinition(
                name="list_mcp_resources",
                description="List available MCP resources exposed by connected MCP servers.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}}},
                validator=lambda value: {"server": value.get("server")} if isinstance(value, dict) else {"server": None},
                run=lambda input_data, _context: ToolResult(
                    ok=True,
                    output="\n".join(
                        f"{entry['serverName']}: {entry['resource'].get('uri')}"
                        + (f" ({entry['resource'].get('name')})" if entry["resource"].get("name") else "")
                        + (f" - {entry['resource'].get('description')}" if entry["resource"].get("description") else "")
                        for entry in resource_index.values()
                        if not input_data.get("server") or entry["serverName"] == input_data["server"]
                    )
                    or "No MCP resources available.",
                ),
            )
        )

        def _read_resource(input_data: dict, _context) -> ToolResult:
            client = next((item for item in clients if item.server_name == input_data["server"]), None)
            if client is None:
                return ToolResult(ok=False, output=f"Unknown MCP server: {input_data['server']}")
            return client.read_resource(input_data["uri"])

        tools.append(
            ToolDefinition(
                name="read_mcp_resource",
                description="Read a specific MCP resource by server and URI.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]},
                validator=lambda value: value,
                run=_read_resource,
            )
        )

    if prompt_index:
        tools.append(
            ToolDefinition(
                name="list_mcp_prompts",
                description="List available MCP prompts exposed by connected MCP servers.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}}},
                validator=lambda value: {"server": value.get("server")} if isinstance(value, dict) else {"server": None},
                run=lambda input_data, _context: ToolResult(
                    ok=True,
                    output="\n".join(
                        f"{entry['serverName']}: {entry['prompt'].get('name')}"
                        + (
                            " args=["
                            + ", ".join(
                                f"{arg.get('name')}{'*' if arg.get('required') else ''}"
                                for arg in entry["prompt"].get("arguments", [])
                            )
                            + "]"
                            if entry["prompt"].get("arguments")
                            else ""
                        )
                        + (f" - {entry['prompt'].get('description')}" if entry["prompt"].get("description") else "")
                        for entry in prompt_index.values()
                        if not input_data.get("server") or entry["serverName"] == input_data["server"]
                    )
                    or "No MCP prompts available.",
                ),
            )
        )

        def _get_prompt(input_data: dict, _context) -> ToolResult:
            client = next((item for item in clients if item.server_name == input_data["server"]), None)
            if client is None:
                return ToolResult(ok=False, output=f"Unknown MCP server: {input_data['server']}")
            return client.get_prompt(input_data["name"], input_data.get("arguments"))

        tools.append(
            ToolDefinition(
                name="get_mcp_prompt",
                description="Fetch a rendered MCP prompt by server, prompt name, and optional arguments.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}, "name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["server", "name"]},
                validator=lambda value: value,
                run=_get_prompt,
            )
        )

    return {
        "tools": tools,
        "servers": servers,
        "dispose": lambda: [client.close() for client in clients],
    }
