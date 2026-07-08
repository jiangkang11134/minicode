"""SmartCode Agent 循环 — 核心入口。

原本是 agent_loop_lite.py 的薄层重新导出，现已整合到此文件。
核心实现见 loop_orchestrator.py（编排）、model_caller.py（模型调用）、tool_executor.py（工具执行）。

外部通过 `from minicode.agent_loop import run_agent_turn` 使用。
"""

from minicode.loop_orchestrator import run_agent_turn

__all__ = ["run_agent_turn"]
