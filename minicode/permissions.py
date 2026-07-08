"""SmartCode 权限管理模块。

提供路径访问、命令执行和文件编辑的权限检查与控制功能。
支持交互式提示和自动模式两种权限决策方式，并可持久化用户偏好设置。
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal

from minicode.config import MINI_CODE_PERMISSIONS_PATH

# Auto mode integration
from minicode.auto_mode import AutoModeChecker, PermissionMode, get_mode_state
from minicode.logging_config import log_permission_check

# 权限决策类型 — 对齐 TS 版 PermissionDecision
PermissionDecision = Literal[
    "allow_once",
    "allow_always",
    "allow_turn",
    "allow_all_turn",
    "deny_once",
    "deny_always",
    "deny_with_feedback",
]

PromptHandler = Callable[[dict[str, Any]], dict[str, Any]]


# ---------------------------------------------------------------------------
# Path normalization with LRU cache
# ---------------------------------------------------------------------------

# LRU cache for _normalize_path — this is called on every permission check
# and Path.resolve() is expensive (stat syscall per path component).
# Typical session: hundreds of checks on ~50 unique paths.
_CACHE_MAX_SIZE = 512

_normalize_path_cached = lru_cache(maxsize=_CACHE_MAX_SIZE)(
    lambda p: str(Path(p).resolve())
)


def _normalize_path(target_path: str) -> str:
    """标准化路径并缓存结果。

    解析符号链接并统一路径分隔符格式。使用 LRU 缓存避免重复的 Path.resolve()
    系统调用，因为同一路径（如工作区根目录）会在每次工具调用时反复检查。

    参数:
        target_path: 待标准化的原始路径字符串。

    返回:
        标准化后的绝对路径字符串。
    """
    return _normalize_path_cached(target_path)


# Pre-computed result for the workspace root check (most common case)
# This avoids calling _is_within_directory for the trivial case.
_is_win = sys.platform == "win32"


def _is_within_directory(root: str, target: str) -> bool:
    """检查目标路径是否在根目录范围内。

    在 Windows 上使用不区分大小写的比较（NTFS 默认不区分大小写）。
    两个路径都应预先标准化（已解析符号链接）以确保比较正确。

    参数:
        root: 根目录路径（已标准化）。
        target: 目标路径（已标准化）。

    返回:
        如果目标路径在根目录内返回 True，否则返回 False。
    """
    if _is_win:
        # Windows: case-insensitive path comparison
        target_str = target.lower()
        root_str = root.lower().rstrip("\\/")
        return (
            target_str == root_str
            or target_str.startswith(root_str + "\\")
            or target_str.startswith(root_str + "/")
        )
    
    # Unix: direct string comparison (paths already normalized)
    root_str = root.rstrip(os.sep)
    return target == root_str or target.startswith(root_str + os.sep)


def _matches_directory_prefix(target_path: str, directories: set[str]) -> bool:
    """检查目标路径是否匹配任一目录前缀。

    优化策略：按长度排序目录（最具体的优先），在首次匹配时短路返回。

    参数:
        target_path: 待检查的目标路径（已标准化）。
        directories: 目录前缀集合。

    返回:
        如果目标路径位于任一目录内返回 True，否则返回 False。
    """
    for directory in directories:
        if _is_within_directory(directory, target_path):
            return True
    return False


def _format_command_signature(command: str, args: list[str]) -> str:
    """格式化命令签名字符串。

    将命令名称和参数列表拼接成一个完整的签名字符串，用于唯一标识一个命令调用。

    参数:
        command: 命令名称（如 "git"、"rm"）。
        args: 命令参数列表。

    返回:
        拼接后的命令签名字符串。
    """
    return " ".join([command, *args]).strip()


def _classify_dangerous_command(command: str, args: list[str]) -> str | None:
    """对命令进行危险性分类。

    检查命令是否属于预定义的危险操作类别（如强制 Git 推送、递归删除、
    磁盘格式化、权限全开等），并返回对应的风险描述。

    参数:
        command: 命令名称。
        args: 命令参数列表。

    返回:
        如果命令被识别为危险操作，返回风险描述字符串；否则返回 None。
    """
    normalized_args = [arg.strip() for arg in args if arg.strip()]
    signature = _format_command_signature(command, normalized_args)

    if command == "git":
        if "reset" in normalized_args and "--hard" in normalized_args:
            return f"git reset --hard can discard local changes ({signature})"
        if "clean" in normalized_args:
            return f"git clean can delete untracked files ({signature})"
        if "checkout" in normalized_args and "--" in normalized_args:
            return f"git checkout -- can overwrite working tree files ({signature})"
        if "push" in normalized_args and any(arg in {"--force", "-f"} for arg in normalized_args):
            return f"git push --force rewrites remote history ({signature})"
        if "restore" in normalized_args and any(arg.startswith("--source") for arg in normalized_args):
            return f"git restore --source can overwrite local files ({signature})"

    if command == "npm" and "publish" in normalized_args:
        return f"npm publish affects a registry outside this machine ({signature})"

    # 灾难性删除命令检测
    if command == "rm":
        # 组合所有标志（支持 -rf, -fr, -Rf, -r -f 等）
        combined_flags = "".join(arg for arg in normalized_args if arg.startswith("-")).lower()
        # 检查是否同时有递归和强制标志
        if "r" in combined_flags and "f" in combined_flags:
            # 检查是否针对根目录或使用 --no-preserve-root
            if any(arg in {"/", "/*"} for arg in normalized_args) or "--no-preserve-root" in normalized_args:
                return f"rm -rf can cause catastrophic data loss ({signature})"
            # 即使不是根目录，rm -rf 也是危险的
            return f"rm -rf can cause catastrophic data loss ({signature})"

    # 磁盘写入/格式化命令检测
    if command in {"dd", "mkfs", "mkfs.ext4", "mkfs.vfat", "fdisk", "format"}:
        return f"{command} can modify or destroy disk partitions ({signature})"

    # 权限全开命令检测
    if command == "chmod":
        if "777" in normalized_args or any(arg.endswith("777") for arg in normalized_args):
            return f"chmod 777 opens permissions to all users ({signature})"

    if command in {
        "node", "python", "python3", "pythonw",
        "bun", "bash", "sh", "zsh", "fish",
        "powershell", "pwsh",
    }:
        return f"{command} can execute arbitrary local code ({signature})"

    # macOS-specific dangerous commands
    if command == "diskutil":
        return f"diskutil can erase or partition disks ({signature})"
    if command == "csrutil":
        return f"csrutil modifies System Integrity Protection ({signature})"
    if command == "defaults" and "write" in normalized_args:
        return f"defaults write modifies system preferences ({signature})"
    if command == "launchctl" and any(arg in {"unload", "bootout", "disable"} for arg in normalized_args):
        return f"launchctl can disable system services ({signature})"
    if command == "dscl":
        return f"dscl can modify directory services and user accounts ({signature})"

    return None


def _read_permission_store() -> dict[str, Any]:
    """从文件读取权限存储。

    从持久化文件中加载用户之前保存的权限决策（如允许/拒绝的目录、命令等）。
    如果文件不存在、损坏或解析失败，返回空字典并记录警告。

    返回:
        包含权限配置的字典，结构为 {str: list}。文件损坏时返回空字典。
    """
    if not MINI_CODE_PERMISSIONS_PATH.exists():
        return {}
    try:
        data = json.loads(MINI_CODE_PERMISSIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        # 损坏的文件 — 返回空存储并记录警告
        import warnings
        warnings.warn(f"Corrupted permissions file, resetting: {e}")
        return {}


def _write_permission_store(store: dict[str, Any]) -> None:
    """将权限存储原子写入文件以防止竞争条件。

    先写入临时文件，然后通过原子替换操作覆盖目标文件，确保在写入过程中
    发生崩溃时不会损坏已有数据。

    参数:
        store: 包含权限配置的字典，结构为 {str: list}。
    """
    import tempfile
    
    MINI_CODE_PERMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # 写入临时文件
    fd, tmp_path = tempfile.mkstemp(
        dir=MINI_CODE_PERMISSIONS_PATH.parent,
        suffix=".tmp"
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(store, f, indent=2)
            f.write('\n')
        # 原子替换
        os.replace(tmp_path, MINI_CODE_PERMISSIONS_PATH)
    except Exception:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PermissionManager:
    """权限管理器，负责控制路径访问、命令执行和文件编辑的权限。

    维护多组允许/拒绝列表（会话级和持久化），结合交互式提示和自动模式
    来决策权限。支持跨轮次的权限持久化和按轮次的临时权限。

    属性:
        workspace_root: 工作区根目录路径（已标准化）。
        prompt: 交互式提示回调函数，用户未提供时为 None。
        auto_checker: 自动模式检查器。
        allowed_directory_prefixes: 持久化允许的目录前缀集合。
        denied_directory_prefixes: 持久化拒绝的目录前缀集合。
        session_allowed_paths: 当前会话允许的路径集合。
        session_denied_paths: 当前会话拒绝的路径集合。
        allowed_command_patterns: 持久化允许的命令模式集合。
        denied_command_patterns: 持久化拒绝的命令模式集合。
        session_allowed_commands: 当前会话允许的命令集合。
        session_denied_commands: 当前会话拒绝的命令集合。
        allowed_edit_patterns: 持久化允许的编辑目标集合。
        denied_edit_patterns: 持久化拒绝的编辑目标集合。
        session_allowed_edits: 当前会话允许的编辑集合。
        session_denied_edits: 当前会话拒绝的编辑集合。
        turn_allowed_edits: 当前轮次允许的编辑集合。
        turn_allow_all_edits: 当前轮次是否允许所有编辑。
    """
    def __init__(self, workspace_root: str, prompt: PromptHandler | None = None, auto_mode: PermissionMode | None = None) -> None:
        """初始化权限管理器。

        设置工作区根目录，初始化自动模式检查器，以及所有允许/拒绝集合。
        并从持久化存储中加载已有的权限配置。

        参数:
            workspace_root: 工作区根目录路径。
            prompt: 可选的交互式提示回调函数，用于向用户请求权限批准。
            auto_mode: 可选的自动模式，控制是否需要提示用户。
        """
        self.workspace_root = _normalize_path(workspace_root)
        self.prompt = prompt
        self.auto_checker = AutoModeChecker(mode=auto_mode or PermissionMode.DEFAULT)
        self.allowed_directory_prefixes: set[str] = set()
        self.denied_directory_prefixes: set[str] = set()
        self.session_allowed_paths: set[str] = set()
        self.session_denied_paths: set[str] = set()
        self.allowed_command_patterns: set[str] = set()
        self.denied_command_patterns: set[str] = set()
        self.session_allowed_commands: set[str] = set()
        self.session_denied_commands: set[str] = set()
        self.allowed_edit_patterns: set[str] = set()
        self.denied_edit_patterns: set[str] = set()
        self.session_allowed_edits: set[str] = set()
        self.session_denied_edits: set[str] = set()
        self.turn_allowed_edits: set[str] = set()
        self.turn_allow_all_edits = False
        self._initialize()

    def _initialize(self) -> None:
        """从持久化存储加载已有的权限配置。

        读取存储中的允许/拒绝目录前缀、命令模式和编辑模式，并填充到
        对应的集合中。在权限管理器初始化时自动调用。
        """
        store = _read_permission_store()
        self.allowed_directory_prefixes |= {_normalize_path(item) for item in store.get("allowedDirectoryPrefixes", [])}
        self.denied_directory_prefixes |= {_normalize_path(item) for item in store.get("deniedDirectoryPrefixes", [])}
        self.allowed_command_patterns |= set(store.get("allowedCommandPatterns", []))
        self.denied_command_patterns |= set(store.get("deniedCommandPatterns", []))
        self.allowed_edit_patterns |= {_normalize_path(item) for item in store.get("allowedEditPatterns", [])}
        self.denied_edit_patterns |= {_normalize_path(item) for item in store.get("deniedEditPatterns", [])}

    def begin_turn(self) -> None:
        """开始一个新的轮次。

        清空当前轮次的编辑允许状态（turn_allowed_edits 和 turn_allow_all_edits），
        为新一轮的权限检查做好准备。每轮开始时调用。
        """
        self.turn_allowed_edits.clear()
        self.turn_allow_all_edits = False

    def end_turn(self) -> None:
        """结束当前轮次。

        委托给 begin_turn() 清理轮次级别的临时权限状态。
        """
        self.begin_turn()

    def get_summary(self) -> list[str]:
        """获取权限状态的摘要信息。

        返回当前工作区根目录、额外允许的目录、危险命令白名单和受信任的
        编辑目标等信息的文本摘要列表，用于状态展示。

        返回:
            包含摘要行的字符串列表。
        """
        summary = [f"cwd: {self.workspace_root}"]
        summary.append(
            "extra allowed dirs: "
            + (", ".join(sorted(self.allowed_directory_prefixes)[:4]) if self.allowed_directory_prefixes else "none")
        )
        summary.append(
            "dangerous allowlist: "
            + (", ".join(sorted(self.allowed_command_patterns)[:4]) if self.allowed_command_patterns else "none")
        )
        if self.allowed_edit_patterns:
            summary.append("trusted edit targets: " + ", ".join(sorted(self.allowed_edit_patterns)[:2]))
        return summary

    def _persist(self) -> None:
        """将当前权限配置持久化到文件。

        将允许/拒绝的目录前缀、命令模式和编辑模式写入持久化存储文件，
        以便在后续会话中继续生效。
        """
        _write_permission_store(
            {
                "allowedDirectoryPrefixes": sorted(self.allowed_directory_prefixes),
                "deniedDirectoryPrefixes": sorted(self.denied_directory_prefixes),
                "allowedCommandPatterns": sorted(self.allowed_command_patterns),
                "deniedCommandPatterns": sorted(self.denied_command_patterns),
                "allowedEditPatterns": sorted(self.allowed_edit_patterns),
                "deniedEditPatterns": sorted(self.denied_edit_patterns),
            }
        )

    def ensure_path_access(self, target_path: str, intent: str) -> None:
        """确保对指定路径的访问权限。

        检查目标路径是否在工作区内、是否在拒绝/允许列表中，并基于
        自动模式或用户交互式提示做出权限决策。路径超出工作区时需要用户批准。

        参数:
            target_path: 需要访问的目标路径。
            intent: 访问意图（如 "read"、"write"、"list"、"command_cwd"）。

        抛出:
            RuntimeError: 如果路径访问被拒绝。
        """
        normalized_target = _normalize_path(target_path)
        
        # Fast path: check workspace root first (most common case)
        # workspace_root is already normalized, so no need for Path.resolve() again
        if _is_within_directory(self.workspace_root, normalized_target):
            return
        
        # Check denial sets first (fail fast)
        if normalized_target in self.session_denied_paths or _matches_directory_prefix(normalized_target, self.denied_directory_prefixes):
            raise RuntimeError(f"Access denied for path outside cwd: {normalized_target}")
        
        # Check approval sets
        if normalized_target in self.session_allowed_paths or _matches_directory_prefix(normalized_target, self.allowed_directory_prefixes):
            return
        if normalized_target in self.session_denied_paths or _matches_directory_prefix(normalized_target, self.denied_directory_prefixes):
            raise RuntimeError(f"Access denied for path outside cwd: {normalized_target}")
        if normalized_target in self.session_allowed_paths or _matches_directory_prefix(normalized_target, self.allowed_directory_prefixes):
            return
        
        # Auto mode risk assessment for path access
        assessment = self.auto_checker.assess_risk("path_access", {"path": normalized_target, "intent": intent})
        if assessment.action == "approve":
            get_mode_state().record_decision("approve")
            self.session_allowed_paths.add(normalized_target)
            log_permission_check("path_access", normalized_target, granted=True)
            return

        if self.prompt is None:
            log_permission_check("path_access", normalized_target, granted=False)
            raise RuntimeError(
                f"Path {normalized_target} is outside cwd {self.workspace_root}. Start minicode in TTY mode to approve it."
            )

        scope_directory = normalized_target if intent in {"list", "command_cwd"} else str(Path(normalized_target).parent)
        result = self.prompt(
            {
                "kind": "path",
                "summary": f"mini-code wants {intent.replace('_', ' ')} access outside the current cwd",
                "details": [
                    f"cwd: {self.workspace_root}",
                    f"target: {normalized_target}",
                    f"scope directory: {scope_directory}",
                ],
                "scope": scope_directory,
                "choices": [
                    {"key": "y", "label": "allow once", "decision": "allow_once"},
                    {"key": "a", "label": "allow this directory", "decision": "allow_always"},
                    {"key": "n", "label": "deny once", "decision": "deny_once"},
                    {"key": "d", "label": "deny this directory", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_paths.add(normalized_target)
            return
        if decision == "allow_always":
            self.allowed_directory_prefixes.add(scope_directory)
            self._persist()
            return
        if decision == "deny_always":
            self.denied_directory_prefixes.add(scope_directory)
            self._persist()
        else:
            self.session_denied_paths.add(normalized_target)
        raise RuntimeError(f"Access denied for path outside cwd: {normalized_target}")

    def ensure_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        force_prompt_reason: str | None = None,
    ) -> None:
        """确保命令执行的权限。

        首先检查命令工作目录的路径权限，然后判断命令是否属于危险操作。
        危险命令需要用户交互式批准或自动模式决策。非危险命令也需通过
        自动模式的风险评估。

        参数:
            command: 命令名称。
            args: 命令参数列表。
            command_cwd: 命令执行的工作目录。
            force_prompt_reason: 若提供，则强制提示用户批准并附带此原因。

        抛出:
            RuntimeError: 如果命令被拒绝执行。
        """
        self.ensure_path_access(command_cwd, "command_cwd")
        reason = force_prompt_reason or _classify_dangerous_command(command, args)
        if not reason:
            # Not classified as dangerous — check auto mode for auto-approve
            _sig = _format_command_signature(command, args)
            assessment = self.auto_checker.assess_risk("run_command", {"command": [command] + args})
            if assessment.action == "approve":
                get_mode_state().record_decision("approve")
                log_permission_check("run_command", _sig, granted=True)
                return
            if assessment.action == "block":
                get_mode_state().record_decision("block")
                log_permission_check("run_command", _sig, granted=False)
                raise RuntimeError(f"Command blocked by auto mode: {assessment.reason}")
            # action == "prompt" — fall through to normal approval flow
            return
        signature = _format_command_signature(command, args)
        if signature in self.session_denied_commands or signature in self.denied_command_patterns:
            raise RuntimeError(f"Command denied: {signature}")
        if signature in self.session_allowed_commands or signature in self.allowed_command_patterns:
            return
        
        # Auto mode risk assessment for dangerous commands
        assessment = self.auto_checker.assess_risk("run_command", {"command": [command] + args})
        if assessment.action == "approve":
            get_mode_state().record_decision("approve")
            self.session_allowed_commands.add(signature)
            log_permission_check("run_command", signature, granted=True)
            return
        if assessment.action == "block":
            get_mode_state().record_decision("block")
            log_permission_check("run_command", signature, granted=False)
            raise RuntimeError(f"Command blocked by auto mode: {assessment.reason}")

        if self.prompt is None:
            log_permission_check("run_command", signature, granted=False)
            raise RuntimeError(
                f"Non-interactive mode: command '{command}' blocked.\n"
                f"  - To write files: use write_file tool instead.\n"
                f"  - To edit files: use edit_file tool instead.\n"
                f"  - To run tests: use test_runner tool instead."
            )
        # Distinguish forced prompts (external trigger) from dangerous commands
        summary = (
            "mini-code wants to run a dangerous command"
            if not force_prompt_reason
            else "mini-code wants approval for this command"
        )
        result = self.prompt(
            {
                "kind": "command",
                "summary": summary,
                "details": [f"cwd: {command_cwd}", f"command: {signature}", f"reason: {reason}"],
                "scope": signature,
                "choices": [
                    {"key": "y", "label": "allow once", "decision": "allow_once"},
                    {"key": "a", "label": "always allow this command", "decision": "allow_always"},
                    {"key": "n", "label": "deny once", "decision": "deny_once"},
                    {"key": "d", "label": "always deny this command", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_commands.add(signature)
            return
        if decision == "allow_always":
            self.allowed_command_patterns.add(signature)
            self._persist()
            return
        if decision == "deny_always":
            self.denied_command_patterns.add(signature)
            self._persist()
        else:
            self.session_denied_commands.add(signature)
        raise RuntimeError(f"Command denied: {signature}")

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        """确保文件编辑操作的权限。

        检查目标文件是否在拒绝/允许列表中，并基于自动模式或用户交互式
        提示做出权限决策。支持多种编辑授权方式（单次、本轮次、永久）。

        参数:
            target_path: 待编辑文件的路径。
            diff_preview: 差异预览字符串，展示即将应用的更改内容。

        抛出:
            RuntimeError: 如果编辑被拒绝，或在拒绝时包含用户反馈信息。
        """
        normalized_target = _normalize_path(target_path)
        if (
            normalized_target in self.session_denied_edits
            or normalized_target in self.denied_edit_patterns
        ):
            raise RuntimeError(f"Edit denied: {normalized_target}")
        if (
            normalized_target in self.session_allowed_edits
            or normalized_target in self.turn_allowed_edits
            or self.turn_allow_all_edits
            or normalized_target in self.allowed_edit_patterns
        ):
            return
        
        # Auto mode risk assessment for file edits
        assessment = self.auto_checker.assess_risk("edit_file", {"path": normalized_target})
        if assessment.action == "approve":
            get_mode_state().record_decision("approve")
            self.session_allowed_edits.add(normalized_target)
            log_permission_check("edit_file", normalized_target, granted=True)
            return
        if assessment.action == "block":
            get_mode_state().record_decision("block")
            log_permission_check("edit_file", normalized_target, granted=False)
            raise RuntimeError(f"Edit blocked by auto mode: {assessment.reason}")

        if self.prompt is None:
            # Non-interactive mode: auto-allow edits within workspace
            if normalized_target.startswith(self.workspace_root):
                self.session_allowed_edits.add(normalized_target)
                log_permission_check("edit_file", normalized_target, granted=True)
                return
            log_permission_check("edit_file", normalized_target, granted=False)
            raise RuntimeError(
                f"Non-interactive mode: edit to '{normalized_target}' blocked (outside workspace)."
            )
        result = self.prompt(
            {
                "kind": "edit",
                "summary": "mini-code wants to apply a file modification",
                "details": [f"target: {normalized_target}", "", diff_preview],
                "scope": normalized_target,
                "choices": [
                    {"key": "1", "label": "apply once", "decision": "allow_once"},
                    {"key": "2", "label": "allow this file in this turn", "decision": "allow_turn"},
                    {"key": "3", "label": "allow all edits in this turn", "decision": "allow_all_turn"},
                    {"key": "4", "label": "always allow this file", "decision": "allow_always"},
                    {"key": "5", "label": "reject once", "decision": "deny_once"},
                    {"key": "6", "label": "reject and send guidance to model", "decision": "deny_with_feedback"},
                    {"key": "7", "label": "always reject this file", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_edits.add(normalized_target)
            return
        if decision == "allow_turn":
            self.turn_allowed_edits.add(normalized_target)
            return
        if decision == "allow_all_turn":
            self.turn_allow_all_edits = True
            return
        if decision == "allow_always":
            self.allowed_edit_patterns.add(normalized_target)
            self._persist()
            return
        if decision == "deny_with_feedback":
            guidance = str(result.get("feedback", "")).strip()
            if guidance:
                raise RuntimeError(f"Edit denied: {normalized_target}\nUser guidance: {guidance}")
        if decision == "deny_always":
            self.denied_edit_patterns.add(normalized_target)
            self._persist()
        else:
            self.session_denied_edits.add(normalized_target)
        raise RuntimeError(f"Edit denied: {normalized_target}")


class PermissionGate:
    """关键操作的显式权限门。

    提供声明式权限检查方式，用于在执行高风险操作（文件写入、命令执行、
    文件编辑等）前进行权限验证。

    用法示例:
        gate = PermissionGate(permissions, cwd)
        gate.check_file_write("src/main.py")
        gate.check_command_run("rm -rf /tmp")
    """
    def __init__(
        self,
        permissions: PermissionManager,
        cwd: str,
    ) -> None:
        """初始化权限门。

        参数:
            permissions: 权限管理器实例，用于执行实际的权限检查。
            cwd: 当前工作目录路径，作为命令执行的默认目录。
        """
        self.permissions = permissions
        self.cwd = cwd

    def check_path_access(self, target_path: str, intent: str) -> None:
        """检查路径访问权限（读/写/列表/搜索）。

        参数:
            target_path: 需要访问的目标路径。
            intent: 访问意图描述。

        抛出:
            RuntimeError: 如果路径访问被拒绝。
        """
        self.permissions.ensure_path_access(target_path, intent)

    def check_file_write(self, target_path: str) -> None:
        """检查文件写入操作的权限。

        路径访问意图固定为 "write"，委托给 check_path_access 实现。

        参数:
            target_path: 待写入的目标文件路径。

        抛出:
            RuntimeError: 如果文件写入被拒绝。
        """
        self.check_path_access(target_path, "write")

    def check_command_run(self, command: str, args: list[str]) -> None:
        """检查命令执行的权限。

        使用当前工作目录作为命令执行目录，委托给权限管理器的 ensure_command 方法。

        参数:
            command: 命令名称。
            args: 命令参数列表。

        抛出:
            RuntimeError: 如果命令执行被拒绝。
        """
        self.permissions.ensure_command(command, args, self.cwd)

    def check_file_edit(self, target_path: str, diff_preview: str) -> None:
        """检查文件编辑操作的权限（带差异预览）。

        将差异预览信息传递给权限管理器，以便在提示用户时展示具体的更改内容。

        参数:
            target_path: 待编辑文件的路径。
            diff_preview: 差异预览字符串，展示即将应用的更改内容。

        抛出:
            RuntimeError: 如果文件编辑被拒绝。
        """
        self.permissions.ensure_edit(target_path, diff_preview)
