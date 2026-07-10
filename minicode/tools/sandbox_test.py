"""Docker 沙箱测试执行器。

在隔离的 Docker 容器中运行测试，避免污染宿主环境。
支持自动容器生命周期管理、依赖安装、结构化结果输出。

使用前提：宿主机已安装 Docker（Docker Desktop for Windows / Docker Engine on Linux）。
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from minicode.tooling import ToolDefinition, ToolResult

logger = None  # lazy init


def _log():
    global logger
    if logger is None:
        from minicode.logging_config import get_logger
        logger = get_logger("sandbox_test")
    return logger


# ---- Docker 沙箱执行器 ----

class DockerSandbox:
    """Docker 容器沙箱，用于隔离运行测试。"""

    IMAGE = "python:3.11-slim"
    CONTAINER_PREFIX = "mc-test-"

    def __init__(self, cwd: str, changed_files: list[str], test_paths: list[str] | None = None):
        self.cwd = Path(cwd).resolve()
        self.changed_files = [Path(f) if Path(f).is_absolute() else self.cwd / f for f in changed_files]
        self.test_paths = test_paths or []
        self.container_name = f"{self.CONTAINER_PREFIX}{uuid.uuid4().hex[:8]}"
        self._container_id: str | None = None
        self._cleanup_done = False

    # ---- 公共 API ----

    def run(self) -> dict[str, Any]:
        """完整沙箱流程：创建容器 → 安装依赖 → 拷贝文件 → 跑测试 → 清理。

        返回:
            {"passed": bool, "output": str, "duration_ms": int, "error": str|None}
        """
        start = time.time()
        try:
            self._check_docker()
            self._create_container()
            self._install_deps()
            if self.changed_files:
                self._copy_changed_files()
            result = self._run_tests()
            elapsed_ms = int((time.time() - start) * 1000)
            return {
                "passed": result["passed"],
                "output": result["output"],
                "duration_ms": elapsed_ms,
                "error": None,
            }
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            return {
                "passed": False,
                "output": "",
                "duration_ms": elapsed_ms,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            self._cleanup()

    # ---- Docker 操作 ----

    @staticmethod
    def _check_docker() -> None:
        """检查 Docker 是否可用。"""
        try:
            r = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, encoding='utf-8', timeout=10,
            )
            if r.returncode != 0:
                raise RuntimeError(f"Docker 不可用: {r.stderr.strip()}")
        except FileNotFoundError:
            raise RuntimeError("未找到 docker 命令，请安装 Docker Desktop")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Docker 响应超时")

    def _create_container(self) -> None:
        """创建并启动容器。

        项目目录以只读挂载到 /workspace（供 pip install 和参考），
        变更文件拷贝到容器内可写的 /sandbox 目录中运行测试。
        """
        _log().info("创建 Docker 容器: %s", self.container_name)

        host_path = str(self.cwd)

        # 处理 Windows 路径（Docker Desktop 需要 Linux 风格路径）
        if sys.platform == "win32":
            import re
            m = re.match(r"^([A-Za-z]):\\", host_path)
            if m:
                drive = m.group(1).lower()
                rest = host_path[3:].replace("\\", "/")
                host_path = f"/{drive}/{rest}"

        r = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", self.container_name,
                "--rm",
                "-v", f"{host_path}:/workspace:ro",  # 只读挂载项目
                "-w", "/sandbox",                      # 工作目录是可写的 sandbox
                self.IMAGE,
                "sleep", "300",
            ],
            capture_output=True, encoding='utf-8', timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError(f"创建容器失败: {r.stderr.strip()}")

        self._container_id = r.stdout.strip()

        # 创建可写 sandbox 目录
        self._exec(["mkdir", "-p", "/sandbox"], timeout=10)

    def _exec(self, cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        """在容器中执行命令。

        用 encoding='utf-8' 而非 text=True 避免 Windows GBK 解码报错。
        容器内输出始终为 UTF-8，但 text=True 会尝试用 GBK 解码。
        """
        docker_cmd = ["docker", "exec", "-i",
                      "-e", "PYTHONIOENCODING=utf-8",
                      self.container_name] + cmd
        return subprocess.run(
            docker_cmd, capture_output=True, encoding="utf-8", timeout=timeout,
        )

    def _install_deps(self) -> None:
        """在容器内安装项目依赖。

        先把项目从只读挂载拷贝到可写的 /sandbox。
        如果有 pyproject.toml/setup.py 则 pip install -e，否则只装 pytest。
        """
        _log().info("拷贝项目到 sandbox...")
        self._exec(["cp", "-r", "/workspace/.", "/sandbox/"], timeout=60)

        # 检查是否有项目文件
        has_project = False
        for chk in ["/sandbox/pyproject.toml", "/sandbox/setup.py", "/sandbox/setup.cfg"]:
            r = self._exec(["test", "-f", chk], timeout=5)
            if r.returncode == 0:
                has_project = True
                break

        if has_project:
            _log().info("安装项目依赖...")
            self._exec(["pip", "install", "-e", "/sandbox"], timeout=180)
        else:
            _log().info("无项目文件，跳过依赖安装")

        # 确保 pytest 可用
        self._exec(["pip", "install", "pytest"], timeout=60)

    def _copy_changed_files(self) -> None:
        """将变更文件拷贝到容器内的 /sandbox 目录（可写）。"""
        for src_path in self.changed_files:
            if not src_path.exists():
                _log().warning("文件不存在，跳过: %s", src_path)
                continue
            rel_path = src_path.relative_to(self.cwd).as_posix()
            container_dest = f"/sandbox/{rel_path}"
            # 确保目标目录存在
            parent_dir = str(Path(container_dest).parent)
            self._exec(["mkdir", "-p", parent_dir], timeout=10)
            # 拷贝文件到容器
            r = subprocess.run(
                ["docker", "cp", str(src_path), f"{self.container_name}:{container_dest}"],
                capture_output=True, encoding='utf-8', timeout=30,
            )
            if r.returncode != 0:
                _log().warning("拷贝文件失败 %s: %s", rel_path, r.stderr[:200])
            else:
                _log().info("已拷贝: %s", rel_path)

    def _run_tests(self) -> dict:
        """运行 pytest 并返回结果。

        返回:
            {"passed": bool, "output": str}
        """
        # 确定测试路径：如果没指定，用 changed_files 的目录
        if self.test_paths:
            test_args = " ".join(self.test_paths)
        elif self.changed_files:
            # 找每个变更文件对应的 test_* 文件
            test_dirs = set()
            for f in self.changed_files:
                rel = f.relative_to(self.cwd)
                # 尝试找 test_{stem}.py 或 tests/test_{stem}.py
                stem = rel.stem
                candidates = [
                    f"/sandbox/tests/test_{stem}.py",
                    f"/sandbox/test_{stem}.py",
                    f"/sandbox/{rel.parent}/test_{stem}.py",
                ]
                for c in candidates:
                    # docker exec ls 检查文件是否存在
                    check = self._exec(["ls", c], timeout=5)
                    if check.returncode == 0:
                        test_dirs.add(str(Path(c).parent))
            test_args = " ".join(test_dirs) if test_dirs else "/sandbox/tests"
        else:
            test_args = "/sandbox/tests"

        _log().info("运行测试: %s", test_args)

        try:
            r = self._exec(
                ["python", "-m", "pytest", test_args, "-x", "--tb=short", "-q"],
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return {"passed": False, "output": "Test execution timed out (300s)"}

        output = r.stdout + "\n" + r.stderr
        if r.returncode == 0:
            return {"passed": True, "output": output[:4000]}
        else:
            return {"passed": False, "output": output[:4000]}

    def _cleanup(self) -> None:
        """停止并删除容器。"""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        if self._container_id:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container_name],
                    capture_output=True, encoding='utf-8', timeout=15,
                )
                _log().info("容器已清理: %s", self.container_name)
            except Exception:
                pass


# ---- 工具函数 ----

def _validate(input_data: dict) -> dict:
    """验证输入参数。"""
    changed_files = input_data.get("changed_files", [])
    if not isinstance(changed_files, list):
        raise ValueError("changed_files 必须是列表")
    for f in changed_files:
        if not isinstance(f, str):
            raise ValueError("changed_files 中的每个元素必须是字符串路径")
    return {
        "changed_files": changed_files,
        "test_paths": input_data.get("test_paths", []),
    }


def _run(input_data: dict, context) -> ToolResult:
    """在 Docker 沙箱中运行测试。

    参数:
        input_data:
            changed_files: 变更文件路径列表（必填）
            test_paths: 测试路径列表（可选，默认自动推断）

    返回:
        [SANDBOX_RESULT: PASS] 或 [SANDBOX_RESULT: FAIL] 格式的结构化输出
    """
    cwd = context.cwd
    changed_files = input_data["changed_files"]
    test_paths = input_data.get("test_paths", [])

    sandbox = DockerSandbox(cwd, changed_files, test_paths)
    result = sandbox.run()

    if result["error"]:
        output = (
            f"[SANDBOX_RESULT: ERROR]\n"
            f"error: {result['error']}\n"
            f"duration_ms: {result['duration_ms']}\n"
        )
        return ToolResult(ok=False, output=output)

    if result["passed"]:
        output = (
            f"[SANDBOX_RESULT: PASS]\n"
            f"duration_ms: {result['duration_ms']}\n\n"
            f"{result['output'][:2000]}"
        )
        return ToolResult(ok=True, output=output)
    else:
        output = (
            f"[SANDBOX_RESULT: FAIL]\n"
            f"duration_ms: {result['duration_ms']}\n\n"
            f"{result['output'][:4000]}"
        )
        return ToolResult(ok=False, output=output)


sandbox_test_tool = ToolDefinition(
    name="sandbox_test",
    description=(
        "Run tests in an isolated Docker sandbox container. "
        "Creates a disposable container, copies changed files, runs pytest, "
        "then destroys the container. Returns PASS or FAIL with test output. "
        "Safe for testing risky code changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "changed_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of changed file paths (relative to project root)",
            },
            "test_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: specific test paths to run (default: auto-detect)",
            },
        },
        "required": ["changed_files"],
    },
    validator=_validate,
    run=_run,
)
