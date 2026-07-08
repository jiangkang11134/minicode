"""端到端评测 — off vs strict 对比测试。

数据集：20 个改编自 HumanEval/MBPP 的多轮+跨文件任务。
评测方法：qwen-max 对最终生成的代码打分，计算正确率。

用法：
    cd SmartCode-1 && python benchmark/run_e2e.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# ── 配置 ──
TASKS_FILE = Path(__file__).resolve().parent / "tasks" / "e2e_full.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
TIMEOUT_PER_TASK = 180
QWEN_API_KEY = "sk-e311f14034d04699bff301cb0da0f472"
QWEN_MODEL = "qwen-max"
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODES = ["off", "strict"]

RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def call_qwen(prompt: str) -> dict:
    """调用 qwen-max 并返回解析后的 JSON。"""
    data = json.dumps({
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.1,
    }).encode("utf-8")

    req = urllib.request.Request(
        QWEN_URL, data=data,
        headers={
            "Authorization": f"Bearer {QWEN_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        text = resp.read().decode()
        result = json.loads(text)
        content = result["choices"][0]["message"]["content"]
        json_match = re.search(r'\{[^}]+\}', content)
        if json_match:
            return json.loads(json_match.group())
        return {"correct": False, "score": 0, "reason": f"解析失败: {content[:100]}"}
    except Exception as e:
        return {"correct": False, "score": 0, "reason": f"API错误: {e}"}


def format_prompt(task: dict) -> str:
    """把多轮任务拼成连续的 prompt。"""
    turns = task["turns"]
    steps = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(turns))
    return (
        f"{task['description']}\n\n"
        f"请按以下步骤逐步完成，每步生成对应的文件：\n{steps}\n\n"
        f"要求：\n"
        f"- 最终生成的文件列表: {', '.join(task['test_files'])}"
    )


def collect_files(task_dir: Path) -> str:
    """收集任务目录中所有生成的 .py 文件内容。"""
    parts = []
    for f in sorted(task_dir.glob("*.py")):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            parts.append(f"# === {f.name} ===\n{content}")
        except Exception:
            pass
    return "\n\n".join(parts) if parts else "(无生成文件)"


def run_single(task: dict, mode: str, task_dir: Path) -> dict:
    """在指定模式下运行一个任务。"""
    prompt = format_prompt(task)
    env = os.environ.copy()
    env["MINICODE_REVIEW_MODE"] = mode
    env["PYTHONIOENCODING"] = "utf-8"
    # 把项目根目录加入 PYTHONPATH，确保子进程能找到 minicode 模块
    project_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")

    start = time.time()
    try:
        # 通过 stdin 传递 prompt（非 TTY 模式从 stdin 读取）
        r = subprocess.run(
            [sys.executable, "-m", "minicode.main"],
            cwd=str(task_dir),
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_PER_TASK,
        )
        elapsed = time.time() - start
        all_code = collect_files(task_dir)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        return {
            "task_id": task["id"],
            "mode": mode,
            "elapsed_s": round(elapsed, 1),
            "all_code": all_code,
            "output_preview": out[:1500],
            "error": r.returncode != 0,
            "error_msg": (r.stderr or "")[:300] if r.returncode else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "task_id": task["id"], "mode": mode,
            "elapsed_s": TIMEOUT_PER_TASK, "all_code": "(超时)",
            "output_preview": "", "error": True, "error_msg": "超时",
        }
    except Exception as e:
        return {
            "task_id": task["id"], "mode": mode,
            "elapsed_s": round(time.time() - start, 1), "all_code": f"(异常: {e})",
            "output_preview": "", "error": True, "error_msg": str(e),
        }


def score_code(task: dict, result: dict) -> dict:
    """用 qwen-max 判断生成的代码是否正确。"""
    prompt = f"""你是代码评测专家。判断以下生成的代码是否正确实现了用户需求。

## 用户需求
{task['description']}

## 需要完成的步骤
{chr(10).join(f'- {t}' for t in task['turns'])}

## 需要生成的文件
{', '.join(task['test_files'])}

## 生成的代码
{result['all_code'][:4000]}

## 判断标准
1. 是否按要求生成了所有需要的文件？
2. 代码是否可运行（语法正确）？
3. 是否正确实现了每一步的功能？

只回复 JSON 格式：
{{"correct": true/false, "score": 0-100, "reason": "一句话判断理由"}}"""
    return call_qwen(prompt)


def main():
    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    print(f"加载 {len(tasks)} 个任务\n")

    all_results = {"tasks": [], "summary": {}}

    for i, task in enumerate(tasks):
        print(f"[{i+1}/{len(tasks)}] {task['id']} ({task['source']}) [{task['category']}] {task['description'][:40]}...")

        for mode in MODES:
            task_dir = RESULTS_DIR / f"{task['id']}_{mode}"
            task_dir.mkdir(parents=True, exist_ok=True)

            r = run_single(task, mode, task_dir)
            print(f"  [{mode}] {r['elapsed_s']}s {'[FAIL]' if r['error'] else 'OK'} 代码={len(r['all_code'])}字符")

            score = score_code(task, r)
            r["score"] = score
            print(f"     评分: {'[PASS]' if score.get('correct') else '[FAIL]'} {score.get('score',0)}/100 - {score.get('reason','')[:60]}")

            all_results["tasks"].append(r)

            # 保存中间结果
            (RESULTS_DIR / "e2e_results.json").write_text(
                json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ── 汇总 ──
    off = [t for t in all_results["tasks"] if t["mode"] == "off"]
    strict = [t for t in all_results["tasks"] if t["mode"] == "strict"]
    off_correct = sum(1 for t in off if t.get("score", {}).get("correct"))
    strict_correct = sum(1 for t in strict if t.get("score", {}).get("correct"))
    total = len(tasks)

    print(f"\n{'='*60}")
    print(f"  最终报告")
    print(f"{'='*60}")
    print(f"\n  off 模式:     {off_correct}/{total} 正确 ({off_correct/total*100:.1f}%)")
    print(f"  strict 模式:  {strict_correct}/{total} 正确 ({strict_correct/total*100:.1f}%)")
    off_time = sum(t["elapsed_s"] for t in off)
    strict_time = sum(t["elapsed_s"] for t in strict)
    print(f"\n  off 总耗时:     {off_time:.0f}s")
    print(f"  strict 总耗时:  {strict_time:.0f}s")
    print(f"  额外开销:      {strict_time - off_time:.0f}s ({(strict_time-off_time)/off_time*100:.0f}%)")

    # ── 写入报告 ──
    report_path = Path(__file__).resolve().parent.parent / "docs" / "端到端评测报告.md"
    lines = [
        "# SmartCode 端到端评测报告",
        f"\n> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 评测模型: {QWEN_MODEL}",
        f"> 任务数: {total}（来自 HumanEval + MBPP 改编）",
        f"\n## 总体结果",
        f"\n| 模式 | 正确数 | 总数 | 正确率 | 总耗时 |",
        f"|:-----|:------:|:----:|:------:|:------:|",
        f"| off（无审查） | {off_correct} | {total} | {off_correct/total*100:.1f}% | {off_time:.0f}s |",
        f"| strict（审查） | {strict_correct} | {total} | {strict_correct/total*100:.1f}% | {strict_time:.0f}s |",
        f"\n## 逐任务详情\n",
    ]

    for t in all_results["tasks"]:
        s = t.get("score", {})
        c = "[PASS]" if s.get("correct") else "[FAIL]"
        lines.append(f"### {t['task_id']} ({t['mode']})")
        lines.append(f"- **正确**: {c} (分数: {s.get('score',0)})")
        lines.append(f"- **耗时**: {t['elapsed_s']}s")
        lines.append(f"- **评语**: {s.get('reason','')}")
        lines.append(f"- **代码**:")
        lines.append(f"  ```python")
        lines.append(t['all_code'][:2000])
        lines.append(f"  ```\n")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  报告已保存: {report_path}")

    all_results["summary"] = {
        "off": {"correct": off_correct, "total": total, "rate": f"{off_correct/total*100:.1f}%"},
        "strict": {"correct": strict_correct, "total": total, "rate": f"{strict_correct/total*100:.1f}%"},
    }
    (RESULTS_DIR / "e2e_results.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("  结果已保存: benchmark/results/e2e_results.json")


if __name__ == "__main__":
    main()
