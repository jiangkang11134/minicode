# SmartCode 审查系统 — 实现指令

## 项目简介

SmartCode 是一个终端 AI 编码 Agent（类似 Claude Code 的开源 Python 实现）。核心是一个 think/act/verify 循环引擎，支持多工具调度、会话持久化、文件回退等。

已有代码框架完整，项目根目录为 `minicode/`。

## 本次任务

给 SmartCode 添加一套**完全插拔的两级审查系统**。

## 架构

```
minicode/
├── agent_loop_lite.py              # 3 处钩子共 ~20 行，try/except ImportError 兜底
├── tools/
│   ├── import_map.py               # 全量建表 + 增量更新（后台线程，不阻塞）
│   ├── code_review.py              # 末尾加一行 from hooks import _pre_review_content
│   └── task.py                     # AGENT_TYPES 追加 "review" 和 "test"
│
└── review/                         # 存在即启用，删除即回归原版
    ├── __init__.py
    ├── config.py                   # 配置
    ├── hooks.py                    # 3 个钩子实现 + _pre_review_content（唯一实现）
    ├── mode_engine.py              # 严格审查触发判断
    ├── memory.py                   # 审查发现持久化
    └── promotion.py                # 发现 → skill/全局记忆 沉淀（已存在）
```

## 需要改动的文件

### 新文件（5 个）

1. **`minicode/review/__init__.py`** — 空文件

2. **`minicode/review/config.py`** — 配置入口
   ```python
   REVIEW_MODE = os.environ.get("MINICODE_REVIEW_MODE", "off").lower()
   FALSE_POSITIVE_PREFIXES = ("test_", "example_", "mock_", "fixture_", "sample_")
   SUB_AGENT_MODEL = os.environ.get("MINICODE_REVIEW_SUB_MODEL", None)
   ```

3. **`minicode/tools/import_map.py`** — import map
   - `build_import_map(project_root)`：全量扫描 → AST 提取符号 → 纯 Python 搜索引用 → JSON
   - `update_import_map_for_file(project_root, file_path)`：增量更新单个文件的符号
   - 首次建表约 3-8 秒，后台线程执行不阻塞启动
   - 增量更新时只扫描该文件的新符号，引用搜索仍然是全项目 O(n)
   - 查表 O(1)，建表/增量 O(n) — 两者是不同操作
   - 存储路径：`.mini-code-import-map/import-map.json`

### 改动文件（4 个）

4. **`minicode/tools/code_review.py`** — 末尾加一行
   ```python
   from minicode.review.hooks import _pre_review_content  # re-export，不重复实现
   ```
   **不**在 `code_review.py` 里声明任何 `_CRITICAL_PATTERNS` 或 `_FP_PREFIXES`，全部由 `hooks.py` 唯一维护，避免两份规则不同步。

5. **`minicode/tools/task.py`** — `AGENT_TYPES` 追加两种类型

   **`review` 类型（审查代码，只分析不改）**
   - 工具集：只读（read_file / grep_files / find_references / code_review / diff_viewer / file_tree / find_symbols）
   - max_turns：8
   - prompt："你是代码审查员，只读不改，不跑测试..."（详细 prompt 见方案文档）

   **`test` 类型（跑测试，沙盒执行，失败自动回滚 + 结构化报告）**
   - 工具集：run_command / test_runner / read_file / list_files / git / session
   - max_turns：8
   - 回滚方式：`git checkout HEAD <file>` 只回滚指定文件，不回滚全部
   - 主 Agent 改多个文件时，`test` agent 只回滚自己检测到失败的那个文件，不影响其他文件
   - 回滚后输出结构化报告注入消息，LLM 下一轮看到失败详情
   - 同规则累计失败 ≥3 次 → 沉淀为 skill

6. **`minicode/agent_loop_lite.py`** — 加 3 处钩子（共 ~20 行）
   - **文件开头**：`try: from minicode.review.hooks import get_review_hooks` — `ImportError` 兜底
   - **钩子 1**（`_execute_single_tool` 中，执行前）：`_review_hooks.on_before_write(...)` → 阻断 or 放行
   - **钩子 2**（`_execute_single_tool` 返回成功后）：`_review_hooks.on_file_written(file_path)` → 后台更新 import map，不阻塞主循环
   - **钩子 3**（Coda 阶段末尾）：`_review_hooks.on_turn_end(...)` → 注入审查发现 + 沉淀

## 已知技术风险与解决方案

### 1. `_pre_review_content` 唯一实现
**风险**：hooks.py 和 code_review.py 各维护一套规则 → 改了一处另一处不同步。
**方案**：只在 `hooks.py` 中实现，`code_review.py` 只做 re-export。所有规则只有一处定义。

### 2. 子 Agent 递归调用主循环
**风险**：`tools.execute("task", ...)` 内部调用 `run_agent_turn()`，主 Agent 循环里再起子 Agent 循环。
**方案**：
- **状态隔离**：传给子 Agent 的 `messages` 是主 Agent 当前消息列表的浅拷贝，子 Agent 的修改不回溯到主 Agent
- **Token 预算**：子 Agent 有自己的上下文窗口（`max_turns=8` 天然限制），不消耗主 Agent 的 context window
- **嵌套深度**：`task` 工具本身不会再次调 `task`，不存在递归风险
- **权限**：审查子 Agent 的工具集只有只读工具，不含任何写工具，不存在越权修改

### 3. import map 性能
**风险**：设计文档混用了"建表 O(n)"和"查表 O(1)"，增量更新引用搜索仍需全项目扫描。
**方案**：
- 首次建表：后台线程执行（3-8 秒），不阻塞 agent 启动
- 增量更新：`update_import_map_for_file()` 放到后台线程执行，钩子 2 立即返回
- `on_turn_end` 时确保建表/更新已完成
- 查表：`get_affected_files()` 是 O(1) 字典查询

### 4. test agent 回滚粒度
**风险**：`git checkout HEAD` 会回滚所有未提交文件，不只是当前文件。
**方案**：使用 `git checkout HEAD <file>` 只回滚指定文件，不影响其他文件的改动。多个文件同时改动不受影响。

### 5. 写后建表阻塞主循环
**风险**：`update_import_map_for_file()` 是同步操作，大项目下每次写文件后等几秒。
**方案**：增量更新放入后台线程执行，钩子 2 立即返回。`on_turn_end` 时等待线程完成。

## import map 数据结构

```json
{
  "version": 1,
  "updated_at": 1716979200.0,
  "symbols": {
    "authenticate": {
      "file": "auth/login.py", "type": "function", "is_public": true,
      "referenced_by": ["views.py", "api.py"]
    }
  }
}
```

## 宽松审查检查项（唯一维护在 hooks.py）

| 规则 ID | 严重程度 | 方式 |
|:-------|:--------:|:----:|
| hardcoded-secret | critical | 正则 |
| sql-injection | critical | 正则 |
| unsafe-eval | major | 正则 |
| bare-except | major | AST |
| todo-left | minor | 正则 |

## 严格审查触发条件

| 优先级 | 条件 | 示例 |
|:------:|:----|:----|
| 1 | 安全路径 | auth/ login/ security/ payment/ api/ |
| 2 | diff 特征 | 改 >200行、跨 >10文件、public API 变更 |
| 3 | 历史问题率 | 90天内同一文件问题率 >60% |
| 4 | 新人代码 | git author 不在已知贡献者列表 |

## 测试失败后的三条处理链路

1. `test` agent 返回 `[TEST_RESULT: FAIL]` + 结构化报告 → 注入消息列表，LLM 下一轮直接看到失败详情
2. 同步沉淀到 `review_memory`（`.mini-code-import-map/review-findings.json`）
3. 同 rule_id 累计失败 ≥3 次 → 自动沉淀为 skill（`~/.opencode/skills/`），下次写代码时系统提示中自动出现

## 判断已经写过的依据

- `minicode/review/` 目录存在
- `agent_loop_lite.py` 开头有 `try: from minicode.review.hooks` 导入
- `code_review.py` 末尾有 `from minicode.review.hooks import _pre_review_content`
