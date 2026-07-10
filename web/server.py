"""SmartCode Web UI 服务器 — FastAPI + SSE 流式响应。

启动：python web/server.py
访问：http://localhost:8341
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# 确保能找到 minicode 模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from minicode.agent_loop import run_agent_turn
from minicode.config import load_runtime_config
from minicode.model_registry import create_model_adapter
from minicode.tools import create_default_tool_registry

app = FastAPI(title="SmartCode Web UI")

# ── API ──

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def get_config():
    """返回当前模型配置和审查模式。"""
    try:
        runtime = load_runtime_config()
        return {
            "model": runtime.get("model", "unknown"),
            "review_mode": os.environ.get("MINICODE_REVIEW_MODE", "off"),
            "cost_limit": os.environ.get("MINICODE_API_COST_LIMIT", "unlimited"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/files")
async def list_files(path: str = "."):
    """列出项目文件。"""
    target = ROOT / path
    if not target.exists():
        return {"error": f"Path not found: {path}"}

    items = []
    for entry in sorted(target.iterdir()):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        items.append({
            "name": entry.name,
            "path": str(entry.relative_to(ROOT)),
            "type": "dir" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    return {"items": items, "current": path, "parent": str(Path(path).parent) if path != "." else None}


@app.get("/api/file")
async def read_file(path: str):
    """读取文件内容。"""
    target = ROOT / path
    if not target.exists() or target.is_dir():
        return {"error": "File not found"}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": content, "size": target.stat().st_size}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/upload")
async def upload_file(file: UploadFile):
    """上传文件到项目目录。"""
    try:
        content = await file.read()
        target = ROOT / file.filename
        target.write_bytes(content)
        return {"status": "ok", "path": file.filename, "size": len(content)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/report")
async def get_report():
    """返回运行报告。"""
    try:
        from minicode.report import build_session_report, format_session_report
        report = build_session_report()
        return report
    except Exception as e:
        return {"error": str(e)}


# ── WebSocket Chat ──

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_text()
        payload = json.loads(data)
        user_input = payload.get("message", "")
        mode = payload.get("mode", "off")
        model_name = payload.get("model", os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash"))

        if not user_input.strip():
            await websocket.send_json({"type": "error", "content": "消息不能为空"})
            await websocket.close()
            return

        # 设置环境
        os.environ["MINICODE_REVIEW_MODE"] = mode
        os.environ["MINI_CODE_SHOW_GUIDE"] = "0"

        # 构建消息
        await websocket.send_json({"type": "status", "content": "正在初始化模型..."})

        loop = asyncio.get_event_loop()

        def run_agent() -> list[dict]:
            runtime = load_runtime_config()
            tools = create_default_tool_registry(str(ROOT), runtime=runtime)
            model = create_model_adapter(model=model_name, tools=tools, runtime=runtime)
            return run_agent_turn(
                model=model, tools=tools,
                messages=[{"role": "user", "content": user_input}],
                cwd=str(ROOT), max_steps=8,
                system_prompt="你是 SmartCode 编码助手。用 write_file/edit_file 工具。",
            )

        await websocket.send_json({"type": "status", "content": "模型已就绪，开始处理..."})

        result = await loop.run_in_executor(None, run_agent)

        # 提取助手回复
        response = ""
        tool_calls = []
        for m in result:
            role = m.get("role", "")
            if role == "assistant" and m.get("content"):
                response += m["content"]
            elif role == "assistant_tool_call":
                tool_calls.append({
                    "tool": m.get("toolName", ""),
                    "input": m.get("input", {}),
                })

        # 检查生成的文件
        output_files = []
        import glob as py_glob
        for f in py_glob.glob(str(ROOT / "*.py")):
            fname = Path(f).name
            if fname not in ("pyproject.toml",) and not fname.startswith("minicode") and not fname.startswith("benchmark"):
                output_files.append(fname)

        await websocket.send_json({
            "type": "result",
            "content": response or "（无回复）",
            "tool_calls": tool_calls[:5],
            "files": output_files[:10],
        })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        await websocket.send_json({"type": "error", "content": str(e)})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── 前端页面 ──

@app.get("/")
async def index():
    html_path = Path(__file__).resolve().parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    print(f"  SmartCode Web UI: http://localhost:8341")
    uvicorn.run(app, host="0.0.0.0", port=8341)
