"""SmartCode Python 的自动模式（Auto Mode）模块。

灵感来自 Claude Code 的 auto mode，位于标准审批模式
和 --dangerously-skip-permissions 之间。提供以下能力：
- 输入层：提示注入检测
- 输出层：转录内容安全分类器
- 安全操作自动审批
- 高风险操作阻断或引导到安全替代方案

权限模式：
- default: 每个操作都询问（当前行为）
- auto: 自动审批安全操作，询问风险操作
- bypass: 跳过所有权限检查（危险！）
- plan: 只读模式，禁止执行操作
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 权限模式
# ---------------------------------------------------------------------------

class PermissionMode(str, Enum):
    """权限模式枚举。

    灵感来自 Claude Code 的权限管理策略，控制工具操作
    的审批方式：全量询问、自动审批、完全绕过或只读计划。
    """
    DEFAULT = "default"           # 每个操作都询问
    AUTO = "auto"                 # 自动审批安全操作
    BYPASS = "bypass"             # 跳过所有权限检查（危险！）
    PLAN = "plan"                 # 只读模式，禁止执行


# ---------------------------------------------------------------------------
# 风险等级
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """操作风险等级枚举。

    用于定义工具操作的危害程度，以决定是自动批准、
    提示用户确认还是直接阻断。
    """
    SAFE = "safe"                 # 自动批准
    LOW = "low"                   # 自动批准并记录日志
    MEDIUM = "medium"             # 提示用户并提供说明
    HIGH = "high"                 # 阻断或要求强理由
    DANGEROUS = "dangerous"       # 始终阻断


# ---------------------------------------------------------------------------
# 风险规则
# ---------------------------------------------------------------------------

# 安全工具（在 auto 模式下自动审批）
SAFE_TOOLS = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "load_skill",
})

# 低风险工具（自动批准但记录日志）
LOW_RISK_TOOLS = {
    "run_command",  # 仅用于只读命令
}

# 中风险工具（需要用户批准）
MEDIUM_RISK_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "patch_file",
    "modify_file",
})

# 高风险命令（阻断或要求强理由）
HIGH_RISK_COMMANDS = {
    # Unix
    "rm -rf",
    "rm -r",
    "git reset --hard",
    "git clean",
    "git push --force",
    "sudo",
    "chmod -R",
    "chown -R",
    # Windows
    "del /s",
    "del /q",
    "rmdir /s",
    "rd /s",
    "icacls",
    "takeown",
    "net user",
    "net localgroup",
    "reg delete",
    "format",
}

# 危险模式（始终阻断）—— 在模块级别预编译以提高性能
_DANGEROUS_PATTERNS_RAW = [
    # Unix
    r"rm\s+-rf\s+/",           # 删除根目录
    r"chmod\s+777",            # 全局可写
    r"curl.*\|\s*sh",          # 管道 curl 到 shell
    r"wget.*\|\s*sh",
    r"mkfs",                   # 格式化文件系统
    r"dd\s+if=",               # 磁盘转储
    # Windows
    r"del\s+/[sfq].*[\\]",     # 递归/强制删除并指定路径
    r"rmdir\s+/s\s+/q",        # 静默递归删除目录
    r"rd\s+/s\s+/q",
    r"format\s+[a-zA-Z]:",     # 格式化驱动器
    r"powershell.*\biex\b",    # PowerShell 远程调用表达式
    r"powershell.*Invoke-Expression",
    r"iwr.*\|\s*iex",          # 下载并执行（PowerShell）
    r"reg\s+delete\s+HKLM",   # 删除计算机范围的注册表项
]
DANGEROUS_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS_RAW]


@dataclass
class RiskAssessment:
    """风险评估结果数据类。

    包含风险等级、工具名称、建议操作、原因说明以及
    可选的安全替代方案，供调用方决策。
    """
    level: RiskLevel
    tool_name: str
    action: str  # "approve"（批准）, "prompt"（提示）, "block"（阻断）
    reason: str
    safe_alternative: str | None = None


# ---------------------------------------------------------------------------
# 自动模式检查器
# ---------------------------------------------------------------------------

class AutoModeChecker:
    """自动模式检查器，判断操作是否可以自动批准。

    灵感来自 Claude Code 的 auto mode，基于权限模式和风险等级
    对每个工具操作进行智能评估，支持输入/输出层的安全检查。
    """

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT):
        """初始化检查器。

        参数:
            mode: 初始权限模式，默认为 DEFAULT
        """
        self.mode = mode

    def set_mode(self, mode: PermissionMode) -> None:
        """更改权限模式。

        参数:
            mode: 目标权限模式
        """
        self.mode = mode

    def assess_risk(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """评估一个工具操作的风险等级。

        根据当前模式进行决策：
        - BYPASS: 批准所有操作
        - PLAN: 仅批准只读工具
        - DEFAULT: 所有操作都需要批准
        - AUTO: 使用智能风险评估

        参数:
            tool_name: 被调用的工具名称
            tool_input: 工具输入参数字典

        返回:
            RiskAssessment，包含推荐动作（批准/提示/阻断）
        """
        # Bypass 模式 —— 批准所有操作
        if self.mode == PermissionMode.BYPASS:
            return RiskAssessment(
                level=RiskLevel.DANGEROUS,
                tool_name=tool_name,
                action="approve",
                reason="Bypass mode: all permissions skipped",
            )

        # Plan 模式 —— 仅允许只读
        if self.mode == PermissionMode.PLAN:
            if tool_name in SAFE_TOOLS:
                return RiskAssessment(
                    level=RiskLevel.SAFE,
                    tool_name=tool_name,
                    action="approve",
                    reason="Plan mode: read-only tool",
                )
            else:
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name=tool_name,
                    action="block",
                    reason="Plan mode: execution not allowed",
                )

        # Default 模式 —— 所有操作都询问
        if self.mode == PermissionMode.DEFAULT:
            return RiskAssessment(
                level=RiskLevel.MEDIUM,
                tool_name=tool_name,
                action="prompt",
                reason="Default mode: approval required",
            )

        # Auto 模式 —— 智能评估
        return self._assess_auto_mode(tool_name, tool_input)

    def _assess_auto_mode(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """在 auto 模式下进行智能风险评估。

        安全工具自动批准，命令类工具检查命令本身的风险，
        文件编辑工具检查目标路径的敏感性，未知工具提示用户。

        参数:
            tool_name: 被调用的工具名称
            tool_input: 工具输入参数字典

        返回:
            RiskAssessment 对象
        """
        # 安全工具 —— 自动批准
        if tool_name in SAFE_TOOLS:
            return RiskAssessment(
                level=RiskLevel.SAFE,
                tool_name=tool_name,
                action="approve",
                reason=f"Auto mode: {tool_name} is read-only",
            )

        # 检查 run_command 是否为只读命令
        if tool_name == "run_command":
            return self._assess_command(tool_input)

        # 文件修改工具
        if tool_name in MEDIUM_RISK_TOOLS:
            return self._assess_file_edit(tool_name, tool_input)

        # 未知工具 —— 提示用户
        return RiskAssessment(
            level=RiskLevel.MEDIUM,
            tool_name=tool_name,
            action="prompt",
            reason=f"Auto mode: unknown tool '{tool_name}'",
        )

    def _assess_command(self, tool_input: dict[str, Any]) -> RiskAssessment:
        """评估 run_command 操作的风险。

        按优先级检查：危险模式 -> 高风险命令 -> 安全的低风险命令。

        参数:
            tool_input: 工具输入，应包含 command 字段

        返回:
            RiskAssessment 对象
        """
        command = tool_input.get("command", "")
        if isinstance(command, list):
            command = " ".join(command)

        # 检查危险模式
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return RiskAssessment(
                    level=RiskLevel.DANGEROUS,
                    tool_name="run_command",
                    action="block",
                    reason=f"Dangerous pattern detected: {pattern}",
                )

        # 检查高风险命令
        for risky_cmd in HIGH_RISK_COMMANDS:
            if risky_cmd in command:
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name="run_command",
                    action="prompt",
                    reason=f"High-risk command: '{risky_cmd}'",
                    safe_alternative=f"Consider safer alternative to '{risky_cmd}'",
                )

        # 低风险 —— 自动批准并记录日志
        return RiskAssessment(
            level=RiskLevel.LOW,
            tool_name="run_command",
            action="approve",
            reason="Auto mode: command appears safe",
        )

    def _assess_file_edit(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """评估文件编辑工具操作的风险。

        检查目标路径是否涉及敏感文件（如 .env、.git、node_modules 等）。
        对非敏感文件的修改仍需要用户批准。

        参数:
            tool_name: 工具名称
            tool_input: 工具输入，应包含 path 字段

        返回:
            RiskAssessment 对象
        """
        path = tool_input.get("path", "")

        # 检查是否正在编辑敏感文件
        # 使用 [/\\] 同时匹配 Unix / 和 Windows \ 路径分隔符
        sensitive_patterns = [
            r"\.env",
            r"\.git[/\\]",
            r"node_modules[/\\]",
            r"__pycache__[/\\]",
            r"\.pyc$",
        ]

        for pattern in sensitive_patterns:
            if re.search(pattern, path):
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name=tool_name,
                    action="prompt",
                    reason=f"Modifying sensitive file: {path}",
                )

        # 普通文件编辑 —— 提示确认
        return RiskAssessment(
            level=RiskLevel.MEDIUM,
            tool_name=tool_name,
            action="prompt",
            reason="Auto mode: file modification requires approval",
        )

    # -----------------------------------------------------------------------
    # 输入/输出层检查（灵感来自 Claude Code）
    # -----------------------------------------------------------------------

    @staticmethod
    def detect_prompt_injection(user_input: str) -> tuple[bool, str]:
        """检测用户输入中潜在的提示注入攻击。

        扫描常见的注入模式，如"忽略先前指令"、"系统提示覆盖"、
        "绕过安全限制"等。

        参数:
            user_input: 用户输入的原始文本

        返回:
            (是否为注入, 匹配到的模式说明) 元组
        """
        injection_patterns = [
            r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|rules|prompts)",
            r"(system|developer)\s*:\s*",
            r"\[?ignore\s+security\]?",
            r"(bypass|skip|override)\s+(permissions|safety|restrictions)",
            r"(execute|run)\s+(this|following)\s+code\s*:",
            r"ignore\s+(all|your)\s+instructions",
        ]

        for pattern in injection_patterns:
            if re.search(pattern, user_input, re.IGNORECASE):
                return True, f"Potential prompt injection: {pattern}"

        return False, ""

    @staticmethod
    def classify_output_safety(output: str) -> tuple[bool, str]:
        """对 AI 输出内容进行安全分类，检测是否包含危险操作。

        检查输出中是否含有危险命令（rm -rf、sudo、DROP TABLE 等）
        或可疑的 SQL 注入模式。

        参数:
            output: AI 输出的原始文本

        返回:
            (是否不安全, 匹配到的危险模式说明) 元组
        """
        unsafe_patterns = [
            # Unix
            r"rm\s+-rf",
            r"sudo\s+",
            r"chmod\s+777",
            # Windows
            r"del\s+/[sfq]",
            r"rmdir\s+/s",
            r"rd\s+/s",
            r"format\s+[a-zA-Z]:",
            # SQL
            r"DROP\s+TABLE",
            r"DELETE\s+FROM.*WHERE\s+1\s*=\s*1",
        ]

        for pattern in unsafe_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return True, f"Unsafe operation detected: {pattern}"

        return False, ""


# ---------------------------------------------------------------------------
# 模式管理
# ---------------------------------------------------------------------------

@dataclass
class ModeState:
    """当前权限模式的状态数据类。

    记录正在使用的模式、模式变更时间与变更者，以及
    自动批准、提示、阻断操作的累计统计数据。
    """
    mode: PermissionMode = PermissionMode.DEFAULT
    mode_changed_at: float = 0.0
    mode_changed_by: str = "user"
    auto_approve_count: int = 0
    prompt_count: int = 0
    block_count: int = 0

    def record_decision(self, action: str) -> None:
        """记录一次权限决策的结果。

        根据 action 参数更新对应的计数器：
        - "approve": 增加自动批准计数
        - "prompt": 增加提示计数
        - "block": 增加阻断计数

        参数:
            action: 决策类型（"approve" / "prompt" / "block"）
        """
        if action == "approve":
            self.auto_approve_count += 1
        elif action == "prompt":
            self.prompt_count += 1
        elif action == "block":
            self.block_count += 1

    def format_status(self) -> str:
        """将当前模式状态格式化为可读的摘要字符串。

        包含当前模式名称、描述以及各决策类型的累计次数
        和自动批准率。

        返回:
            格式化后的状态描述
        """
        mode_descriptions = {
            PermissionMode.DEFAULT: "Ask for every action",
            PermissionMode.AUTO: "Auto-approve safe operations",
            PermissionMode.BYPASS: "Skip all permissions (dangerous!)",
            PermissionMode.PLAN: "Read-only mode",
        }

        lines = [
            "Permission Mode",
            "=" * 50,
            f"Current mode: {self.mode.value}",
            f"Description: {mode_descriptions.get(self.mode, 'Unknown')}",
            "",
            "Statistics:",
            f"  Auto-approved: {self.auto_approve_count}",
            f"  Prompted: {self.prompt_count}",
            f"  Blocked: {self.block_count}",
        ]

        total = self.auto_approve_count + self.prompt_count + self.block_count
        if total > 0:
            auto_pct = self.auto_approve_count / total * 100
            lines.append(f"  Auto-approval rate: {auto_pct:.0f}%")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_checker = AutoModeChecker()
_mode_state = ModeState()


def get_checker() -> AutoModeChecker:
    """获取全局自动模式检查器单例。

    返回:
        AutoModeChecker 实例
    """
    return _checker


def get_mode_state() -> ModeState:
    """获取全局模式状态单例。

    返回:
        ModeState 实例
    """
    return _mode_state


def set_permission_mode(mode: PermissionMode) -> str:
    """设置全局权限模式并返回确认消息。

    同步更新 AutoModeChecker 和 ModeState 中的模式，
    记录变更时间，并返回适合展示给用户的确认文本。

    参数:
        mode: 目标权限模式

    返回:
        描述模式变更结果的字符串
    """
    import time
    _checker.set_mode(mode)
    _mode_state.mode = mode
    _mode_state.mode_changed_at = time.time()

    mode_messages = {
        PermissionMode.DEFAULT: "Default mode: All actions require approval",
        PermissionMode.AUTO: "Auto mode: Safe operations auto-approved",
        PermissionMode.BYPASS: "BYPASS MODE: All permissions skipped!",
        PermissionMode.PLAN: "Plan mode: Read-only operations allowed",
    }

    return mode_messages.get(mode, f"Mode changed to {mode.value}")
