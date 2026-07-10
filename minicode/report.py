"""运行报告 — 聚合会话指标生成统一摘要。

从 state、cost_tracker、turn_kernel、circuit_breaker 四个来源汇总数据。
"""

from __future__ import annotations
import os
import time
from typing import Any


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m}m"


def build_session_report() -> dict[str, Any]:
    """构建当前会话的运行报告。

    数据来源：
      - AppState（会话信息、成本、调用数）
      - CostTracker（按模型 token 明细）
      - CompactionCircuitBreaker（上下文压缩熔断器）
      - TurnRecurrentState / TurnCodaSummary（步数、错误、widening）
      - review/hooks（严格审查熔断器）
    """
    report: dict[str, Any] = {
        "session": {},
        "llm": {},
        "tools": {},
        "review": {},
        "compaction": {},
        "turn": {},
    }

    # ── 1. AppState 基本数据 ──
    try:
        from minicode.state import get_global_store
        store = get_global_store()
        state = store.get_state()
        elapsed = time.time() - state.created_at

        report["session"] = {
            "id": state.session_id,
            "model": state.model,
            "workspace": state.workspace,
            "duration": format_duration(elapsed),
            "duration_s": round(elapsed, 1),
        }
        report["llm"]["api_calls"] = state.api_calls
        report["llm"]["api_errors"] = state.api_errors
        report["llm"]["total_tokens"] = state.token_usage
        report["tools"]["tool_calls"] = state.tool_call_count
        report["tools"]["message_count"] = state.message_count

        # CostTracker 明细
        if hasattr(state, "cost_tracker") and state.cost_tracker:
            ct = state.cost_tracker
            report["llm"]["cost_usd"] = round(ct.total_cost_usd, 4)
            model_details = {}
            for model_name, usage in ct.model_usage.items():
                model_details[model_name] = {
                    "tokens": usage.total_tokens,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "calls": usage.call_count,
                    "errors": usage.error_count,
                    "cost": round(usage.cost_usd, 4),
                    "duration_ms": usage.total_duration_ms,
                }
            report["llm"]["model_breakdown"] = model_details
    except Exception:
        report["session"]["error"] = "AppState 不可用"

    # ── 2. 上下文压缩熔断器 ──
    try:
        from minicode.circuit_breaker import get_compaction_circuit_breaker
        cb = get_compaction_circuit_breaker()
        cb_state = cb.get_state()
        report["compaction"] = {
            "consecutive_failures": cb_state.consecutive_failures,
            "is_open": cb_state.is_open,
            "total_failures": cb_state.total_failures,
            "total_successes": cb_state.total_successes,
        }
    except Exception:
        report["compaction"]["error"] = "不可用"

    # ── 3. 审查系统熔断器 ──
    try:
        from minicode.review.hooks import _strict_review_failures, _strict_review_max_failures
        report["review"]["circuit_breaker"] = f"{_strict_review_failures[0]}/{_strict_review_max_failures}"
    except Exception:
        report["review"]["circuit_breaker"] = "N/A"
    report["review"]["mode"] = os.environ.get("MINICODE_REVIEW_MODE", "off")

    # 子 Agent 消耗
    if report.get("llm", {}).get("model_breakdown"):
        breakdown = report["llm"]["model_breakdown"]
        sub_model = os.environ.get("MINICODE_REVIEW_SUB_MODEL", "")
        if sub_model and sub_model in breakdown:
            report["review"]["sub_agent_llm_calls"] = breakdown[sub_model]["calls"]
            report["review"]["sub_agent_tokens"] = breakdown[sub_model]["tokens"]

    return report


def format_session_report(report: dict[str, Any] | None = None) -> str:
    if report is None:
        report = build_session_report()

    lines = [
        "=" * 56,
        "  SmartCode 运行报告",
        "=" * 56,
        "",
    ]

    # 会话
    s = report.get("session", {})
    if s:
        lines.extend([
            f"  会话: {s.get('id', 'N/A')[:12]}",
            f"  模型: {s.get('model', 'N/A')}",
            f"  运行时长: {s.get('duration', 'N/A')}",
            "",
        ])

    # LLM
    llm = report.get("llm", {})
    if llm:
        lines.append("  ── LLM 调用 ──")
        lines.append(f"    API 调用: {llm.get('api_calls', 0)} 次")
        lines.append(f"    API 错误: {llm.get('api_errors', 0)} 次")
        lines.append(f"    Token: {llm.get('total_tokens', 0):,}")
        if llm.get("cost_usd"):
            lines.append(f"    成本: ${llm['cost_usd']:.4f}")

        details = llm.get("model_breakdown", {})
        if details:
            lines.append("")
            lines.append("  ── 模型明细 ──")
            for model_name, d in details.items():
                dur = d.get("duration_ms", 0)
                lines.append(f"    {model_name}:")
                lines.append(f"      调用 {d['calls']} 次 | "
                             f"输入 {d.get('input_tokens',0):,} | "
                             f"输出 {d.get('output_tokens',0):,} | "
                             f"错误 {d['errors']}")
                if d.get("cost"):
                    lines.append(f"      成本 ${d['cost']:.4f}")
                if dur:
                    lines.append(f"      总耗时 {dur/1000:.0f}s")
        lines.append("")

    # 工具
    tools = report.get("tools", {})
    if tools:
        lines.append("  ── 工具执行 ──")
        lines.append(f"    工具调用: {tools.get('tool_calls', 0)} 次")
        lines.append(f"    消息总数: {tools.get('message_count', 0)} 条")
        lines.append("")

    # 上下文压缩
    comp = report.get("compaction", {})
    if comp and "error" not in comp:
        lines.append("  ── 上下文压缩 ──")
        cb_status = "已熔断" if comp.get("is_open") else "正常"
        lines.append(f"    熔断器: {cb_status}")
        lines.append(f"    累计成功: {comp.get('total_successes', 0)} 次")
        lines.append(f"    累计失败: {comp.get('total_failures', 0)} 次")
        if comp.get("consecutive_failures"):
            lines.append(f"    连续失败: {comp['consecutive_failures']} 次")
        lines.append("")

    # 审查系统
    review = report.get("review", {})
    if review:
        lines.append("  ── 审查系统 ──")
        lines.append(f"    审查模式: {review.get('mode', 'off')}")
        lines.append(f"    严格审查熔断器: {review.get('circuit_breaker', 'N/A')}")
        sub_calls = review.get("sub_agent_llm_calls")
        sub_tokens = review.get("sub_agent_tokens")
        if sub_calls is not None:
            lines.append(f"    子 Agent 调用: {sub_calls} 次")
            lines.append(f"    子 Agent Token: {sub_tokens:,}")
        lines.append("")

    lines.append("=" * 56)
    return "\n".join(lines)
