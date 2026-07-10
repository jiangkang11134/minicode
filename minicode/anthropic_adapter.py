"""Anthropic Messages API 的模型适配器，处理 HTTP 请求、流式响应和工具调用。

负责将内部消息格式转换为 Anthropic Messages API 格式，管理 API 请求
的生命周期（包括重试、流式解析、成本追踪），以及处理思考块（thinking
blocks）的往返保留。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from minicode.api_retry import (
    RETRYABLE_STATUS,
    calculate_backoff,
)
from minicode.state import add_cost, record_api_error, update_context_usage
from minicode.types import AgentStep, StepDiagnostics

if TYPE_CHECKING:
    from minicode.state import AppState, Store

DEFAULT_MAX_RETRIES = 4


def _get_retry_limit() -> int:
    """从环境变量获取最大重试次数，并对结果做有效性约束。

    读取 MINI_CODE_MAX_RETRIES 环境变量，若无效或未设置则返回
    DEFAULT_MAX_RETRIES（4），确保返回值为非负整数。

    返回:
        最大重试次数
    """
    try:
        value = int(float(os.environ.get("MINI_CODE_MAX_RETRIES", DEFAULT_MAX_RETRIES)))
    except ValueError:
        value = DEFAULT_MAX_RETRIES
    return max(0, value)


def _parse_retry_after_seconds(retry_after: str | None) -> float | None:
    """将 Retry-After 响应头部解析为秒数。

    支持两种格式：
    1. 整数值或小数值（如 "120"、"1.5"）
    2. HTTP-date 格式（如 "Wed, 21 Oct 2015 07:28:00 GMT"）

    参数:
        retry_after: Retry-After 头部的原始字符串值

    返回:
        解析后的秒数；解析失败或输入为空时返回 None
    """
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(retry_after)
        return max(0.0, target.timestamp() - time.time())
    except (ValueError, TypeError):
        pass
    return None


def _read_json_body(response) -> Any:
    """读取 HTTP 响应体并将其解析为 JSON 对象。

    对空响应体返回空字典；对无效 JSON 格式返回包含原始文本的
    错误结构，确保调用方始终能获得可处理的字典对象。

    参数:
        response: HTTP 响应对象（应支持 .read() 方法）

    返回:
        解析后的字典，解析失败时返回 {"error": {"message": ...}}
    """
    text = response.read().decode("utf-8")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": text.strip()}}


def _extract_error_message(data: Any, status: int) -> str:
    """从 API 错误响应中提取人类可读的错误消息。

    优先使用 Anthropic 格式的 error.message 字段，否则使用状态码
    构造默认错误消息。

    参数:
        data: API 返回的（解析后的）JSON 数据
        status: HTTP 状态码

    返回:
        错误描述字符串
    """
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"Model request failed: {status}"


def _messages_endpoint(base_url: str) -> str:
    """构造 Anthropic Messages API 的完整 URL 路径。

    自动处理 URL 末尾缺失 /v1 或 /v1/messages 片段的兼容性。

    参数:
        base_url: 基础 URL（带或不带 /v1 后缀均可）

    返回:
        完整的 /v1/messages 端点 URL
    """
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    return normalized + "/v1/messages"


def _parse_assistant_text(content: str) -> tuple[str, str | None]:
    """解析助手的文本回复，提取特殊标记（final/progress）。

    支持 <final>...</final>、[FINAL]、<progress>...</progress>、
    [PROGRESS] 等标记格式。无标记时返回原始文本及 None 类型。

    参数:
        content: 助手的原始文本内容

    返回:
        (清理后的文本, 类型标记) 元组，类型为 "final"、"progress" 或 None
    """
    trimmed = content.strip()
    if not trimmed:
        return "", None
    markers = [
        ("<final>", "final", "</final>"),
        ("[FINAL]", "final", None),
        ("<progress>", "progress", "</progress>"),
        ("[PROGRESS]", "progress", None),
    ]
    for prefix, kind, closing_tag in markers:
        if trimmed.startswith(prefix):
            raw = trimmed[len(prefix) :].strip()
            if closing_tag:
                raw = raw.replace(closing_tag, "").strip()
            return raw, kind
    return trimmed, None


def _to_text_block(text: str) -> dict[str, str]:
    """将文本字符串转换为 Anthropic API 的 text content block 格式。

    参数:
        text: 文本内容

    返回:
        {"type": "text", "text": text} 格式的字典
    """
    return {"type": "text", "text": text}


def _to_assistant_text(message: dict[str, Any]) -> str:
    """将内部消息格式中的助手内容转换为文本表示。

    对于 assistant_progress 类型的消息，用 <progress> 标记包裹内容。

    参数:
        message: 内部消息字典，应包含 role 和 content 字段

    返回:
        文本形式的助手消息内容
    """
    if message["role"] == "assistant_progress":
        return f"<progress>\n{message['content']}\n</progress>"
    return message["content"]


def _push_anthropic_message(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    """将 content block 追加到 Anthropic 格式的消息列表。

    如果列表最后一条消息的角色与指定 role 相同，则将 block 追加到该
    消息的 content 数组中；否则创建新消息。这保证了 Anthropic 交替
    user/assistant 角色序列的正确性。

    参数:
        messages: 正在构建的 Anthropic 格式消息列表
        role: 消息角色（"user" 或 "assistant"）
        block: 要追加的 content block 字典
    """
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """将内部消息格式转换为 Anthropic Messages API 格式。

    处理五种角色：
    - system: 提取为独立字符串，不放入消息列表
    - user: 转换为 text block
    - assistant / assistant_progress: 转换为文本 block
    - assistant_tool_call: 转换为 tool_use block
    - 其他（tool_result）: 转换为 tool_result block

    参数:
        messages: 内部消息列表

    返回:
        (system_message, converted_messages) 元组
    """
    system = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "user":
            _push_anthropic_message(converted, "user", _to_text_block(message["content"]))
            continue
        if role in {"assistant", "assistant_progress"}:
            _push_anthropic_message(converted, "assistant", _to_text_block(_to_assistant_text(message)))
            continue
        if role == "assistant_tool_call":
            _push_anthropic_message(
                converted,
                "assistant",
                {"type": "tool_use", "id": message["toolUseId"], "name": message["toolName"], "input": message["input"]},
            )
            continue
        _push_anthropic_message(
            converted,
            "user",
            {
                "type": "tool_result",
                "tool_use_id": message["toolUseId"],
                "content": message["content"],
                "is_error": message["isError"],
            },
        )
    return system, converted


class AnthropicModelAdapter:
    """Anthropic Messages API 的模型调用适配器。

    封装了完整的 API 调用流程，包括消息格式转换、工具序列化缓存、
    重试逻辑、流式响应解析、成本追踪以及思考块（thinking blocks）
    的往返保留。支持流式（streaming）和非流式两种调用模式。
    """

    def __init__(self, runtime: dict[str, Any], tools) -> None:
        """初始化适配器实例。

        参数:
            runtime: 运行时配置字典，包含 model、baseUrl、apiKey、
                     authToken、maxOutputTokens、disableThinking 等字段
            tools: 工具管理器，提供 .list() 方法获取工具列表
        """
        self.runtime = runtime
        self.tools = tools
        # Cache the serialized tool list — tools rarely change within a session
        self._cached_tools_json: list[dict[str, Any]] | None = None
        self._tools_cache_key: int = 0  # hash of tool list for invalidation
        self._thinking_blocks: list[dict[str, Any]] = []  # Preserve thinking blocks for round-trip

    def _get_serialized_tools(self) -> list[dict[str, Any]]:
        """获取序列化后的工具列表（带缓存）。

        通过工具名称和描述计算哈希值判断是否有更新，避免频繁序列化。
        缓存机制在工具不常变更的会话中显著减少重复计算。

        返回:
            序列化工具列表，每项包含 name、description、input_schema
        """
        current_tools = self.tools.list()
        current_key = hash(tuple((t.name, t.description) for t in current_tools))
        if self._cached_tools_json is None or current_key != self._tools_cache_key:
            self._cached_tools_json = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in current_tools
            ]
            self._tools_cache_key = current_key
        return self._cached_tools_json

    def next(
        self,
        messages: list[dict[str, Any]],
        on_stream_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        store: Store[AppState] | None = None,
    ) -> AgentStep:
        """调用模型获取下一步动作。

        支持流式和非流式两种模式：
        - 非流式：同步等待完整响应后进行解析
        - 流式：逐 chunk 回调文本内容、思考过程和工具调用
        - 在非流式模式中，自动处理工具调用列表和文本内容的解析
        - 在流式模式中，实时组装 tool_use、thinking 等块

        同时负责：
        - 重放先前保留的 thinking blocks
        - 管理 API 重试（基于环境变量配置）
        - 更新 store 中的成本追踪和上下文使用量
        - 对非流式响应注入 thinking 块保留逻辑

        参数:
            messages: 内部格式的消息列表
            on_stream_chunk: 流式模式中，每个文本 delta 的回调
            on_thinking_delta: 流式模式中，每个思考 delta 的回调
            store: 全局状态 store，用于记录成本和上下文用量

        返回:
            AgentStep，类型为 "assistant"（纯文本回复）或
            "tool_calls"（工具调用）
        """
        system_message, converted_messages = _to_anthropic_messages(messages)

        # Replay stored thinking blocks into the first assistant message
        # with text content (DeepSeek extended thinking round-trip)
        if self._thinking_blocks:
            for i in range(len(converted_messages)):
                msg = converted_messages[i]
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        converted_messages[i] = dict(msg)
                        converted_messages[i]["content"] = list(self._thinking_blocks) + content
                        break
            self._thinking_blocks = []

        request_body = {
            "model": self.runtime.get("model", ""),
            "system": system_message,
            "messages": converted_messages,
            "tools": self._get_serialized_tools(),
        }
        # Disable extended thinking for non-Anthropic models that support it
        # but require round-trip preservation our message format can't provide
        if self.runtime.get("disableThinking"):
            request_body["thinking"] = {"type": "disabled"}
        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]
        if on_stream_chunk:
            request_body["stream"] = True

        # 成本上限检查
        from minicode.cost_tracker import check_cost_limit
        check_cost_limit()

        request = urllib.request.Request(
            url=_messages_endpoint(self.runtime.get("baseUrl", "")),
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                **(
                    {"x-api-key": self.runtime["apiKey"]}
                    if self.runtime.get("apiKey")
                    else {"Authorization": f"Bearer {self.runtime.get('authToken', '')}"}
                ),
            },
            method="POST",
        )

        max_retries = _get_retry_limit()
        response = None
        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                timeout = int(os.environ.get("MINICODE_MODEL_TIMEOUT", "60"))
                response = urllib.request.urlopen(request, timeout=timeout)
                break
            except urllib.error.HTTPError as error:
                last_exception = error
                response = error
                if error.code not in RETRYABLE_STATUS or attempt >= max_retries:
                    break
                # Use semantic error classification for adaptive backoff
                from minicode.api_retry import classify_error
                category = classify_error(error)
                retry_after = _parse_retry_after_seconds(error.headers.get("retry-after"))
                wait = calculate_backoff(attempt, retry_after=retry_after,
                                        category=category)
                time.sleep(wait)
            except urllib.error.URLError as error:
                last_exception = error
                if attempt >= max_retries:
                    break
                wait = calculate_backoff(attempt)
                time.sleep(wait)
        if response is None:
            if last_exception is not None:
                raise RuntimeError(
                    f"Model request failed before receiving a response: {last_exception}"
                ) from last_exception
            raise RuntimeError("Model request failed before receiving a response")

        if not on_stream_chunk:
            data = _read_json_body(response)
            status = getattr(response, "status", getattr(response, "code", 200))
            if status >= 400:
                if store:
                    store.set_state(record_api_error())
                raise RuntimeError(_extract_error_message(data, status))

            # Update store with API call success and cost tracking
            if store:
                # Calculate token usage and cost (with cache support)
                from minicode.cost_tracker import calculate_cost
                usage = data.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

                cost_usd = calculate_cost(
                    model=self.runtime.get("model", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                )
                if cost_usd > 0:
                    store.set_state(add_cost(cost_usd))

                # Update context usage
                total_tokens = input_tokens + output_tokens
                store.set_state(update_context_usage(total_tokens))

            tool_calls: list[dict[str, Any]] = []
            text_parts: list[str] = []
            block_types: list[str] = []
            ignored_block_types: list[str] = []

            for block in data.get("content", []) if isinstance(data, dict) else []:
                block_type = block.get("type")
                block_types.append(block_type)
                if block_type == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                    tool_calls.append({"id": block["id"], "toolName": block["name"], "input": block.get("input")})
                elif block_type == "thinking":
                    self._thinking_blocks.append(block)  # Preserve for round-trip
                else:
                    ignored_block_types.append(str(block_type))

            parsed_text, kind = _parse_assistant_text("\n".join(text_parts).strip())
            diagnostics = StepDiagnostics(
                stopReason=data.get("stop_reason") if isinstance(data, dict) else None,
                blockTypes=block_types,
                ignoredBlockTypes=ignored_block_types,
            )

            if tool_calls:
                return AgentStep(
                    type="tool_calls",
                    calls=tool_calls,
                    content=parsed_text,
                    contentKind="progress" if kind == "progress" else None,
                    diagnostics=diagnostics,
                )
            return AgentStep(type="assistant", content=parsed_text, kind=kind, diagnostics=diagnostics)

        # STREAMING PARSER
        tool_calls = []
        text_parts = []
        block_types = []
        ignored_block_types = []
        active_tool_call = None
        active_thinking_block = None
        stop_reason = None

        # Streaming cost tracking
        stream_input_tokens = 0
        stream_output_tokens = 0
        stream_cache_read_tokens = 0
        stream_cache_creation_tokens = 0

        for line in response:
            line_str = line.decode("utf-8").strip()
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str == "[DONE]":
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            if etype == "message_start":
                # Initial usage from message_start
                msg = event.get("message", {})
                usage = msg.get("usage", {})
                stream_input_tokens = usage.get("input_tokens", 0)
                stream_cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                stream_cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
            elif etype == "content_block_start":
                cb = event.get("content_block", {})
                c_type = cb.get("type")
                block_types.append(c_type)
                if c_type == "tool_use":
                    active_tool_call = {
                        "id": cb.get("id"),
                        "name": cb.get("name"),
                        "input_json": ""
                    }
                elif c_type == "thinking":
                    active_thinking_block = {"type": "thinking", "thinking": ""}
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                d_type = delta.get("type")
                if d_type == "text_delta":
                    chunk = delta.get("text", "")
                    text_parts.append(chunk)
                    on_stream_chunk(chunk)
                elif d_type == "input_json_delta":
                    if active_tool_call:
                        active_tool_call["input_json"] += delta.get("partial_json", "")
                elif d_type == "thinking_delta":
                    if active_thinking_block:
                        chunk = delta.get("thinking", "")
                        active_thinking_block["thinking"] += chunk
                        if on_thinking_delta:
                            on_thinking_delta(chunk)
                elif d_type == "signature_delta":
                    if active_thinking_block:
                        active_thinking_block["signature"] = active_thinking_block.get("signature", "") + delta.get("signature", "")
            elif etype == "content_block_stop":
                if active_tool_call:
                    try:
                        parsed_input = json.loads(active_tool_call["input_json"])
                    except Exception:
                        parsed_input = {}
                    tool_calls.append({
                        "id": active_tool_call["id"],
                        "toolName": active_tool_call["name"],
                        "input": parsed_input
                    })
                    active_tool_call = None
                if active_thinking_block:
                    self._thinking_blocks.append(active_thinking_block)
                    active_thinking_block = None
            elif etype == "message_delta":
                delta = event.get("delta", {})
                if "stop_reason" in delta:
                    stop_reason = delta["stop_reason"]
                # Final output tokens from message_delta
                usage = event.get("usage", {})
                if usage.get("output_tokens"):
                    stream_output_tokens = usage["output_tokens"]
            elif etype == "error":
                err = event.get("error", {})
                raise RuntimeError(f"Streaming error: {err.get('message', 'Unknown')}")

        # Update store with streaming cost tracking
        if store:
            from minicode.cost_tracker import calculate_cost
            cost_usd = calculate_cost(
                model=self.runtime.get("model", ""),
                input_tokens=stream_input_tokens,
                output_tokens=stream_output_tokens,
                cache_read_tokens=stream_cache_read_tokens,
                cache_creation_tokens=stream_cache_creation_tokens,
            )
            if cost_usd > 0:
                store.set_state(add_cost(cost_usd))
            total_tokens = stream_input_tokens + stream_output_tokens
            store.set_state(update_context_usage(total_tokens))

        parsed_text, kind = _parse_assistant_text("".join(text_parts).strip())
        diagnostics = StepDiagnostics(
            stopReason=stop_reason,
            blockTypes=block_types,
            ignoredBlockTypes=ignored_block_types,
        )
        if tool_calls:
            return AgentStep(
                type="tool_calls",
                calls=tool_calls,
                content=parsed_text,
                contentKind="progress" if kind == "progress" else None,
                diagnostics=diagnostics,
            )
        return AgentStep(type="assistant", content=parsed_text, kind=kind, diagnostics=diagnostics)
