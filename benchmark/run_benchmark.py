"""审查系统基准测试 — 数据驱动的性能与准确性评测。

用法：
    cd SmartCode-1 && python benchmark/run_benchmark.py
"""

from __future__ import annotations

import glob
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minicode.review.hooks import _pre_review_content
from minicode.tools.import_map import build_import_map

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_meta(path: Path) -> dict[str, str]:
    """解析样本头部 @ 注释。"""
    meta = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.startswith("# @"):
                break
            raw = line.strip("# \n\r")
            if ": " in raw:
                k, v = raw.split(": ", 1)
                k = k.lstrip("@")  # Remove @ prefix from key
                meta[k] = v
    return meta


def load_samples(cat: str) -> list[dict]:
    samples = []
    for path in sorted(glob.glob(str(SAMPLES_DIR / cat / "*.py"))):
        p = Path(path)
        meta = parse_meta(p)
        content = p.read_text(encoding="utf-8")
        sev = meta.get("severity", "")
        rule = meta.get("rule_id", "")
        exp_issues = int(meta.get("expected_issues", "1"))
        skip = meta.get("expected_skip", "false") == "true"
        samples.append(dict(
            path=str(p.relative_to(REPO_ROOT)),
            cat=cat,
            content=content,
            exp_sev=sev,
            exp_rule=rule,
            exp_issues=exp_issues,
            exp_skip=skip,
        ))
    return samples


def test_one(s: dict) -> dict:
    start = time.perf_counter()
    issues = _pre_review_content(s["content"], str(s["path"]))
    ms = round((time.perf_counter() - start) * 1000, 1)
    rules = set(i["rule_id"] for i in issues)
    n = len(issues)

    # 判断是否命中预期
    if s["exp_skip"]:
        hit = (n == 0)
    elif s["exp_rule"] and s["exp_issues"] > 0:
        hit = s["exp_rule"] in rules
    elif s["exp_issues"] == 0:
        hit = (n == 0)
    else:
        hit = (n > 0)

    return dict(path=s["path"], issues=issues, n=n, ms=ms, hit=hit,
                rules=rules, exp_rule=s["exp_rule"])


def test_import_map() -> dict:
    t0 = time.time()
    data = build_import_map(str(SAMPLES_DIR / "cross_file"))
    t = round(time.time() - t0, 2)
    sym = data.get("symbols", {})
    a = sym.get("authenticate", {})
    return dict(time=t, count=len(sym),
                has_auth="authenticate" in sym,
                auth_type=a.get("type", ""),
                auth_refs=a.get("referenced_by", []))


def main():
    all_samples = []
    for cat in ["security", "false_positive", "clean"]:
        ss = load_samples(cat)
        all_samples.extend(ss)
        print(f"  {cat}: {len(ss)} samples")
    print()

    results = []
    for i, s in enumerate(all_samples):
        r = test_one(s)
        results.append(r)
        fname = str(s["path"]).replace("\\", "/").split("/")[-1]
        tag = "OK" if r["hit"] else "XX"
        extra = f"  found={','.join(sorted(r['rules']))}" if r["issues"] else ""
        print(f"  [{tag}] ({i+1}/{len(all_samples)}) {fname:35s} {r['ms']:5.1f}ms{extra}")

    # 按类别汇总
    by_cat = {}
    for r in results:
        parts = str(r["path"]).replace("\\", "/").split("/")
        by_cat.setdefault(parts[2], []).append(r)

    # ---- 报告 ----
    bar = "=" * 62
    print(f"\n  {bar}")
    print(f"  审查系统基准测试报告")
    print(f"  {bar}")

    for cat_name, cat_results in sorted(by_cat.items()):
        hits = sum(1 for r in cat_results if r["hit"])
        avg_ms = sum(r["ms"] for r in cat_results) / len(cat_results) if cat_results else 0
        print(f"\n  [{cat_name}]  {hits}/{len(cat_results)} pass, avg {avg_ms:.2f}ms")

        for r in cat_results:
            fname = str(r["path"]).replace("\\", "/").split("/")[-1]
            tag = "OK" if r["hit"] else "XX"
            exp = r["exp_rule"] if r["exp_rule"] else "-"
            got = ",".join(sorted(r["rules"])) if r["issues"] else "(clean)"
            print(f"    {tag}  {fname:35s}  expect={exp:20s}  got={got}")

    # 综合指标
    sec = by_cat.get("security", [])
    cln = by_cat.get("clean", [])
    fp  = by_cat.get("false_positive", [])
    tp  = sum(1 for r in sec if r["hit"])
    fn  = sum(1 for r in sec if not r["hit"])
    fp_count = sum(1 for r in cln if r["n"] > 0)
    tn = sum(1 for r in cln if r["n"] == 0)
    precision = tp / (tp + fp_count) * 100 if (tp + fp_count) > 0 else 0
    recall = tp / (tp + fn) * 100 if (tp + fn) > 0 else 0
    total_ms = sum(r["ms"] for r in results)
    fp_skip = sum(1 for r in fp if r["hit"])

    print(f"\n  [综合指标]")
    print(f"    Precision:        {precision:5.1f}%   ({tp}/{tp+fp_count})")
    print(f"    Recall:           {recall:5.1f}%   ({tp}/{tp+fn})")
    print(f"    Avg Latency:      {total_ms/len(results):.2f}ms   ({len(results)} samples)")
    print(f"    False Positives:  {fp_count}   (clean code flagged)")
    print(f"    FP Prefix Skip:   {fp_skip}/{len(fp)}")

    # import map
    im = test_import_map()
    print(f"\n  [Import Map]")
    print(f"    Build time: {im['time']}s")
    print(f"    Symbols extracted: {im['count']}")
    if im["has_auth"]:
        print(f"    authenticate type:   {im['auth_type']}")
        print(f"    authenticate refs:   {im['auth_refs']}")

    print(f"\n  {bar}")
    print(f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {bar}\n")


if __name__ == "__main__":
    main()
