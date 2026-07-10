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

# 严格审查熔断器（用列表包装实现方法内修改）
_strict_review_failures: list[int] = [0]
_strict_review_max_failures: int = 3
_strict_review_lock: threading.Lock = threading.Lock()

from minicode.review.config import (
    IMPORT_MAP_DIR,
    IMPORT_MAP_FILE,
    SUB_AGENT_API_BASE,
    SUB_AGENT_API_KEY,
    SUB_AGENT_MODEL,
    get_review_mode,
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
    # 钩子 1 — 写前预审查（宽松审查 + 严格审查，阻断写入）
    # -----------------------------------------------------------------------

    def on_before_write(
        self, tool_name: str, tool_input: dict[str, Any]
    ):
        """write/edit/patch 执行前调用。

        两阶段审查：
          阶段1（宽松，毫秒级）：正则+AST，阻断 security critical/major
          阶段2（严格，秒级，strict 模式）：收集上下文+1次LLM，按等级处理

        返回 ToolResult(ok=False) 阻断写入，或 None 放行。
        """
        # 模式切换检测
        self._check_transition()

        # off 模式直接放行
        if get_review_mode() == "off":
            return None

        from minicode.tooling import ToolResult

        content = tool_input.get("content") or tool_input.get("new_string", "")
        file_path = tool_input.get("file_path") or tool_input.get("path", "")

        if file_path and not Path(file_path).exists():
            self._new_files.add(file_path)

        if not content.strip():
            return None

        # ════════════════════════════════════════════════════════════════
        # 阶段1：宽松审查（正则+AST，毫秒级）
        # ════════════════════════════════════════════════════════════════
        start = time.time()
        issues = _pre_review_content(content, file_path)
        elapsed_ms = int((time.time() - start) * 1000)

        critical = [i for i in issues if i["severity"] in ("critical", "major")]
        minor = [i for i in issues if i["severity"] in ("minor", "suggestion")]

        # 宽松审查命中 critical → 阻断（force=true 可跳过）
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

        # ════════════════════════════════════════════════════════════════
        # 阶段2：严格审查（strict 模式，收集+1次LLM，~5s）
        # 带熔断器和超时保护，防止 API 不稳定时阻塞写入
        # ════════════════════════════════════════════════════════════════
        if get_review_mode() == "strict":
            with _strict_review_lock:
                breaker_open = _strict_review_failures[0] >= _strict_review_max_failures

            if breaker_open:
                logger.warning("Strict review circuit breaker OPEN (%d/%d failures), skipping",
                               _strict_review_failures[0], _strict_review_max_failures)
                self._coda_findings.append({
                    "role": "system",
                    "content": (
                        f"[审查熔断] 严格审查因连续 {_strict_review_failures[0]} 次 API 失败已临时关闭，"
                        f"仅执行了基础安全检查。写入已放行。"
                    ),
                })
            else:
                strict_verdict = self._do_strict_review(file_path, content, reason="写入前审查")
                if strict_verdict is None:
                    # 严格审查异常 → 熔断计数 + 退化到宽松审查
                    with _strict_review_lock:
                        _strict_review_failures[0] += 1
                    fs = _strict_review_failures[0]
                    logger.warning("Strict review failed (%d/%d), falling back to loose review",
                                   fs, _strict_review_max_failures)
                    self._coda_findings.append({
                        "role": "system",
                        "content": (
                            f"[审查退化] 严格审查因 API 异常未完成（{fs}/{_strict_review_max_failures}），"
                            f"本次仅执行了基础安全检查。写入已放行。"
                        ),
                    })
                else:
                    # 严格审查成功 → 重置熔断器
                    with _strict_review_lock:
                        _strict_review_failures[0] = 0

                    # 按严重等级处理结果
                    sev = strict_verdict.get("severity", "pass")
                    if sev == "critical":
                        force = tool_input.get("force", False) or tool_input.get("force_write", False)
                        if not force:
                            return ToolResult(
                                ok=False,
                                output=(
                                    f"[审查阻断: 安全风险] {file_path}\n\n"
                                    f"问题: {strict_verdict['summary']}\n\n"
                                    f"详情:\n{strict_verdict.get('detail', '')}\n\n"
                                    f"建议修复方案:\n"
                                    f"1. 将密钥/密码移至环境变量，运行时读取\n"
                                    f"2. 使用参数化查询替代字符串拼接\n"
                                    f"3. 避免使用 eval()，改用安全的替代方案\n\n"
                                    f"如需强制写入，添加 force=true 参数。"
                                ),
                            )
                    elif sev == "major":
                        force = tool_input.get("force", False) or tool_input.get("force_write", False)
                        if not force:
                            return ToolResult(
                                ok=False,
                                output=(
                                    f"[审查阻断: 兼容性影响] {file_path}\n\n"
                                    f"问题: {strict_verdict['summary']}\n\n"
                                    f"冲突分析:\n{strict_verdict.get('detail', '')}\n\n"
                                    f"建议修复方案:\n"
                                    f"1. 保持向后兼容——保留旧的函数签名，新增带新参数的函数\n"
                                    f"2. 修改所有调用方（views.py、api.py等）同步更新\n"
                                    f"3. 或添加适配器层，兼容新旧两种调用方式\n\n"
                                    f"如需强制写入，添加 force=true 参数。"
                                ),
                            )
                    elif sev == "minor":
                        self._coda_findings.append({
                            "role": "system",
                            "content": (
                                f"[审查建议] {file_path}\n\n"
                                f"{strict_verdict.get('detail', strict_verdict.get('summary', ''))}\n\n"
                                f"建议优化:\n"
                                f"1. 考虑补充异常处理\n"
                                f"2. 命名和代码风格可进一步规范\n"
                                f"3. 不影响功能，可选择后续优化"
                            ),
                        })
                    else:
                        self._coda_findings.append({
                            "role": "system",
                            "content": f"[审查通过] {file_path} — 代码安全无兼容性问题",
                        })

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

    def _do_strict_review(self, file_path: str, content: str, reason: str = "写入前审查") -> dict | None:
        """同步执行严格审查：收集上下文 → 1 次 LLM → 返回裁决。

        返回:
            {"severity": "critical"|"major"|"minor"|"pass",
             "summary": "一句话总结",
             "detail": "详细报告"}
            或 None（异常时）
        """
        import json

        context_parts = []

        # 1. 环境摘要
        context_parts.append(f"[环境]\n变更文件: {file_path}\n原因: {reason}")

        # 2. 变更代码
        context_parts.append(f"[变更代码]\n```python\n{content}\n```")

        # 3. import map → 受影响文件 + 内容
        import_map_path = Path(self.cwd) / IMPORT_MAP_DIR / IMPORT_MAP_FILE
        affected = []
        if import_map_path.exists():
            try:
                data = json.loads(import_map_path.read_text(encoding="utf-8"))
                for sym_name, sym_data in data.get("symbols", {}).items():
                    if sym_data.get("file") == file_path:
                        for ref in sym_data.get("referenced_by", []):
                            if ref != file_path and ref not in affected:
                                affected.append(ref)
                if affected:
                    context_parts.append(f"[受影响文件]\n" + "\n".join(f"- {f}" for f in affected))
                    for af in affected:
                        af_path = Path(self.cwd) / af
                        if af_path.exists():
                            afc = af_path.read_text(encoding="utf-8", errors="replace")
                            context_parts.append(f"[文件: {af}]\n```python\n{afc}\n```")
            except Exception:
                pass

        # 4. code_review 结果
        try:
            cr = self.tools.execute("code_review", {"path": str(Path(self.cwd) / Path(file_path).parent)}, self.tool_context)
            if cr and cr.ok:
                context_parts.append(f"[code review]\n{cr.output[:1500]}")
        except Exception:
            pass

        prompt = (
            f"审查以下代码变更。按严重等级输出结果。\n\n"
            + "\n\n".join(context_parts) +
            "\n\n"
            f"输出格式（严格按以下 JSON 格式）：\n"
            f'{{"severity": "critical"/"major"/"minor"/"pass", '
            f'"summary": "一句话总结", '
            f'"detail": "详细报告"}}\n\n'
            f"等级定义：\n"
            f"- critical: 安全漏洞（密钥硬编码/SQL注入/eval/可被外部利用的问题）\n"
            f"- major: 兼容性问题（改签名未更新调用方/API 破坏性变更）\n"
            f"- minor: 代码质量问题（缺少异常处理/命名不规范/可优化）\n"
            f"- pass: 无问题"
        )

        task_input = {"description": f"审查 {file_path}", "prompt": prompt, "agent_type": "review",
                       "max_turns": 1}
        if SUB_AGENT_MODEL:
            task_input["model"] = SUB_AGENT_MODEL
        if SUB_AGENT_API_KEY:
            task_input["sub_api_key"] = SUB_AGENT_API_KEY
        if SUB_AGENT_API_BASE:
            task_input["sub_api_base"] = SUB_AGENT_API_BASE

        try:
            # 带超时的工具调用（10 秒，防止 API 挂死阻塞写入）
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.tools.execute, "task", task_input, self.tool_context)
                try:
                    result = future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    logger.warning("Strict review timed out after 10s")
                    return None

            if result and result.ok:
                output = result.output or ""
                # 尝试提取 JSON 输出
                import re
                m = re.search(r'\{[^}]+\}', output)
                if m:
                    verdict = json.loads(m.group())
                    return verdict
                # 兜底：未解析出 JSON 时根据关键字符串判断
                if "[REVIEW_RESULT: FAIL]" in output or "critical" in output.lower():
                    return {"severity": "major", "summary": "审查发现问题", "detail": output[:1000]}
                return {"severity": "pass", "summary": "审查通过", "detail": output[:500]}
        except Exception as exc:
            logger.warning("Strict review failed: %s", exc)
            return None

    def on_file_written(
        self,
        file_path: str,
        diff_stat: dict[str, Any] | None = None,
        review_store=None,
        author: str | None = None,
        known_authors: set[str] | None = None,
    ):
        """write/edit/patch 成功后调用。

        严格审查已在 on_before_write 中同步完成。
        此钩子只做 import map 增量更新（后台线程）。
        """
        # 1. 增量更新 import map → 后台线程执行
        self._import_map_thread = threading.Thread(
            target=self._async_update_import_map,
            args=(file_path,),
            daemon=True,
        )
        self._import_map_thread.start()

        # 2. strict 模式 → 检查是否需要启动 test agent（仅当文件是新文件或安全路径时）
        if get_review_mode() == "strict":
            from minicode.review.mode_engine import should_trigger_strict

            should, reason = should_trigger_strict(
                file_path, diff_stat, review_store,
                cwd=self.cwd,
                is_new_file=file_path in self._new_files,
            )

            if should:
                # test agent 启动只在写后做（不重复审查，只跑测试）
                self._spawn_test_sub_agent(file_path)

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
    from minicode.review.memory import ReviewFinding, ReviewMemoryStore

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
