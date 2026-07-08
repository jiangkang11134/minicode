"""审查系统钩子 — agent_loop_lite.py 的 3 处插入点。

钩子 1: on_before_write — write/edit/patch 前的宽松预审查（阻断写入或放行）
钩子 2: on_file_written — write/edit/patch 成功后更新 import map + 触发严格审查
钩子 3: on_turn_end — Coda 阶段注入累积发现 + 沉淀重要发现
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from minicode.review.config import (
    get_review_mode,
    SUB_AGENT_MODEL,
    SUB_AGENT_API_KEY,
    SUB_AGENT_API_BASE,
    IMPORT_MAP_DIR,
    IMPORT_MAP_FILE,
)

logger = logging.getLogger("minicode.review")

# 线程锁：保护 _fp_cache 并发访问（Defect 7）
_fp_cache_lock = threading.Lock()


def get_review_hooks(cwd: str, tools=None, tool_context=None):
    """工厂函数 — 在 agent_loop.py 开头调用。

    总是返回 ReviewHooks 实例（永不返回 None），因为实例内部做模式判断。
    off 模式下的调用都是 no-op，但实例会持续检测模式切换。

    try/except ImportError 兜底：
        try:
            from minicode.review.hooks import get_review_hooks
            _review_hooks = get_review_hooks(cwd, tools, tool_context)
        except ImportError:
            _review_hooks = None

    ⚠️ 不能在这里检查 get_review_mode() == "off" 就返回 None，
       否则 off→strict 切换后没有实例来检测过渡。
    """
    from minicode.review.hooks import ReviewHooks
    return ReviewHooks(cwd, tools, tool_context)


class ReviewHooks:
    """审查钩子集合。

    通过 tools/tool_context 引用，可以直接调 task 子 Agent，
    不依赖主 Agent 的 LLM 决定。

    模式切换支持：
      - __init__ 时 mode=off 也创建实例（保持存在以检测过渡）
      - 每个钩子调用时通过 get_review_mode() 实时读取当前模式
      - 检测到 off→loose/strict 过渡 → 触发全面回补扫描
    """

    def __init__(self, cwd: str, tools=None, tool_context=None):
        self.cwd = cwd
        self.tools = tools
        self.tool_context = tool_context
        self._coda_findings: list[dict[str, Any]] = []
        self._coda_lock = threading.Lock()           # 保护 _coda_findings 并发写入
        self._background_threads: list[threading.Thread] = []  # 后台子 Agent 线程追踪
        self._import_map_thread: threading.Thread | None = None  # Defect 3: 后台线程追踪
        self._prev_mode: str | None = None   # 模式切换检测（记录上一次模式）
        self._retro_scan_done: bool = False  # 过渡后只扫描一次
        self._new_files: set[str] = set()    # 跟踪本轮新创建的文件（用于新人代码触发条件）

        # 惰性建表（只在 loose/strict 模式下执行）
        self._init_import_map()

    # -----------------------------------------------------------------------
    # 模式切換檢測
    # -----------------------------------------------------------------------

    def _check_transition(self) -> bool:
        """检测是否从 off 切换到了 loose/strict。

        需要在每个钩子入口处调用。
        返回 True 表示刚刚完成过渡，on_before_write 的调用方可以降级为提示而非阻断。

        流程：
          ① 读当前模式（get_review_mode）
          ② 如果 prev_mode=None → 首次初始化，记录即可
          ③ 如果 prev_mode=off 且当前在 loose/strict → 过渡！
             - 启动全面回补扫描
             - 标记 _retro_scan_done=True（只扫一次）
          ④ 记录当前模式到 _prev_mode
        """
        current = get_review_mode()

        if self._prev_mode is None:
            # 首次初始化
            self._prev_mode = current
            return False

        if self._prev_mode == "off" and current in ("loose", "strict") and not self._retro_scan_done:
            self._retro_scan_done = True
            self._prev_mode = current
            # 过渡检测到！启动全面回补扫描
            logger.info("=== 检测到审查模式切换: off → %s，启动全面回补扫描 ===", current)
            self._init_retroactive_scan()
            return True

        if self._prev_mode != current:
            # 其他模式切换（strict↔loose），不需要回补扫描
            logger.info("审查模式切换: %s → %s", self._prev_mode, current)
            self._prev_mode = current

        return False

    def _init_retroactive_scan(self) -> None:
        """全面回补扫描：对已有代码做一次性审查。

        在检测到 off→loose/strict 过渡时触发。
        后台线程执行，不阻塞主循环。结果在 Coda 阶段注入。
        """
        threading.Thread(target=self._retroactive_scan, daemon=True).start()

    def _retroactive_scan(self) -> None:
        """回补扫描的具体逻辑：建表 + 全量审查。

        执行步骤：
          ① 如果 import map 还没建，全量建表
          ② 遍历项目所有 .py 文件，对每个文件运行 _pre_review_content
          ③ 发现的问题汇总为报告，存入 _coda_findings
          ④ 严重问题同步沉淀到 skill/记忆
        """
        import glob as py_glob

        logger.info("全面回补扫描开始...")

        # 1. 全量建 import map（如果还没建）
        import_map_path = Path(self.cwd) / IMPORT_MAP_DIR / IMPORT_MAP_FILE
        if not import_map_path.exists():
            try:
                from minicode.tools.import_map import build_import_map
                build_import_map(self.cwd)
                logger.info("回补扫描：import map 已建")
            except Exception as exc:
                logger.warning("回补扫描：import map 建表失败: %s", exc)

        # 2. 全量扫描所有 .py 文件
        all_findings: list[dict[str, Any]] = []
        scanned = 0
        for py_file in py_glob.glob(f"{self.cwd}/**/*.py", recursive=True):
            try:
                content = Path(py_file).read_text(encoding="utf-8", errors="replace")
                rel_path = str(Path(py_file).relative_to(self.cwd))
                file_findings = _pre_review_content(content, rel_path)
                if file_findings:
                    all_findings.extend(file_findings)
                scanned += 1
            except (OSError, UnicodeDecodeError):
                continue

        logger.info("回补扫描完成：扫描 %d 文件，发现 %d 个问题", scanned, len(all_findings))

        # 3. 整理报告
        if all_findings:
            critical = [f for f in all_findings if f["severity"] in ("critical", "major")]
            minor = [f for f in all_findings if f["severity"] in ("minor", "suggestion")]

            report_lines = [
                f"[Retroactive Scan] 全面回补扫描完成：扫描 {scanned} 个文件，"
                f"发现 {len(critical)} 个严重问题，{len(minor)} 个建议。"
            ]
            if critical:
                report_lines.append("\n⚠️  严重问题（需处理）：")
                for f in critical[:30]:
                    report_lines.append(f"  {f['file_path']}:L{f['line']} [{f['severity']}] {f['message']}")
                if len(critical) > 30:
                    report_lines.append(f"  ... 还有 {len(critical) - 30} 个")
            if minor:
                report_lines.append("\n💡 建议：")
                for f in minor[:10]:
                    report_lines.append(f"  {f['file_path']}:L{f['line']} {f['message']}")
                if len(minor) > 10:
                    report_lines.append(f"  ... 还有 {len(minor) - 10} 个")

            self._coda_findings.append({
                "role": "system",
                "content": "\n".join(report_lines),
            })

            # 4. 沉淀严重问题
            if critical:
                try:
                    from minicode.review.promotion import promote_findings
                    promote_findings(critical, self.cwd)
                except Exception:
                    pass
        else:
            logger.info("回补扫描完成：已有代码未发现问题 ✅")

    # -----------------------------------------------------------------------
    # 惰性建表
    # -----------------------------------------------------------------------

    def _init_import_map(self) -> None:
        """后台线程建 import map，不阻塞 agent 启动。
        只在 loose/strict 模式下执行（off 模式跳过）。
        """
        if get_review_mode() == "off":
            return
        import_map_path = Path(self.cwd) / IMPORT_MAP_DIR / IMPORT_MAP_FILE
        if import_map_path.exists():
            return

        def _build():
            try:
                from minicode.tools.import_map import build_import_map
                start = time.time()
                build_import_map(self.cwd)
                elapsed = int((time.time() - start) * 1000)
                logger.info("Import map built in %d ms (background)", elapsed)
            except Exception as exc:
                logger.warning("Background import map build failed: %s", exc)

        threading.Thread(target=_build, daemon=True).start()

    # -----------------------------------------------------------------------
    # 钩子 1 — 写前预审查（宽松审查，阻断写入）
    # -----------------------------------------------------------------------

    def on_before_write(
        self, tool_name: str, tool_input: dict[str, Any]
    ):
        """write/edit/patch 执行前调用。

        返回 ToolResult(ok=False) 阻断写入，或 None 放行。

        ⚠️ 模式切换检测在每个钩子入口处执行：
           off→loose/strict 过渡时启动全面回补扫描（不阻断当前写入）。
        """
        # 模式切换检测（off→loose/strict 过渡）
        self._check_transition()

        # off 模式：不执行审查，直接放行
        if get_review_mode() == "off":
            return None

        from minicode.tooling import ToolResult

        content = tool_input.get("content") or tool_input.get("new_string", "")
        file_path = tool_input.get("file_path") or tool_input.get("path", "")

        # 跟踪新创建的文件（硬性检查文件是否已存在）
        if file_path and not Path(file_path).exists():
            self._new_files.add(file_path)

        if not content.strip():
            return None

        start = time.time()
        issues = _pre_review_content(content, file_path)
        elapsed_ms = int((time.time() - start) * 1000)

        critical = [i for i in issues if i["severity"] in ("critical", "major")]
        minor = [i for i in issues if i["severity"] in ("minor", "suggestion")]

        # 日志
        logger.info("review.pre_check", extra={
            "file": file_path,
            "tool": tool_name,
            "blocked": bool(critical),
            "critical_count": len(critical),
            "minor_count": len(minor),
            "duration_ms": elapsed_ms,
        })

        # 阻断：critical/major 问题
        if critical:
            force = tool_input.get("force", False) or tool_input.get("force_write", False)
            if force:
                _record_false_positive(file_path, critical)
                return None
            detail = "\n".join(
                f"  L{i['line']} [{i['severity']}] {i['message']}"
                for i in critical[:5]
            )
            return ToolResult(
                ok=False,
                output=(
                    f"[Pre-review Blocked] 已阻止写入 {file_path}：\n{detail}\n\n"
                    f"如需强制写入，添加 force=true 参数。"
                ),
            )

        # 放行：附带 minor 提示
        if minor:
            return ToolResult(
                ok=True,
                output=(
                    "[Pre-review OK] 写入已放行。建议关注：\n"
                    + "\n".join(f"  L{i['line']} {i['message']}" for i in minor[:3])
                ),
            )

        return None

    # -----------------------------------------------------------------------
    # 钩子 2 — 写后处理（更新 import map + 严格审查触发）
    # -----------------------------------------------------------------------

    def on_file_written(
        self,
        file_path: str,
        diff_stat: dict[str, Any] | None = None,
        review_store=None,
        author: str | None = None,
        known_authors: set[str] | None = None,
    ):
        """write/edit/patch 成功后调用。

        触发条件判断（mode_engine.should_trigger_strict）：
          1. 安全路径（auth/login/security 等）
          2. diff 特征（大变更/跨文件/API 变更）
          3. 新文件（首次创建，此前不存在）
          4. 历史问题率（同文件 90 天内 >60%）
        """
        # 1. 增量更新 import map → 后台线程执行
        self._import_map_thread = threading.Thread(
            target=self._async_update_import_map,
            args=(file_path,),
            daemon=True,
        )
        self._import_map_thread.start()

        # 2. 严格模式 → 触发判断
        if get_review_mode() == "strict":
            from minicode.review.mode_engine import should_trigger_strict

            should, reason = should_trigger_strict(
                file_path, diff_stat, review_store,
                cwd=self.cwd,
                is_new_file=file_path in self._new_files,
            )

            logger.info("review.post_write", extra={
                "file": file_path,
                "triggered_strict": should,
                "reason": reason,
            })

            if should:
                # review agent 完成后再启动 test agent（串行，不并行）
                self._spawn_review_sub_agent(file_path, reason, spawn_test=True)

    def _async_update_import_map(self, file_path: str) -> None:
        """后台线程执行：import map 增量更新。

        Defect 3: 不阻塞主循环，on_turn_end 时等待线程完成。
        """
        try:
            from minicode.tools.import_map import update_import_map_for_file
            update_import_map_for_file(self.cwd, file_path)
        except Exception as exc:
            logger.debug("Import map update failed: %s", exc)

    def _spawn_review_sub_agent(self, file_path: str, reason: str, spawn_test: bool = False):
        """后台线程调审查子 Agent，不阻塞主循环。

        spawn_test=True 时，仅在审查结果为 [REVIEW_RESULT: PASS] 时
        才自动调测试子 Agent。审查发现 FAIL 则不调 test，等主 Agent 先修代码。
        review 和 test 共用同一后台线程（串行执行）。
        on_turn_end 时 join 等待所有后台线程完成后统一注入。
        """
        if not self.tools or not self.tool_context:
            return

        prompt = (
            f"Review the changes in {file_path}.\n"
            f"Trigger reason: {reason}\n\n"
            f"1. Read the import map at .mini-code-import-map/import-map.json\n"
            f"2. Find which files reference changed symbols\n"
            f"3. Read affected files and check backward compatibility\n"
            f"4. Run code_review on {file_path}\n"
            f"5. Output a structured report with severity levels\n\n"
            f"At the end, output [REVIEW_RESULT: PASS] if no issues found, "
            f"or [REVIEW_RESULT: FAIL] if issues were found."
        )

        task_input = {"description": f"审查 {file_path}", "prompt": prompt, "agent_type": "review"}
        if SUB_AGENT_MODEL:
            task_input["model"] = SUB_AGENT_MODEL
        if SUB_AGENT_API_KEY:
            task_input["sub_api_key"] = SUB_AGENT_API_KEY
        if SUB_AGENT_API_BASE:
            task_input["sub_api_base"] = SUB_AGENT_API_BASE

        t = threading.Thread(
            target=self._background_review,
            args=(file_path, reason, task_input, spawn_test),
            daemon=True,
        )
        self._background_threads.append(t)
        t.start()

    def _background_review(self, file_path: str, reason: str, task_input: dict, spawn_test: bool = False) -> None:
        """后台执行：调审查子 Agent，结果安全写入 _coda_findings。

        如果 spawn_test=True 且审查结果为 PASS（无问题），自动调测试子 Agent。
        审查发现 FAIL 则不调 test，等主 Agent 先修代码。
        """
        review_passed = False
        try:
            result = self.tools.execute("task", task_input, self.tool_context)
            if result and result.ok:
                output = result.output or ""
                review_passed = "[REVIEW_RESULT: PASS]" in output
                with self._coda_lock:
                    self._coda_findings.append({
                        "role": "system",
                        "content": f"[Auto Review] {file_path}（{reason}）:\n{output}",
                    })
        except Exception as exc:
            logger.warning("Review sub-agent failed: %s", exc)

        # review PASS → 自动调 test（同一后台线程，串行）
        if spawn_test and review_passed:
            logger.info("Review PASS for %s, automatically spawning test...", file_path)
            self._spawn_test_sub_agent(file_path)
        elif spawn_test and not review_passed:
            logger.info("Review FAIL for %s, skipping test (fix code first)", file_path)

    def _spawn_test_sub_agent(self, file_path: str):
        """在 Docker 沙箱中跑测试，然后用精简 agent 分析结果生成报告。

        测试执行走 sandbox_test 工具（零模型调用），
        测试结果交给 test agent（max_turns=3）分析失败原因并生成结构化报告。
        """
        if not self.tools or not self.tool_context:
            return

        t = threading.Thread(target=self._background_test, args=(file_path,), daemon=True)
        self._background_threads.append(t)
        t.start()

    def _background_test(self, file_path: str) -> None:
        """后台执行：先跑 sandbox_test，再把结果交给 test agent 分析。"""
        try:
            # 第一步：Docker 沙箱跑测试（零模型调用）
            result = self.tools.execute("sandbox_test", {
                "changed_files": [file_path],
            }, self.tool_context)

            if not result:
                return

            output = result.output or ""
            passed = "[SANDBOX_RESULT: PASS]" in output

            if passed:
                logger.info("Tests PASSED for %s (sandbox)", file_path)
                return

            # 第二步：测试失败 → 启动精简 test agent 分析失败原因
            logger.warning("Tests FAILED for %s, analyzing...", file_path)

            prompt = (
                f"以下是在 {file_path} 上运行测试的结果。分析失败原因并输出结构化报告。\n\n"
                f"测试输出：\n```\n{output[:3000]}\n```\n\n"
                f"请输出：\n"
                f"1. 失败原因\n"
                f"2. 失败的具体测试用例\n"
                f"3. 修复建议"
            )

            task_input = {"description": f"分析 {file_path} 测试失败", "prompt": prompt, "agent_type": "test"}
            if SUB_AGENT_MODEL:
                task_input["model"] = SUB_AGENT_MODEL
            if SUB_AGENT_API_KEY:
                task_input["sub_api_key"] = SUB_AGENT_API_KEY
            if SUB_AGENT_API_BASE:
                task_input["sub_api_base"] = SUB_AGENT_API_BASE

            analysis = self.tools.execute("task", task_input, self.tool_context)
            analysis_text = analysis.output[:2000] if analysis and analysis.ok else output[:2000]

            with self._coda_lock:
                self._coda_findings.append({
                    "role": "system",
                    "content": (
                        f"[Auto Test] {file_path} 测试失败（Docker 沙箱）。\n"
                        f"分析报告：```\n{analysis_text}\n```\n"
                    ),
                })
        except Exception as exc:
            logger.warning("Test sub-agent failed: %s", exc)

    # -----------------------------------------------------------------------
    # 钩子 3 — Coda 阶段注入 + 沉淀
    # -----------------------------------------------------------------------

    def on_turn_end(self, current_messages: list[dict]) -> list[dict[str, Any]]:
        """Coda 阶段末尾调用，注入累积的审查发现 + 沉淀重要发现。

        等待顺序：
          1. import map 后台线程（最多 5 秒）
          2. 所有后台子 Agent 线程（review + test，最多 30 秒）
          3. 然后检查 _coda_findings

        ⚠️ 模式切换检测：即使本轮回合没有写入，也能触发回补扫描。
        """
        # 模式切换检测（off→loose/strict 过渡）
        self._check_transition()

        # 1. 等待 import map 后台线程完成（最多 5 秒）
        if self._import_map_thread and self._import_map_thread.is_alive():
            self._import_map_thread.join(timeout=5)
            self._import_map_thread = None

        # 2. 等待所有后台子 Agent 线程完成（review + test，最多 30 秒）
        for t in self._background_threads:
            if t.is_alive():
                t.join(timeout=30)
        self._background_threads.clear()

        # 3. 加锁操作 _coda_findings
        with self._coda_lock:
            if not self._coda_findings:
                return []

            # 注入审查报告到消息列表
            for msg in self._coda_findings:
                current_messages.append(msg)

            # 沉淀重要发现
            findings = self._accumulate_findings()
            self._try_promote(findings)

            self._coda_findings.clear()
            return findings

    def _accumulate_findings(self) -> list[dict[str, Any]]:
        """从审查报告中提取结构化发现。"""
        results = []
        for msg in self._coda_findings:
            results.append({
                "content": msg.get("content", ""),
                "role": "system",
            })
        return results

    def _try_promote(self, findings: list[dict[str, Any]]) -> None:
        """尝试将重要审查发现沉淀为 skill 或全局记忆。

        失败不影响主流程（try/except 兜底）。
        """
        try:
            from minicode.review.promotion import promote_findings
            result = promote_findings(findings, self.cwd)
            if result["skills"] or result["global_memories"]:
                logger.info("Promoted: skills=%s global_memories=%s",
                            result["skills"], result["global_memories"])
        except Exception as exc:
            logger.debug("Promotion failed: %s", exc)


# ---------------------------------------------------------------------------
# 宽松审查 — 写前预审查（正则 + AST）
# ---------------------------------------------------------------------------

_fp_cache: set[str] = set()

_FP_PREFIXES = ("test_", "example_", "mock_", "fixture_", "sample_", "fake_")

_CRITICAL_PATTERNS = [
    (r'(?i)(api[_-]?key|secret|password|token)\s*=\s*["\'][^"\']+["\']',
     "hardcoded-secret", "密钥硬编码"),
    (r'(?i)(execute|exec|raw)\(.*f["\']|\.format\(.*(?:request|input|user|get)',
     "sqli", "SQL 注入风险"),
    (r'\beval\s*\(', "unsafe-eval", "不安全的 eval 使用"),
]

_MINOR_PATTERNS = [
    (r'#\s*(TODO|FIXME|HACK|XXX)\b', "todo-left", "待办事项遗留"),
]

# 额外正则检测：裸 except（补充 AST 检测，覆盖无 try 上下文的情况）
_MAJOR_PATTERNS = [
    (r'^\s*except\s*:\s*$', "bare-except", "裸 except 捕获所有异常"),
]


def _pre_review_content(content: str, file_path: str = "") -> list[dict]:
    """写前快速审查。返回发现问题列表。

    宽松/严格模式共用此函数（严格模式在此基础上加跨文件分析）。
    """
    import ast
    import re

    findings = []
    lines = content.splitlines()

    # False-positive prefix check on file path (skip seed/test/fixture files)
    fp_file = any(file_path.startswith(p) or f"/{p}" in file_path for p in _FP_PREFIXES)

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if fp_file:
            continue
        # Skip lines that start with false-positive prefixes (mock_/test_/sample_/etc.)
        if any(stripped.startswith(p) for p in _FP_PREFIXES):
            continue
        # Skip literal string-only lines (docstrings, etc.) but not comments with TODO
        if stripped.startswith(('#', '"', "'")):
            # Only skip pure string literals; comments with TODO/FIXME still need checking
            if stripped.startswith('"') or stripped.startswith("'"):
                continue

        for pattern, rule_id, message in _CRITICAL_PATTERNS:
            if _is_false_positive(file_path, rule_id, i):
                continue
            if re.search(pattern, stripped):
                findings.append({
                    "line": i, "severity": "critical",
                    "rule_id": rule_id, "message": message,
                })

        for pattern, rule_id, message in _MINOR_PATTERNS:
            if re.search(pattern, stripped):
                findings.append({
                    "line": i, "severity": "minor",
                    "rule_id": rule_id, "message": message,
                })

        for pattern, rule_id, message in _MAJOR_PATTERNS:
            if re.search(pattern, stripped):
                findings.append({
                    "line": i, "severity": "major",
                    "rule_id": rule_id, "message": message,
                })

    # AST 检查
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                isinstance(h.type, ast.Name)
                and h.type.id == "Exception"
                and not h.name
                for h in node.handlers
            ):
                findings.append({
                    "line": getattr(node, "lineno", 0),
                    "severity": "major",
                    "rule_id": "bare-except",
                    "message": "裸 except 捕获所有异常",
                })
    except SyntaxError:
        pass

    return findings


def _line_hash(file_path: str, line: str) -> str:
    """基于行内容的稳定 hash，不依赖行号。"""
    import hashlib
    sig = f"{file_path}:{line.strip()[:60]}"
    return hashlib.md5(sig.encode()).hexdigest()[:12]


def _record_false_positive(file_path: str, issues: list[dict]) -> None:
    """记录 false_positive 到 review_memory + _fp_cache。"""
    from minicode.review.memory import ReviewMemoryStore, ReviewFinding

    store = ReviewMemoryStore()
    for issue in issues:
        fp_id = f"fp-{issue['rule_id']}-{_line_hash(file_path, issue.get('line_content', ''))}"
        store.add_finding(ReviewFinding(
            id=fp_id,
            severity=issue["severity"],
            file_path=file_path,
            rule_id=issue["rule_id"],
            status="false_positive",
        ))
    store.save()
    with _fp_cache_lock:  # Defect 7
        _fp_cache.add(fp_id)


def _is_false_positive(file_path: str, rule_id: str, line: int) -> bool:
    """判断是否已知的 false positive（优先查 _fp_cache，避免读盘）。

    Defect 2: 先用内存缓存判断，命中直接返回，不读磁盘。
    """
    key = f"fp-{rule_id}-{_line_hash(file_path, '')}"
    with _fp_cache_lock:  # Defect 7
        if key in _fp_cache:
            return True

    # 缓存未命中 → 查磁盘
    from minicode.review.memory import ReviewMemoryStore

    store = ReviewMemoryStore()
    for f in store.find_by_file(file_path):
        if f.rule_id == rule_id and f.status == "false_positive":
            with _fp_cache_lock:
                _fp_cache.add(key)
            return True
    return False
