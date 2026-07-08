"""完全验证：20个复杂任务（多轮+跨文件），off vs strict 对比。"""
from __future__ import annotations
import json, os, re, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicode.agent_loop import run_agent_turn
from minicode.config import load_runtime_config
from minicode.model_registry import create_model_adapter
from minicode.tools import create_default_tool_registry

QWEN_KEY = "sk-e311f14034d04699bff301cb0da0f472"

def call_qwen(code: str, prompt: str) -> dict:
    data = json.dumps({"model": "qwen-max", "messages": [{"role": "user", "content": f'''判断以下代码是否正确实现了需求。只回复JSON。\n\n需求: {prompt}\n\n生成的代码:\n```python\n{code[:3000]}\n```\n\n{{"correct": true/false, "score": 0-100, "reason": "一句话"}}'''}], "max_tokens": 200, "temperature": 0.1}).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", data, {"Authorization": f"Bearer {QWEN_KEY}", "Content-Type": "application/json"}), timeout=30)
        text = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
        m = re.search(r'\{[^}]+\}', text)
        return json.loads(m.group()) if m else {"correct": False, "score": 0, "reason": "parse error"}
    except Exception as e:
        return {"correct": False, "score": 0, "reason": str(e)[:80]}

def format_prompt(task: dict) -> str:
    """根据任务类型生成不同的 prompt。

    multi-turn: 所有步骤都在同一个文件中逐步增加功能
    multi-file: 涉及多个文件，每个文件有自己的步骤
    """
    steps = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(task["steps"]))

    if task["category"] == "multi-turn":
        files = ", ".join(task["test_files"])
        return (
            f"{task['description']}\n\n"
            f"所有代码写在 {files} 中，逐步修改这个文件增加功能。\n\n"
            f"步骤：\n{steps}\n\n"
            f"每次修改时用 edit_file 工具更新已有文件，不要从头重写。\n"
            f"用 write_file 创建新文件，用 edit_file 修改已有文件。"
        )
    else:
        return (
            f"{task['description']}\n\n"
            f"需要创建多个文件。请按步骤逐个创建：\n{steps}\n\n"
            f"最终需要生成的文件: {', '.join(task['test_files'])}\n\n"
            f"用 write_file 工具创建文件。"
        )

tasks = json.load(open(ROOT / "benchmark" / "tasks" / "e2e_full.json", encoding="utf-8"))
selected = tasks  # 全部 20 个任务

results = []
for task in selected:
    print(f"\n{'='*60}")
    print(f"  {task['id']} [{task['category']}] {task['description']}")

    for mode in ["off", "strict"]:
        print(f"\n  == 模式: {mode} ==")

        for f in task["test_files"]:
            p = ROOT / f
            if p.exists(): p.unlink()

        # 设置环境变量
        os.environ["MINICODE_REVIEW_MODE"] = mode
        os.environ["CUSTOM_API_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        os.environ["CUSTOM_API_KEY"] = "sk-e311f14034d04699bff301cb0da0f472"
        os.environ["ANTHROPIC_MODEL"] = "deepseek-v4-flash"

        runtime = load_runtime_config()
        tools = create_default_tool_registry(str(ROOT), runtime=runtime)
        model = create_model_adapter(model='deepseek-v4-flash', tools=tools, runtime=runtime)

        prompt = format_prompt(task)
        sys_prompt = "你是编码助手。用 write_file 工具创建文件，用 edit_file 修改已有文件。"
        start = time.time()
        try:
            result = run_agent_turn(
                model=model, tools=tools,
                messages=[{"role": "system", "content": sys_prompt},
                          {"role": "user", "content": prompt}],
                cwd=str(ROOT), max_steps=10,
            )
            elapsed = time.time() - start

            code = ""
            for f in task["test_files"]:
                p = ROOT / f
                if p.exists():
                    try: code += f"# === {f} ===\n{p.read_text(encoding='utf-8', errors='replace')}\n\n"
                    except: pass
                    p.unlink()

            res = {"id": task["id"], "mode": mode, "elapsed_s": round(elapsed, 1),
                   "code": code or "(无文件)", "error": False}
            score = call_qwen(res["code"], task["description"])
            res["score"] = score
            print(f"  耗时: {elapsed:.0f}s  file={'YES' if code else 'NO':3s}  score={score.get('score',0)}")
            results.append(res)

        except Exception as e:
            elapsed = time.time() - start
            print(f"  异常 ({elapsed:.0f}s): {e}")
            results.append({"id": task["id"], "mode": mode, "elapsed_s": round(elapsed, 1),
                           "code": f"({e})", "error": True,
                           "score": {"correct": False, "score": 0, "reason": str(e)[:80]}})

    for f in task["test_files"]:
        p = ROOT / f
        if p.exists(): p.unlink()

# ── 保存结果 ──
json.dump(results, open(ROOT / "benchmark" / "results_full.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ── 生成报告 ──
off = [r for r in results if r["mode"] == "off"]
strict = [r for r in results if r["mode"] == "strict"]
off_c = sum(1 for r in off if r.get("score", {}).get("correct"))
st_c = sum(1 for r in strict if r.get("score", {}).get("correct"))
off_t = sum(r["elapsed_s"] for r in off)
st_t = sum(r["elapsed_s"] for r in strict)
total = len(tasks)

print(f"\n{'='*60}")
print(f"  批量测试完成")
print(f"{'='*60}")
print(f"\n  off 模式:     {off_c}/{total} 正确 ({off_c/total*100:.1f}%)  总耗时 {off_t:.0f}s")
print(f"  strict 模式:  {st_c}/{total} 正确 ({st_c/total*100:.1f}%)  总耗时 {st_t:.0f}s")
print(f"")

lines = [
    f"# SmartCode 端到端评测报告（完整版）",
    f"\n> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    f"> 模型: deepseek-v4-flash (DashScope)",
    f"> 任务数: {total}（多轮+跨文件）",
    f"\n## 总体结果",
    f"\n| 模式 | 正确数 | 总数 | 正确率 | 总耗时 |",
    f"|:-----|:------:|:----:|:------:|:------:|",
    f"| off | {off_c} | {total} | {off_c/total*100:.1f}% | {off_t:.0f}s |",
    f"| strict | {st_c} | {total} | {st_c/total*100:.1f}% | {st_t:.0f}s |",
    f"\n## 逐任务详情\n",
]
for r in results:
    s = r.get("score", {})
    c = "PASS" if s.get("correct") else "FAIL"
    lines.append(f"### {r['id']} ({r['mode']})")
    lines.append(f"- **结果**: {c} (分数: {s.get('score',0)})")
    lines.append(f"- **耗时**: {r['elapsed_s']}s")
    lines.append(f"- **评语**: {s.get('reason','')[:200]}")
    lines.append(f"- **代码**:")
    lines.append(f"  ```python")
    lines.append(r['code'][:1500] if len(r['code']) > 30 else r['code'])
    lines.append(f"  ```\n")

report_path = ROOT / "docs" / "端到端评测完整版.md"
report_path.write_text("\n".join(lines), encoding="utf-8")
print(f"  报告已保存: {report_path}")
print(f"  结果已保存: benchmark/results_full.json")
