"""MockModelAdapter —— 用于测试和开发的模拟模型适配器。

解析用户输入的类 shell 命令（如 /ls, /read, /grep 等），模拟 LLM 的
工具调用响应，无需真实调用任何 LLM API。适用于开发、测试和离线演示。
"""

from __future__ import annotations

import time

from minicode.types import AgentStep


def _last_user_message(messages):
    """从消息列表中找到最后一条 user 角色的消息内容。

    参数:
        messages: 消息字典列表。

    返回:
        最后一条 user 消息的 content 字符串，若没有则返回空字符串。
    """
    # return next((message["content"] for message in reversed(messages) if message["role"] == "user"), "")


def _last_tool_message(messages):
    """从消息列表中找到最后一条 tool_result 角色的消息。

    参数:
        messages: 消息字典列表。

    返回:
        最后一条 tool_result 消息的字典，若没有则返回 None。
    """
    # return next((message for message in reversed(messages) if message["role"] == "tool_result"), None)


def _latest_assistant_call(messages):
    """从消息列表中找到最后一条 assistant_tool_call 角色消息中的工具名称。

    参数:
        messages: 消息字典列表。

    返回:
        最后调用的工具名称字符串，若没有则返回 None。
    """
    # call = next((message for message in reversed(messages) if message["role"] == "assistant_tool_call"), None)
    return call["toolName"] if call else None


class MockModelAdapter:
    """模拟 LLM 模型适配器，用于开发和测试。

    解析用户输入中的类 shell 命令（/ls, /read, /grep, /write, /edit, /patch, /cmd, /tools），
    返回相应的工具调用 AgentStep 或模拟的 assistant 回复。
    """

    def next(self, messages, on_stream_chunk=None, **_kwargs):
        """模拟模型的一次推理步骤，根据最后一条消息生成响应。

        如果最后消息是 tool_result，则根据对应的工具调用生成模拟回复。
        如果最后消息是 user，则解析其中的类 shell 命令并返回相应的工具调用。

        参数:
            messages: 消息列表，包含对话历史。
            on_stream_chunk: 流式回调（本适配器中未使用）。
            **_kwargs: 其他参数（本适配器中忽略）。

        返回:
            AgentStep 实例，类型为 "assistant" 或 "tool_calls"。
        """
        # tool_message = _last_tool_message(messages)
        if tool_message and tool_message["role"] == "tool_result":
            last_call = _latest_assistant_call(messages)
            if last_call == "list_files":
                return AgentStep(type="assistant", content=f"Directory contents:\n\n{tool_message['content']}")
            if last_call == "read_file":
                return AgentStep(type="assistant", content=f"File contents:\n\n{tool_message['content']}")
            if last_call in {"write_file", "edit_file", "modify_file", "patch_file"}:
                return AgentStep(type="assistant", content=tool_message["content"])
            return AgentStep(type="assistant", content=f"I received the tool result:\n\n{tool_message['content']}")

        user_text = _last_user_message(messages).strip()
        tool_id = f"mock-{int(time.time() * 1000)}"

        if user_text == "/tools":
            return AgentStep(
                type="assistant",
                content="Available tools: ask_user, list_files, grep_files, read_file, write_file, edit_file, patch_file, run_command",
            )

        if user_text.startswith("/ls"):
            directory = user_text.replace("/ls", "", 1).strip()
            return AgentStep(
                type="tool_calls",
                calls=[{"id": tool_id, "toolName": "list_files", "input": {"path": directory} if directory else {}}],
            )

        if user_text.startswith("/grep "):
            payload = user_text[len("/grep ") :].strip()
            pattern, _, search_path = payload.partition("::")
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": tool_id,
                        "toolName": "grep_files",
                        "input": {"pattern": pattern.strip(), "path": search_path.strip() or None},
                    }
                ],
            )

        if user_text.startswith("/read "):
            return AgentStep(
                type="tool_calls",
                calls=[{"id": tool_id, "toolName": "read_file", "input": {"path": user_text[len('/read ') :].strip()}}],
            )

        if user_text.startswith("/cmd "):
            payload = user_text[len("/cmd ") :].strip()
            return AgentStep(type="tool_calls", calls=[{"id": tool_id, "toolName": "run_command", "input": {"command": payload}}])

        if user_text.startswith("/write "):
            payload = user_text[len("/write ") :]
            target_path, separator, content = payload.partition("::")
            if not separator:
                return AgentStep(type="assistant", content="Usage: /write <path>::<content>")
            return AgentStep(
                type="tool_calls",
                calls=[{"id": tool_id, "toolName": "write_file", "input": {"path": target_path.strip(), "content": content}}],
            )

        if user_text.startswith("/edit "):
            payload = user_text[len("/edit ") :]
            parts = payload.split("::")
            if len(parts) != 3:
                return AgentStep(type="assistant", content="Usage: /edit <path>::<search>::<replace>")
            target_path, search, replace = parts
            return AgentStep(
                type="tool_calls",
                calls=[{"id": tool_id, "toolName": "edit_file", "input": {"path": target_path.strip(), "search": search, "replace": replace}}],
            )

        if user_text.startswith("/patch "):
            payload = user_text[len("/patch ") :]
            parts = payload.split("::")
            if len(parts) < 3 or len(parts) % 2 == 0:
                return AgentStep(type="assistant", content="Usage: /patch <path>::<search1>::<replace1>::<search2>::<replace2> ...")
            target_path, *ops = parts
            replacements = []
            for index in range(0, len(ops), 2):
                replacements.append({"search": ops[index], "replace": ops[index + 1]})
            return AgentStep(
                type="tool_calls",
                calls=[{"id": tool_id, "toolName": "patch_file", "input": {"path": target_path.strip(), "replacements": replacements}}],
            )

        return AgentStep(
            type="assistant",
            content="\n".join(
                [
                    "This is a minimal MiniCode Python shell.",
                    "You can try:",
                    "/tools",
                    "/ls",
                    "/grep pattern::src",
                    "/read README.md",
                    "/cmd pwd",
                    "/write notes.txt::hello",
                    "/edit notes.txt::hello::hello world",
                ]
            ),
        )
