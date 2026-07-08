"""SmartCode 核心类型定义。

本模块定义了 SmartCode 系统中使用的所有核心数据类型，
包括聊天消息、工具调用、智能体步骤、运行时事件以及模型适配器协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypedDict


class ChatMessage(TypedDict, total=False):
    """Agent 循环中传递的最小消息单元，表示一次对话中的单条消息。

    【在流程中的位置】消息由用户输入、模型适配器返回、工具结果等不同来源创建，
    以列表形式在整个对话上下文中传递，是 ModelAdapter.next() 方法的输入和
    agent 循环各阶段的处理对象。agent 循环的每一步都会追加或更新此类型消息。

    字段说明:
        role: 消息角色
              - "system": 系统提示词，设定模型行为
              - "user": 用户输入或系统生成的指令
              - "assistant": 模型的完整回复（最终答案）
              - "assistant_progress": 模型的中间进度更新
              - "assistant_tool_call": 模型的工具调用请求
              - "tool_result": 工具执行结果
        content: 消息文本内容。
        toolUseId: 工具调用唯一标识符，tool_result 消息中关联到原始调用。
        toolName: 工具名称，assistant_tool_call 和 tool_result 消息中使用。
        input: 工具调用的输入参数字典。
        isError: 工具结果是否为错误（True 时表示执行异常）。
    """
    role: Literal[
        "system",
        "user",
        "assistant",
        "assistant_progress",
        "assistant_tool_call",
        "tool_result",
    ]
    content: str
    toolUseId: str
    toolName: str
    input: Any
    isError: bool


class ToolCall(TypedDict):
    """表示模型发出的工具调用。

    参数:
        id: 工具调用唯一标识符。
        toolName: 要调用的工具名称。
        input: 工具调用输入参数。
    """
    id: str
    toolName: str
    input: Any


@dataclass(slots=True)
class StepDiagnostics:
    """单次模型调用的诊断快照，记录模型响应中的停止原因和内容块构成。

    【在流程中的位置】由模型适配器在解析 API 响应时创建
    （anthropic_adapter.py 第 464-468 行、openai_adapter.py 第 495-499 行），
    作为 AgentStep.diagnostics 字段返回。被 agent 循环的 Step C（处理模型返回阶段）
    在 agent_loop.py 第 2067-2089 行消费，用于：
    - 提取 stopReason、blockTypes、ignoredBlockTypes 供 decide_assistant_turn 决策
    - 判断是否为可恢复的思考中断（_is_recoverable_thinking_stop）
    - 格式化诊断信息文本（_format_diagnostics）

    字段说明:
        stopReason: 模型返回的停止原因
                    - "end_turn": 正常结束
                    - "max_tokens": 达到 token 上限被截断
                    - "tool_use": 请求调用工具
                    - "pause_turn": 暂停
                    None: 未提供
        blockTypes: API 返回的所有 content block 类型列表，
                   如 ["text", "tool_use", "thinking"] 等。
        ignoredBlockTypes: 当前适配器未处理的 block 类型列表，
                          如 ["thinking"] 等扩展块类型。
    """
    stopReason: str | None = None
    blockTypes: list[str] = field(default_factory=list)
    ignoredBlockTypes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentStep:
    """模型适配器单次调用的返回结果，封装了模型的一次响应（回复文本或工具调用）。

    【在流程中的位置】由模型适配器（AnthropicModelAdapter、OpenAIModelAdapter、
    MockModelAdapter）在 next() 方法中创建，被 agent 循环的 Step B（模型调用阶段）
    在 agent_loop.py 第 1935 行 `next_step = _model_next(...)` 处接收。随后在第 2066 行
    根据 `next_step.type` 分发：type="assistant" 进入 Step C（处理模型返回，
    调用 decide_assistant_turn），type="tool_calls" 进入 Step D（执行工具）。

    字段说明:
        type: 步骤类型
              - "assistant": 模型回复文本，进入 decide_assistant_turn 决策
              - "tool_calls": 模型请求调用工具，进入工具执行流程
        content: 文本内容，默认为空字符串。
        kind: 内容分类标记
              - "final": 最终答案
              - "progress": 中间进度更新
              None: 无明确标记（由 decide_assistant_turn 进一步判断）
        calls: 工具调用列表，仅 type="tool_calls" 时有值。
        contentKind: 内容子分类，当前仅支持 "progress"。
        diagnostics: 单次模型调用的诊断快照（StepDiagnostics），
                    记录了停止原因和内容块构成，供 Step C 决策使用。
    """
    type: Literal["assistant", "tool_calls"]
    content: str = ""
    kind: Literal["final", "progress"] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    contentKind: Literal["progress"] | None = None
    diagnostics: StepDiagnostics | None = None


RuntimeEventCategory = Literal[
    "phase",
    "compaction",
    "guard",
    "widening",
    "recovery",
    "stop",
]
"""运行时事件分类的字面量类型别名。

可选值:
    - phase: 阶段切换事件
    - compaction: 压缩事件
    - guard: 防护检查事件
    - widening: 扩展事件
    - recovery: 恢复事件
    - stop: 停止事件
"""


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """表示运行时中发生的诊断事件。

    用于在运行时追踪各阶段状态变化、守护检查、扩展与恢复等诊断信息。

    参数:
        category: 事件分类。
        message: 事件描述信息。
        step: 事件发生时所在的步骤号，默认为 None。
        profile: 配置名称，默认为空字符串。
        phase: 阶段名称，默认为空字符串。
        verification_focus: 验证焦点描述，默认为空字符串。
        stop_reason: 停止原因描述，默认为空字符串。
        widening_reason: 扩展原因描述，默认为空字符串。
        evidence_summary: 证据摘要，默认为空字符串。
    """
    category: RuntimeEventCategory
    message: str
    step: int | None = None
    profile: str = ""
    phase: str = ""
    verification_focus: str = ""
    stop_reason: str = ""
    widening_reason: str = ""
    evidence_summary: str = ""


class ModelAdapter(Protocol):
    """模型适配器协议，定义 agent 循环与底层 LLM 之间的统一调用接口。

    【在流程中的位置】此 Protocol 被 agent 循环的 run_agent_turn() 函数
    作为入参接收（agent_loop.py 第 1121 行 `model: ModelAdapter`），
    在 Step B（模型调用阶段）通过 _model_next() 调用其 next() 方法
    获取 AgentStep。所有具体模型适配器都必须实现此协议。当前实现包括：
    - AnthropicModelAdapter（Anthropic Messages API）
    - OpenAIModelAdapter（OpenAI Chat Completions API）
    - MockModelAdapter（测试用模拟适配器，解析类 shell 命令）

    方法说明:
        next(): 接收消息列表，返回模型的单次响应（AgentStep）。
                支持可选的流式回调 on_stream_chunk 和状态存储 store 参数。
    """
    def next(
        self,
        messages: list[ChatMessage],
        on_stream_chunk: Callable[[str], None] | None = None,
        store: Any | None = None,
    ) -> AgentStep:
        """获取模型的下一次响应。

        向模型发送消息列表，并返回对应的智能体执行步骤。
        支持通过回调函数处理流式输出。

        参数:
            messages: 聊天消息列表。
            on_stream_chunk: 流式输出的回调函数，每次收到新块时调用，
                             默认为 None。
            store: 可选的存储对象，用于跨步骤持久化数据，默认为 None。

        返回:
            包含模型响应内容的 AgentStep 实例。
        """
        ...
