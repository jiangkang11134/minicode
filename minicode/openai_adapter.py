"""OpenAI 兼容 API 适配器 —— 为 SmartCode 提供 OpenAI 系列模型的接入能力。

支持 GPT-4o、GPT-4-turbo、GPT-4o-mini 以及任何 OpenAI 兼容的端点
（例如 Azure OpenAI、本地 LLM 的 OpenAI 兼容 API）。

核心功能：
- 消息格式转换（内部格式 <-> OpenAI Chat Completion 格式）
- 流式和非流式响应处理
- 自动重试与错误分类
- Token 用量追踪与成本计算
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from minicode.api_retry import RETRYABLE_STATUS, calculate_backoff
from minicode.cost_tracker import calculate_cost
from minicode.state import AppState, Store, add_cost, record_api_error, update_context_usage
from minicode.types import AgentStep, StepDiagnostics

DEFAULT_MAX_RETRIES = 4
OPENAI_MODELS = {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-5.5", "gpt5.5", "o1", "o1-mini", "o3-mini"}
DEFAULT_OPENAI_USER_AGENT = "SmartCode-Python/0.5.0 (OpenAI-Compatible Adapter)"


def _is_openai_model(model: str) -> bool:
    """判断模型名称是否指示使用 OpenAI 兼容 API。

    通过直接匹配已知 OpenAI 模型名、前缀匹配（如 gpt-4、o1-）或检查
    环境变量中是否配置了 OpenAI 基础 URL 来判断。

    参数:
        model: 模型名称字符串。

    返回:
        是否为 OpenAI 兼容模型。
    """
    model_lower = model.lower()
    # Direct match
    if model_lower in OPENAI_MODELS:
        return True
    # Prefix match for versioned models
    for prefix in ("gpt-5", "gpt-4", "gpt-3.5", "gpt5", "o1-", "o3-", "chatgpt-"):
        if model_lower.startswith(prefix):
            return True
    # Check if explicitly using OpenAI base URL
    base_url = os.environ.get("OPENAI_BASE_URL", os.environ.get("OPENAI_API_BASE", ""))
    if base_url and "openai" in base_url.lower():
        return True
    return False


def _get_openai_base_url(runtime: dict[str, Any]) -> str:
    """获取 OpenAI 兼容 API 的基础 URL。

    优先级：runtime 配置 > OPENAI_BASE_URL 环境变量 > OPENAI_API_BASE 环境变量 > 默认值。

    参数:
        runtime: 运行时配置字典，可能包含 "openaiBaseUrl" 键。

    返回:
        基础 URL 字符串，末尾不带斜杠。
    """
    return (
        runtime.get("openaiBaseUrl", "")
        or os.environ.get("OPENAI_BASE_URL", "")
        or os.environ.get("OPENAI_API_BASE", "")
        or "https://api.openai.com"
    ).rstrip("/")


def _get_openai_chat_completions_url(runtime: dict[str, Any]) -> str:
    """拼接 OpenAI Chat Completions 接口的完整 URL。

    根据基础 URL 的结尾自动处理路径拼接。

    参数:
        runtime: 运行时配置字典。

    返回:
        Chat Completions 接口的完整 URL 字符串。
    """
    base_url = _get_openai_base_url(runtime)
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _get_openai_api_key(runtime: dict[str, Any]) -> str:
    """获取 OpenAI API key。

    优先级：runtime 配置 > OPENAI_API_KEY 环境变量。

    参数:
        runtime: 运行时配置字典，可能包含 "openaiApiKey" 键。

    返回:
        API key 字符串。
    """
    return (
        runtime.get("openaiApiKey", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


def _parse_openai_response_body(response: Any) -> tuple[dict[str, Any], str]:
    """解析 OpenAI 兼容 API 的 HTTP 响应体。

    尝试将响应体解码为 JSON，若失败则将原始文本作为降级返回。

    参数:
        response: HTTPResponse 对象，具有 read() 方法。

    返回:
        (解析后的 JSON 字典, 原始文本字符串) 的元组。
    """
    raw_body = response.read()
    decoded = raw_body.decode("utf-8", errors="replace")
    if not decoded.strip():
        return {}, ""
    try:
        return json.loads(decoded), decoded
    except json.JSONDecodeError:
        return {}, decoded


@dataclass
class _BufferedHTTPResponse:
    """用于在重试场景中保存 HTTP 错误响应的缓冲数据结构。

    将 HTTPError 的响应体预先读取到内存中，避免在重试时重复读取。
    """
    status: int
    code: int
    headers: Any
    body: bytes

    def read(self) -> bytes:
        """返回缓冲的响应体字节数据。

        返回:
            缓冲的 body 字节串。
        """
        return self.body


def _is_non_retryable_openai_error(status: int, data: dict[str, Any], raw_text: str) -> bool:
    """判断 OpenAI 错误是否为不可重试的永久性错误。

    检查 error.code 和 error.message 中是否包含如 model_not_found、
    insufficient_quota 等永久性错误标记。

    参数:
        status: HTTP 状态码。
        data: 解析后的 JSON 响应体。
        raw_text: 原始响应文本。

    返回:
        是否为不可重试的错误。
    """
    if status < 500:
        return False
    error_block = data.get("error", {}) if isinstance(data, dict) else {}
    code = str(error_block.get("code", "")).lower()
    message = str(error_block.get("message", "") or raw_text).lower()
    permanent_markers = (
        "model_not_found",
        "no available channel",
        "insufficient_quota",
        "monthly usage limit reached",
        "invalid api key",
    )
    return any(marker in code or marker in message for marker in permanent_markers)


def _to_openai_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """将 SmartCode 内部消息格式转换为 OpenAI Chat Completion 格式。

    处理 system / user / assistant / assistant_progress / assistant_tool_call /
    tool_result 等多种角色，分别转换为 OpenAI 对应的消息结构。

    参数:
        messages: SmartCode 内部格式的消息列表。

    返回:
        (system_message, chat_messages) 元组。
        system_message 为合并后的系统提示文本（可能为空字符串）。
        chat_messages 为 OpenAI 格式的消息字典列表。
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for message in messages:
        role = message["role"]
        content = message.get("content", "")

        if role == "system":
            system_parts.append(content)
            continue

        if role == "user":
            converted.append({"role": "user", "content": content})
            continue

        if role in ("assistant", "assistant_progress"):
            text = content
            if role == "assistant_progress":
                text = f"<progress>\n{content}\n</progress>"
            converted.append({"role": "assistant", "content": text})
            continue

        if role == "assistant_tool_call":
            # OpenAI format: assistant message with tool_calls
            converted.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": message["toolUseId"],
                    "type": "function",
                    "function": {
                        "name": message["toolName"],
                        "arguments": json.dumps(message["input"]) if isinstance(message["input"], dict) else "{}",
                    },
                }],
            })
            continue

        if role == "tool_result":
            converted.append({
                "role": "tool",
                "tool_call_id": message["toolUseId"],
                "content": message.get("content", ""),
            })
            continue

    system_message = "\n\n".join(system_parts)
    return system_message, converted


AssistantTextKind = Literal["final", "progress"]


def _parse_assistant_text(content: str) -> tuple[str, AssistantTextKind | None]:
    """解析 assistant 回复文本中的进度/最终标记。

    支持 <final>...</final>、[FINAL]、<progress>...</progress>、[PROGRESS]
    等标记，用于区分最终回复和中间进度更新。

    参数:
        content: 待解析的 assistant 文本。

    返回:
        (解析后的纯文本, 类型标记) 元组。
        类型标记为 "final"、"progress" 或 None。
    """
    trimmed = content.strip()
    if not trimmed:
        return "", None
    markers: list[tuple[str, AssistantTextKind, str | None]] = [
        ("<final>", "final", "</final>"),
        ("[FINAL]", "final", None),
        ("<progress>", "progress", "</progress>"),
        ("[PROGRESS]", "progress", None),
    ]
    for prefix, kind, closing_tag in markers:
        if trimmed.startswith(prefix):
            raw = trimmed[len(prefix):].strip()
            if closing_tag:
                raw = raw.replace(closing_tag, "").strip()
            return raw, kind
    return trimmed, None


class OpenAIModelAdapter:
    """OpenAI 兼容 API 的模型适配器。

    支持 GPT-4o、GPT-4-turbo、GPT-4o-mini 等 OpenAI 模型以及任何
    OpenAI 兼容的第三方端点。提供流式和非流式两种调用模式，内置
    重试机制和成本追踪。
    """

    def __init__(self, runtime: dict[str, Any], tools) -> None:
        """初始化 OpenAIModelAdapter。

        参数:
            runtime: 运行时配置字典，需包含 "model" 键。
            tools: 工具列表对象（具有 list() 方法）。
        """
        self.runtime = runtime
        self.tools = tools
        self._cached_tools_json: list[dict[str, Any]] | None = None
        self._tools_cache_key: int = 0

    def _get_serialized_tools(self) -> list[dict[str, Any]]:
        """获取序列化后的工具列表（OpenAI function 格式），带缓存。

        将工具对象的 name、description 和 input_schema 转换为 OpenAI
        的 function calling 格式。当工具列表内容变化时自动失效缓存。

        返回:
            OpenAI function calling 格式的工具定义字典列表。
        """
        current_tools = self.tools.list()
        current_key = hash(tuple((t.name, t.description) for t in current_tools))
        if self._cached_tools_json is None or current_key != self._tools_cache_key:
            self._cached_tools_json = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
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
        """执行一次模型推理，接收消息列表并返回 AgentStep。

        支持两种模式：
        - 非流式：等待完整响应，解析工具调用或文本内容。
        - 流式：通过 on_stream_chunk 逐块返回文本，最后组装完整结果。

        内置重试逻辑：最多重试 4 次，可重试的 HTTP 错误自动回退等待，
        不可重试的错误（如 model_not_found）立即抛出。

        参数:
            messages: SmartCode 内部格式的消息列表。
            on_stream_chunk: 流式模式下逐块回调函数。
            on_thinking_delta: 思考增量回调（本适配器中未使用）。
            store: 可选的全局状态存储，用于记录成本和 token 用量。

        返回:
            AgentStep 实例，类型为 "assistant" 或 "tool_calls"。
        """
        system_message, converted_messages = _to_openai_messages(messages)

        request_body: dict[str, Any] = {
            "model": self.runtime["model"],
            "messages": converted_messages,
            "tools": self._get_serialized_tools(),
        }

        if system_message:
            request_body["messages"].insert(0, {"role": "system", "content": system_message})

        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]

        if on_stream_chunk:
            request_body["stream"] = True

        api_key = _get_openai_api_key(self.runtime)

        # Build headers — support OpenRouter and custom endpoints
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": DEFAULT_OPENAI_USER_AGENT,
        }
        # OpenRouter extra headers (HTTP-Referer, X-Title)
        openrouter_headers = self.runtime.get("_openrouter_headers", {})
        headers.update(openrouter_headers)
        # Custom endpoint extra headers
        custom_headers = self.runtime.get("_custom_headers", {})
        headers.update(custom_headers)

        # OpenRouter extra params (transforms, etc.)
        openrouter_params = self.runtime.get("_openrouter_params", {})
        for k, v in openrouter_params.items():
            if v is not None:
                request_body[k] = v

        # 成本上限检查
        from minicode.cost_tracker import check_cost_limit
        check_cost_limit()

        request = urllib.request.Request(
            url=_get_openai_chat_completions_url(self.runtime),
            data=json.dumps(request_body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        # Retry logic
        max_retries = 4
        response = None
        for attempt in range(max_retries + 1):
            try:
                timeout = int(os.environ.get("MINICODE_MODEL_TIMEOUT", "120"))
                response = urllib.request.urlopen(request, timeout=timeout)
                break
            except urllib.error.HTTPError as error:
                buffered_error = _BufferedHTTPResponse(
                    status=error.code,
                    code=error.code,
                    headers=getattr(error, "headers", None),
                    body=error.read(),
                )
                response = buffered_error
                parsed_error, raw_error_text = _parse_openai_response_body(buffered_error)
                if (
                    error.code not in RETRYABLE_STATUS
                    or attempt >= max_retries
                    or _is_non_retryable_openai_error(error.code, parsed_error, raw_error_text)
                ):
                    break
                from minicode.api_retry import classify_error
                category = classify_error(error)
                wait = calculate_backoff(attempt, category=category)
                time.sleep(wait)
            except urllib.error.URLError:
                if attempt >= max_retries:
                    raise
                wait = calculate_backoff(attempt)
                time.sleep(wait)

        if response is None:
            raise RuntimeError("OpenAI request failed before receiving a response")

        if not on_stream_chunk:
            # Non-streaming response
            data, raw_text = _parse_openai_response_body(response)
            status = getattr(response, "status", getattr(response, "code", 200))

            if status >= 400:
                if store:
                    store.set_state(record_api_error())
                error_msg = (
                    data.get("error", {}).get("message")
                    or raw_text.strip()
                    or f"OpenAI API error: {status}"
                )
                raise RuntimeError(error_msg)
            if not data:
                raise RuntimeError(
                    "OpenAI-compatible endpoint returned a non-JSON success payload."
                )

            # Cost tracking
            if store:
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cost_usd = calculate_cost(
                    model=self.runtime.get("model", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                if cost_usd > 0:
                    store.set_state(add_cost(cost_usd))
                store.set_state(update_context_usage(input_tokens + output_tokens))

            # Parse response
            choices = data.get("choices", [])
            if not choices:
                return AgentStep(type="assistant", content="")

            choice = choices[0]
            message = choice.get("message", {})
            text_content = message.get("content", "") or ""
            tool_calls_raw = message.get("tool_calls", [])

            stop_reason = choice.get("finish_reason")

            tool_calls = []
            if tool_calls_raw:
                for tc in tool_calls_raw:
                    func = tc.get("function", {})
                    try:
                        parsed_input = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        parsed_input = {}
                    tool_calls.append({
                        "id": tc.get("id", ""),
                        "toolName": func.get("name", ""),
                        "input": parsed_input,
                    })

            parsed_text, kind = _parse_assistant_text(text_content.strip())
            diagnostics = StepDiagnostics(
                stopReason=stop_reason,
                blockTypes=["tool_calls"] if tool_calls else (["text"] if text_content else []),
                ignoredBlockTypes=[],
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

        # Streaming response
        if isinstance(response, _BufferedHTTPResponse):
            data, raw_text = _parse_openai_response_body(response)
            if store:
                store.set_state(record_api_error())
            error_msg = (
                data.get("error", {}).get("message")
                or raw_text.strip()
                or f"OpenAI API error: {response.status}"
            )
            raise RuntimeError(error_msg)

        tool_calls = []
        text_parts = []
        active_tool_calls: dict[int, dict[str, Any]] = {}
        stop_reason = None
        stream_input_tokens = 0
        stream_output_tokens = 0

        for line in response:
            line_str = line.decode("utf-8").strip()
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = event.get("choices", [])
            if not choices:
                # Maybe usage info
                usage = event.get("usage", {})
                if usage:
                    stream_input_tokens = usage.get("prompt_tokens", 0)
                    stream_output_tokens = usage.get("completion_tokens", 0)
                continue

            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")
            if finish_reason:
                stop_reason = finish_reason

            # Text content
            content = delta.get("content", "")
            if content:
                text_parts.append(content)
                on_stream_chunk(content)

            # Tool calls (incremental)
            tc_deltas = delta.get("tool_calls", [])
            for tc_delta in tc_deltas:
                idx = tc_delta.get("index", 0)
                if idx not in active_tool_calls:
                    active_tool_calls[idx] = {
                        "id": tc_delta.get("id", ""),
                        "name": "",
                        "arguments": "",
                    }
                func = tc_delta.get("function", {})
                if func.get("name"):
                    active_tool_calls[idx]["name"] = func["name"]
                if func.get("arguments"):
                    active_tool_calls[idx]["arguments"] += func["arguments"]
                if tc_delta.get("id"):
                    active_tool_calls[idx]["id"] = tc_delta["id"]

        # Finalize tool calls
        for idx in sorted(active_tool_calls.keys()):
            tc = active_tool_calls[idx]
            try:
                parsed_input = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                parsed_input = {}
            tool_calls.append({
                "id": tc["id"],
                "toolName": tc["name"],
                "input": parsed_input,
            })

        # Streaming cost tracking
        if store:
            # Estimate if not provided in stream
            if stream_input_tokens == 0:
                from minicode.context_manager import estimate_messages_tokens
                stream_input_tokens = estimate_messages_tokens(messages)
            if stream_output_tokens == 0:
                stream_output_tokens = len("".join(text_parts)) // 4

            cost_usd = calculate_cost(
                model=self.runtime.get("model", ""),
                input_tokens=stream_input_tokens,
                output_tokens=stream_output_tokens,
            )
            if cost_usd > 0:
                store.set_state(add_cost(cost_usd))
            store.set_state(update_context_usage(stream_input_tokens + stream_output_tokens))

        parsed_text, kind = _parse_assistant_text("".join(text_parts).strip())
        diagnostics = StepDiagnostics(
            stopReason=stop_reason,
            blockTypes=["tool_calls"] if tool_calls else (["text"] if text_parts else []),
            ignoredBlockTypes=[],
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
