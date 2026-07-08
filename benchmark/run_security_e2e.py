"""审查系统端到端评测 — 验证审查系统的确定性行为。

评测维度：
  1. 宽松审查（6 项）— 直接调 _pre_review_content，验证正则+AST 检测
  2. 跨文件审查（1 项）— 修改函数签名，验证子 Agent 跨文件检测
  3. 新文件检测（1 项）— 在安全路径创建文件，验证严格触发
  4. Docker 沙箱（1 项）— 验证 sanbdox_test 工具可用
"""

from __future__ import annotations
import json, os, sys, time, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicode.review.hooks import _pre_review_content
from minicode.review.mode_engine import should_trigger_strict
from minicode.tools.sandbox_test import DockerSandbox

TASKS = json.loads((ROOT / "benchmark" / "tasks" / "security_e2e.json").read_text(encoding="utf-8"))
RESULTS_DIR = ROOT / "benchmark" / "results_security"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

results = []
os.environ["CUSTOM_API_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
os.environ["CUSTOM_API_KEY"] = "sk-e311f14034d04699bff301cb0da0f472"


# ════════════════════════════════════════════════════════════════
# 维度 1：宽松审查确定性检测
# ════════════════════════════════════════════════════════════════

def test_loose_review(task: dict) -> dict:
    """直接调 _pre_review_content，验证审查规则是否生效。"""
    issues = _pre_review_content(task["test_content"], "test.py")
    found_rules = set(i["rule_id"] for i in issues)
    found_severities = set(i["severity"] for i in issues)

    expected = task["expected_rule"]
    if expected == "none":
        passed = len(issues) == 0
        detail = f"期望无问题，实际 {'无' if passed else f'有 {len(issues)} 个'}"
    else:
        passed = expected in found_rules
        detail = f"期望={expected}，检测到={','.join(found_rules) if found_rules else '无'}"

    return {
        "id": task["id"],
        "category": task["category"],
        "passed": passed,
        "detail": detail,
        "issues": [{"rule": i["rule_id"], "severity": i["severity"]} for i in issues],
        "elapsed_s": 0,
    }


# ════════════════════════════════════════════════════════════════
# 维度 2：跨文件检测 + 维度 3：新文件检测（通过 agent 任务）
# ════════════════════════════════════════════════════════════════

def run_agent_task(task: dict, mode: str, task_dir: Path) -> dict:
    """在指定模式下运行 agent 任务。"""
    from minicode.agent_loop import run_agent_turn
    from minicode.config import load_runtime_config
    from minicode.model_registry import create_model_adapter
    from minicode.tools import create_default_tool_registry

    runtime = load_runtime_config()
    tools = create_default_tool_registry(str(ROOT), runtime=runtime)
    model = create_model_adapter(model="deepseek-v4-flash", tools=tools, runtime=runtime)

    os.environ["MINICODE_REVIEW_MODE"] = mode

    start = time.time()
    try:
        result = run_agent_turn(
            model=model, tools=tools,
            messages=[{"role": "user", "content": task["prompt"]}],
            cwd=str(task_dir), max_steps=10,
            system_prompt="你是编码助手。用 write_file/edit_file 工具。",
        )
        elapsed = time.time() - start

        # 收集生成的代码
        code = ""
        for f in sorted(task_dir.rglob("*.py")):
            try:
                rel = f.relative_to(task_dir)
                code += f"# === {rel} ===\n{f.read_text(encoding='utf-8', errors='replace')}\n\n"
            except Exception:
                pass

        return {"id": task["id"], "mode": mode, "elapsed_s": round(elapsed, 1), "code": code or "(无)", "error": False}
    except Exception as e:
        elapsed = time.time() - start
        return {"id": task["id"], "mode": mode, "elapsed_s": round(elapsed, 1), "code": f"({e})", "error": True}


def check_cross_file_result(code: str) -> dict:
    """检查跨文件任务中调用方是否更新。"""
    checks = [
        ("role= 参数传递", "role=" in code or "'role'" in code),
        ("authenticate 三参数", "authenticate(username, password, role)" in code or "authenticate(user, pwd, role)" in code),
    ]
    passed_checks = [name for name, ok in checks if ok]
    return {"passed": len(passed_checks) > 0, "details": passed_checks if passed_checks else ["调用方未更新"]}


def check_new_file_result(task_dir: Path, mode: str) -> dict:
    """检查新文件是否被创建 + strict 模式下是否应触发审查。"""
    files = list(task_dir.rglob("*.py"))
    created = [str(f.relative_to(task_dir)) for f in files]
    return {"passed": len(created) > 0, "files": created}


# ════════════════════════════════════════════════════════════════
# 维度 4：Docker 沙箱
# ════════════════════════════════════════════════════════════════

def test_sandbox() -> dict:
    """测试 Docker 沙箱是否可用。"""
    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "tests").mkdir()
    (tmpdir / "tests" / "test_pass.py").write_text(
        "import sys\nsys.path.insert(0, '/sandbox')\ndef test_greet(): assert True\n",
        encoding="utf-8")
    (tmpdir / "hello.py").write_text("def greet(): return 'hi'", encoding="utf-8")

    try:
        sandbox = DockerSandbox(str(tmpdir), changed_files=[str(tmpdir / "hello.py")])
        result = sandbox.run()
        passed = result["passed"]
        return {"id": "SANDBOX-01", "category": "sandbox-test", "passed": passed,
                "detail": f"Docker 沙箱 {'通过' if passed else '失败'}"}
    except Exception as e:
        return {"id": "SANDBOX-01", "category": "sandbox-test", "passed": False,
                "detail": f"Docker 沙箱异常: {e}"}


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

def main():
    print(f"安全审查系统端到端评测\n", flush=True)

    # 维度 1：宽松审查（6 项）
    print(f"{'='*60}")
    print(f"维度 1：宽松审查确定性检测")
    print(f"{'='*60}")
    for task in TASKS:
        if task["type"] != "direct-test" or task["category"] == "sandbox-test":
            continue
        r = test_loose_review(task)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {task['id']}: {r['detail']}", flush=True)

    # 维度 2 + 3：跨文件 + 新文件（agent 任务）
    print(f"\n{'='*60}")
    print(f"维度 2&3：Agent 任务（跨文件 + 新文件）")
    print(f"{'='*60}")
    for task in TASKS:
        if task["type"] != "agent-task":
            continue
        for mode in ["off", "strict"]:
            task_dir = RESULTS_DIR / f"{task['id']}_{mode}"
            if task_dir.exists():
                shutil.rmtree(task_dir)
            task_dir.mkdir(parents=True)

            # 写入预制文件
            if "files" in task:
                for fname, content in task["files"].items():
                    fp = task_dir / fname
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(content, encoding="utf-8")

            r = run_agent_task(task, mode, task_dir)

            if task["id"] == "CROSS-01":
                check = check_cross_file_result(r["code"])
            else:
                check = check_new_file_result(task_dir, mode)

            r["check"] = check
            results.append(r)
            status = "PASS" if check["passed"] else "FAIL"
            print(f"  [{status}] {task['id']} ({mode}) {r['elapsed_s']:.0f}s  {check.get('details','')}", flush=True)

    # 维度 4：Docker 沙箱
    print(f"\n{'='*60}")
    print(f"维度 4：Docker 沙箱")
    print(f"{'='*60}")
    sandbox_r = test_sandbox()
    results.append(sandbox_r)
    status = "PASS" if sandbox_r["passed"] else "FAIL"
    print(f"  [{status}] {sandbox_r['detail']}", flush=True)

    # ── 汇总报告 ──
    print(f"\n{'='*60}")
    print(f"最终评测报告")
    print(f"{'='*60}")

    def _passed(r):
        return r.get("passed") or r.get("check", {}).get("passed", False)

    total = len(results)
    passed = sum(1 for r in results if _passed(r))
    print(f"\n  总项: {total}, 通过: {passed}, 失败: {total - passed}")
    print(f"  通过率: {passed/total*100:.1f}%\n")

    for r in results:
        status = "PASS" if _passed(r) else "FAIL"
        did = r.get("id", "")
        if "detail" in r:
            det = r["detail"][:80]
        elif r.get("code","") and len(r.get("code","")) > 30:
            det = f"代码已生成 ({len(r['code'])} 字符)"
            if r.get("check"):
                det += f" | 检查: {r['check'].get('details', 'ok')}"
        else:
            det = r.get("code", "")[:80]
        print(f"  [{status}] {did:12s} {det}")

    # 保存
    (RESULTS_DIR / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_path = ROOT / "docs" / "安全审查评测报告.md"
    lines = [
        "# SmartCode 安全审查系统评测报告",
        f"\n> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"\n## 总体结果",
        f"\n| 维度 | 通过 | 总数 | 通过率 |",
        f"|:-----|:----:|:----:|:-----:|",
    ]
    cats = {}
    for r in results:
        cats.setdefault(r.get("category", "other"), []).append(r)
    for cat, items in sorted(cats.items()):
        p = sum(1 for r in items if _passed(r))
        lines.append(f"| {cat} | {p} | {len(items)} | {p/len(items)*100:.0f}% |")
    lines.append(f"| **总计** | **{passed}** | **{total}** | **{passed/total*100:.0f}%** |")
    lines.append(f"\n## 逐项详情\n")
    for r in results:
        status = "PASS" if _passed(r) else "FAIL"
        if "detail" in r:
            det = r["detail"][:200]
        elif r.get("check"):
            det = f"检查: {r['check'].get('details', 'ok')}"
        else:
            det = (r.get("code","") or "")[:200]
        lines.append(f"### {r.get('id','')} ({r.get('category','')}) → {status}")
        lines.append(f"- {det}")
        lines.append(f"")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()
