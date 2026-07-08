"""简化端到端评测 — 10 个单轮单文件任务，off vs strict。"""
from __future__ import annotations
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

TASKS = json.loads((Path(__file__).resolve().parent / "tasks" / "e2e_simple.json").read_text(encoding="utf-8"))
ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results_simple"
QWEN_KEY = "sk-e311f14034d04699bff301cb0da0f472"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def call_qwen(code: str, desc: str) -> dict:
    data = json.dumps({"model": "qwen-max", "messages": [{"role": "user", "content": f'''判断以下代码是否正确实现了需求。只回复JSON。\n\n需求: {desc}\n\n代码:\n```python\n{code[:2000]}\n```\n\n{{\\"correct\\": true/false, \\"score\\": 0-100, \\"reason\\": "一句话"}}'''}], "max_tokens": 200, "temperature": 0.1}).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", data, {"Authorization": f"Bearer {QWEN_KEY}", "Content-Type": "application/json"}), timeout=30)
        text = resp.read().decode()
        content = json.loads(text)["choices"][0]["message"]["content"]
        m = re.search(r'\{[^}]+\}', content)
        return json.loads(m.group()) if m else {"correct": False, "score": 0, "reason": "parse error"}
    except Exception as e:
        return {"correct": False, "score": 0, "reason": str(e)[:80]}

def run_one(task: dict, mode: str, task_dir: Path) -> dict:
    env = os.environ.copy()
    env["MINICODE_REVIEW_MODE"] = mode; env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 阿里云百炼 DashScope
    env["CUSTOM_MODEL"] = "deepseek-v4-flash"
    env["CUSTOM_API_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    env["CUSTOM_API_KEY"] = "sk-e311f14034d04699bff301cb0da0f472"
    env["MINICODE_REVIEW_SUB_MODEL"] = "deepseek-v4-flash"
    env["MINICODE_REVIEW_SUB_API_KEY"] = "sk-e311f14034d04699bff301cb0da0f472"
    env["MINICODE_REVIEW_SUB_API_BASE"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    task_dir.mkdir(parents=True, exist_ok=True)
    # 先把目标文件放到 project root（模型更擅长写当前目录）
    target_file = ROOT / task["file"]
    desc_with_path = task["desc"] + f"\n\n用 write_file 工具创建文件 {task['file']}，确保写入完整代码。不要用 run_command。文件名: {target_file}"
    start = time.time()
    try:
        r = subprocess.run([sys.executable, "-m", "minicode.main"], cwd=str(ROOT), env=env,
            input=desc_with_path, capture_output=True, text=True, timeout=120)
        elapsed = time.time() - start
        code = ""
        for f in ROOT.glob(task["file"]):
            try: code += f"# === {f.name} ===\n{f.read_text(encoding='utf-8', errors='replace')}\n"
            except: pass
            # move to results dir for record keeping
            import shutil
            shutil.move(str(f), str(task_dir / f.name))
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        return {"id": task["id"], "mode": mode, "elapsed_s": round(elapsed, 1),
                "code": code[:3000] or "(无文件)", "output": out[:800], "error": bool(r.returncode)}
    except subprocess.TimeoutExpired:
        return {"id": task["id"], "mode": mode, "elapsed_s": 120, "code": "(超时)", "output": "", "error": True}
    except Exception as e:
        return {"id": task["id"], "mode": mode, "elapsed_s": round(time.time()-start, 1), "code": f"({e})", "output": "", "error": True}

def main():
    all_results = []
    for i, task in enumerate(TASKS):
        print(f"\n[{i+1}/{len(TASKS)}] {task['id']} {task['desc'][:40]}...")
        for mode in ["off", "strict"]:
            td = RESULTS_DIR / f"{task['id']}_{mode}"
            r = run_one(task, mode, td)
            print(f"  [{mode}] {r['elapsed_s']}s code={len(r['code'])}字符")
            score = call_qwen(r["code"], task["desc"])
            r["score"] = score
            print(f"    评分: {score.get('score',0)}/100 correct={score.get('correct')}")
            all_results.append(r)
            (RESULTS_DIR / "results.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    off = [t for t in all_results if t["mode"]=="off"]
    st = [t for t in all_results if t["mode"]=="strict"]
    off_c = sum(1 for t in off if t.get("score",{}).get("correct"))
    st_c = sum(1 for t in st if t.get("score",{}).get("correct"))
    t_n = len(TASKS)

    report = f'''# SmartCode 端到端评测报告（简化版）
> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}
> 评测模型: qwen-max
> 任务数: {t_n}（单轮单文件）

## 总体结果

| 模式 | 正确数 | 总数 | 正确率 | 总耗时 |
|:-----|:------:|:----:|:------:|:------:|
| off（无审查） | {off_c} | {t_n} | {off_c/t_n*100:.1f}% | {sum(t["elapsed_s"] for t in off):.0f}s |
| strict（审查） | {st_c} | {t_n} | {st_c/t_n*100:.1f}% | {sum(t["elapsed_s"] for t in st):.0f}s |

## 逐任务详情

'''
    for r in all_results:
        s = r.get("score",{})
        report += f'''### {r["id"]} ({r["mode"]})
- **正确**: {"[PASS]" if s.get("correct") else "[FAIL]"} (分数: {s.get("score",0)})
- **耗时**: {r["elapsed_s"]}s
- **评语**: {s.get("reason","")}
- **代码**:
  ```python
{r["code"][:1000]}
  ```
'''
    (ROOT / "docs" / "端到端评测简单版.md").write_text(report, encoding="utf-8")
    print(f"\n报告已保存")

if __name__ == "__main__":
    main()
