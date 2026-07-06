"""本地工具快捷命令解析器。

提供类 Slack 风格的斜杠命令（如 /ls, /grep, /read 等），
将用户输入中的快捷命令转换为标准化的工具调用字典，
便于客户端直接映射到具体的工具执行。
"""

from __future__ import annotations


def parse_local_tool_shortcut(user_input: str) -> dict | None:
    """解析用户输入中的本地工具快捷命令。

    支持的快捷命令及格式：
    - /ls [path]              -> list_files
    - /grep pattern[::path]   -> grep_files
    - /read path              -> read_file
    - /write path::content    -> write_file
    - /modify path::content   -> modify_file
    - /edit path::search::replace -> edit_file
    - /patch path::search::replace[...] -> patch_file
    - /cmd [cwd::]command     -> run_command

    注意：/ls 必须是精确命令或以空格开头，避免误匹配 /lsfoo 这样的文本。
    /patch 接受奇数个 :: 分隔的部分：路径 + 若干对 search/replace。

    参数:
        user_input: 用户原始输入字符串

    返回:
        包含 toolName 和 input 的字典，如果不是快捷命令则返回 None
    """
    # # `/ls` must be an exact command or followed by a space, so that adjacent
    # text like `/lsfoo` is NOT misparsed as a list_files shortcut (TS parity).
    if user_input == "/ls" or user_input.startswith("/ls "):
        directory = user_input[len("/ls") :].strip()
        return {"toolName": "list_files", "input": {"path": directory} if directory else {}}

    if user_input.startswith("/grep "):
        payload = user_input[len("/grep ") :].strip()
        pattern, _, search_path = payload.partition("::")
        if not pattern.strip():
            return None
        input_data = {"pattern": pattern.strip()}
        if search_path.strip():
            input_data["path"] = search_path.strip()
        return {"toolName": "grep_files", "input": input_data}

    if user_input.startswith("/read "):
        file_path = user_input[len("/read ") :].strip()
        return {"toolName": "read_file", "input": {"path": file_path}} if file_path else None

    if user_input.startswith("/write "):
        payload = user_input[len("/write ") :]
        target_path, separator, content = payload.partition("::")
        if not separator or not target_path.strip():
            return None
        return {
            "toolName": "write_file",
            "input": {"path": target_path.strip(), "content": content},
        }

    if user_input.startswith("/modify "):
        payload = user_input[len("/modify ") :]
        target_path, separator, content = payload.partition("::")
        if not separator or not target_path.strip():
            return None
        return {
            "toolName": "modify_file",
            "input": {"path": target_path.strip(), "content": content},
        }

    if user_input.startswith("/edit "):
        payload = user_input[len("/edit ") :]
        parts = payload.split("::")
        if len(parts) != 3 or not parts[0].strip():
            return None
        target_path, search, replace = parts
        return {
            "toolName": "edit_file",
            "input": {"path": target_path.strip(), "search": search, "replace": replace},
        }

    if user_input.startswith("/patch "):
        payload = user_input[len("/patch ") :]
        parts = payload.split("::")
        if len(parts) < 3 or len(parts) % 2 == 0:
            return None
        target_path, *ops = parts
        replacements = []
        for index in range(0, len(ops), 2):
            replacements.append({"search": ops[index], "replace": ops[index + 1]})
        return {
            "toolName": "patch_file",
            "input": {"path": target_path.strip(), "replacements": replacements},
        }

    if user_input.startswith("/cmd "):
        payload = user_input[len("/cmd ") :].strip()
        cwd, separator, command_text = payload.partition("::")
        text = command_text.strip() if separator else payload
        command_cwd = cwd.strip() if separator else None
        if not text:
            return None
        return {
            "toolName": "run_command",
            "input": {"command": text, "cwd": command_cwd or None},
        }

    return None
