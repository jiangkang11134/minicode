# SmartCode — AI Coding Agent

<p align="center">
  <strong>终端 AI 编码 Agent · 两级代码审查 · Docker 沙箱测试 · 纯 Python 零依赖</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-1000%2B%20passed-brightgreen?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
</p>

---

## 项目简介

SmartCode 是一个终端 AI 编码 Agent，类似 Claude Code 的开源 Python 实现。你给它一个自然语言任务（如"帮我写一个登录模块"），它会：

1. **理解需求** → 调用 LLM 生成方案
2. **执行工具** → 读文件、写代码、跑命令
3. **审查代码** → 自动检测安全漏洞和兼容性问题
4. **反复迭代** → 直到任务完成

| 维度 | 数据 |
|:-----|:------|
| 源码量 | ~42K 行，102 个 .py 文件 |
| 运行时依赖 | **零**（纯 Python 标准库） |
| 模型支持 | 任何 OpenAI 兼容 API（DeepSeek、Qwen、中转站等） |
| 入口 | `pip install -e .` 后 `minicode` 命令 |

---

## 核心架构

```
                                     用户输入
                                        │
                                        ▼
┌────────────────── Prelude ────────────────────────────────┐
│  回合状态初始化 → 意图解析 → 上下文压缩 → 审查系统初始化    │
└────────────────────────┬──────────────────────────────────┘
                         ▼
┌────────── Recurrent Kernel（think/act/verify 循环）────────┐
│                                                             │
│  Step A → 策略推导（explore / execute / verify）             │
│  Step B → 调 LLM 模型                                       │
│  Step C → 判断返回（progress / retry / fallback / final）   │
│  Step D → 执行工具                                          │
│            ├─ write_file → 钩子 1（宽松审查）→ 钩子 2（写后）│
│            ├─ read_file → 直接返回                          │
│            └─ run_command → 权限检查                         │
│                                                             │
└────────────────────────┬──────────────────────────────────┘
                         ▼
┌────────────────── Coda ────────────────────────────────────┐
│  钩子 3：注入审查发现 → 沉淀重要模式到 skill/记忆 → 收尾     │
└────────────────────────────────────────────────────────────┘
```

### 模块结构

```
minicode/
├── agent_loop.py                入口（从 loop_orchestrator 重新导出）
├── loop_orchestrator.py         三阶段循环编排（878 行）
├── model_caller.py              LLM 调用、回退、错误处理（215 行）
├── tool_executor.py             工具执行 + 写前/写后钩子（193 行）
├── turn_kernel.py               决策引擎/状态机（~1700 行）
├── main.py                      CLI 入口
├── session.py                   会话持久化（含文件级检查点回退）
├── memory.py                    跨会话记忆系统（BM25 搜索）
│
├── review/                      审查系统（插拔式）
│   ├── config.py                配置（off/loose/strict 三级模式）
│   ├── hooks.py                 3 个钩子 + 宽松审查（正则+AST）
│   ├── mode_engine.py           严格审查触发（4 层判断）
│   ├── memory.py                审查发现持久化
│   └── promotion.py             发现→skill/记忆 自动沉淀
│
├── tools/                       18 个工具
│   ├── import_map.py            AST 建表 + 增量更新
│   ├── sandbox_test.py          Docker 沙箱测试执行器
│   ├── task.py                  子 Agent 调度（review + test 类型）
│   ├── write_file.py / edit_file.py / read_file.py
│   └── ...
│
└── tui/                         TUI 子系统（16 个文件）
```

---

## 核心特性

### 1. 两级代码审查系统

**宽松审查（毫秒级）** — 在写入代码前执行正则 + AST 检查：

| 规则 | 严重度 | 方式 | 处理 |
|:-----|:------:|:----:|:----:|
| 硬编码密钥 | critical | 正则 | 阻断写入 |
| SQL 注入 | critical | 正则 | 阻断写入 |
| unsafe eval | major | 正则 | 阻断写入 |
| bare except | major | AST + 正则 | 阻断写入 |
| TODO/FIXME 遗留 | minor | 正则 | 提示 |

**严格审查（秒级）** — 满足触发条件时自动启动子 Agent 做跨文件分析：

| 触发条件 | 示例 |
|:---------|:-----|
| 安全路径 | auth/ login/ security/ payment/ api/ |
| Diff 特征 | 改 >200 行、跨 >10 文件、public API 变更 |
| 历史问题率 | 90 天内同一文件问题率 >60% |
| 新人代码 | git author 不在已知贡献者列表 |

**审查结果沉淀** — Coda 阶段自动将重要发现沉淀为 skill 或全局记忆，跨项目生效。

### 2. 插拔式设计

- `review/` 目录存在 → 审查系统自动接入
- `review/` 目录删除 → 完全回归原版（`try/except ImportError` 兜底）
- 无需修改任何配置

### 3. 三级模式切换

```bash
export MINICODE_REVIEW_MODE=off      # 对照组，不审查
export MINICODE_REVIEW_MODE=loose    # 只开宽松审查
export MINICODE_REVIEW_MODE=strict   # 全开 + 子 Agent

# 支持运行时切换：off → strict 自动触发全面回补扫描，检查已有代码
```

### 4. Docker 沙箱测试

```python
# sandbox_test 工具 — 纯工具调用，零模型调用开销
sandbox_test(changed_files=["auth/login.py"])
  → 创建临时容器 → pip install 依赖 → 拷贝变更文件 → pytest → 销毁容器
  → 返回 [SANDBOX_RESULT: PASS] 或 [SANDBOX_RESULT: FAIL]
```

| 对比 | 传统方案 | SmartCode Docker 沙箱 |
|:-----|:---------|:-------------------|
| 隔离性 | ❌ 共享文件系统 | ✅ 完全容器隔离 |
| 回滚 | git checkout HEAD <file> | 删容器 = 回滚，零残留 |
| 新文件 | git checkout 报错 | Docker 自动处理 |
| 模型开销 | 5-15 轮 LLM 调用 | 零（纯工具调用） |

### 5. Import Map（纯 Python 跨平台符号索引）

```bash
# AST 提取项目符号 → 搜索引用 → 建表（后台线程，不阻塞启动）
# 审查时 O(1) 查表，不用 grep 全项目
tools/import_map.py → .mini-code-import-map/import-map.json
```

### 6. 架构拆分

原 `agent_loop_lite.py`（1225 行）拆分为三个独立模块：

| 模块 | 行数 | 职责 |
|:-----|:----:|:------|
| `loop_orchestrator.py` | 878 | 三阶段编排 |
| `model_caller.py` | 215 | LLM 调用 |
| `tool_executor.py` | 193 | 工具执行 + 审查钩子 |

---

## 测试结果

### 审查系统准确率基准

```bash
python benchmark/run_benchmark.py
```

| 分类 | 结果 |
|:-----|:----:|
| 安全检测（15 个样本） | **100% 通过**（硬编码密钥、SQL 注入、eval 滥用、bare except、TODO） |
| 白名单跳过（3 个样本） | **100% 通过**（test_/mock_/sample_ 前缀正确跳过） |
| 干净代码（6 个样本） | **100% 通过**（0 误报） |

| 综合指标 | 值 |
|:---------|:----|
| 准确率（Precision） | **100.0%** |
| 拦截率（Recall） | **100.0%** |
| 平均检测耗时 | **0.72ms** |

### 端到端编码测试

10 个单文件编码任务（源自 HumanEval），分别用 off 和 strict 模式测试：

| 模式 | 正确数 | 正确率 | 总耗时 |
|:-----|:------:|:------:|:------:|
| off（无审查） | 10/10 | **100%** | 141s |
| strict（审查） | 10/10 | **100%** | 162s |

> strict 模式额外开销约 15%，用于安全检查、import map 更新等，不影响代码生成质量。

### 多轮 + 跨文件任务（编码能力）

20 个多轮迭代与跨文件任务（改编自 HumanEval + MBPP，单文件/多轮/跨文件三类场景），使用 deepseek-v4-flash（DashScope）测试：

| 模式 | 正确数 | 正确率 | 总耗时 | 平均每任务 |
|:-----|:------:|:------:|:------:|:----------:|
| off（无审查） | 19/20 | **95%** | 1187s | 59s |
| strict（审查） | 19/20 | **95%** | 1191s | 60s |

> 零超时，代码文件生成成功率 100%。strict 模式仅增加 4 秒总开销，审查系统不产生误报。

### 安全审查端到端测试

9 项审查系统专项测试（含确定性检测 + Agent 任务 + Docker 沙箱）：

| 维度 | 通过率 | 说明 |
|:-----|:------:|:------|
| 宽松审查规则 | **6/6 100%** | 密钥/SQL注入/eval/bare-except/白名单/干净代码 |
| 跨文件兼容性 | **2/2 100%** | 函数签名变更自动检测所有调用方 |
| 新文件检测 | **2/2 100%** | 安全路径创建新文件自动触发审查 |
| Docker 沙箱 | **1/1 100%** | 容器创建→依赖安装→测试→销毁全链路 |

### LLM 连接

| 项目 | 数据 |
|:-----|:------|
| 模型 | deepseek-v4-flash（阿里云百炼 DashScope） |
| 适配器 | OpenAIModelAdapter（兼容任何 OpenAI API） |
| 响应 | 正常（任务平均完成时间 14-16s） |

---

## 快速开始

### 1. 安装

```bash
cd SmartCode-1
pip install -e .
```

### 2. 配置模型（阿里云百炼示例）

```bash
source set_dashscope.sh
```

或手动配置：

```bash
export CUSTOM_MODEL=deepseek-v4-flash
export CUSTOM_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export CUSTOM_API_KEY=your-api-key-here
```

支持任何 OpenAI 兼容 API（DeepSeek 直连、中转站、通义千问等）。

### 3. 运行

```bash
# TUI 交互模式
minicode

# CLI 模式（单轮）
minicode "帮我分析这个项目的结构"

# 管道模式（非交互）
echo "写一个 fibonacci 函数" | minicode

# 启用审查系统
export MINICODE_REVIEW_MODE=strict
minicode "写一个登录模块"
```

### 4. 审查系统配置

```bash
# 三级模式
export MINICODE_REVIEW_MODE=off      # 关闭审查
export MINICODE_REVIEW_MODE=loose    # 只开宽松（默认）
export MINICODE_REVIEW_MODE=strict   # 全开

# 运行时切换（立即生效，不需重启）
export MINICODE_REVIEW_MODE=strict

# 子 Agent 独立模型（可选）
export MINICODE_REVIEW_SUB_MODEL=deepseek-v4-flash
export MINICODE_REVIEW_SUB_API_KEY=your-key
export MINICODE_REVIEW_SUB_API_BASE=https://api.example.com/v1
```

### 5. 运行基准测试

```bash
# 审查系统准确率
python benchmark/run_benchmark.py

# 端到端编码测试
python benchmark/run_simple.py
```

---

## 文件改动总览

| 文件 | 类型 | 说明 |
|:-----|:----:|:------|
| `review/__init__.py` | 新建 | 包入口 |
| `review/config.py` | 新建 | 审查系统配置（三级模式） |
| `review/hooks.py` | 新建 | 3 个钩子 + 宽松审查唯一实现 |
| `review/mode_engine.py` | 新建 | 4 层严格审查触发 |
| `review/memory.py` | 新建 | 审查发现持久化 |
| `review/promotion.py` | 新建 | 发现→skill/记忆自动沉淀 |
| `tools/import_map.py` | 新建 | AST 建表 + 增量更新 |
| `tools/sandbox_test.py` | 新建 | Docker 沙箱测试 |
| `tools/task.py` | 改 | AGENT_TYPES 追加 review + test |
| `tools/code_review.py` | 改 | +1 行 re-export |
| `tool_executor.py` | 新建 | 工具执行 + 钩子 1/2 |
| `loop_orchestrator.py` | 新建 | 三阶段循环编排 |
| `model_caller.py` | 新建 | LLM 调用模块 |
| `agent_loop.py` | 改 | 核心入口重新导出 |
| `agent_loop_lite.py` | 删除 | 拆分为 3 个模块 |

---

## 项目结构

```
SmartCode-1/
├── minicode/          核心源码（102 个 .py 文件，~42K 行）
├── tests/             测试（31 个文件，~11K 行）
├── benchmark/         基准测试
│   ├── samples/       审查系统测试样本（24 个）
│   ├── tasks/         端到端编码任务（20 个）
│   ├── run_benchmark.py   审查准确率测试
│   ├── run_simple.py      端到端编码测试
│   └── test_complex.py    多轮/跨文件测试
├── docs/              文档
└── set_dashscope.sh   阿里云百炼配置脚本
```

---

## 技术栈

- **Python 3.10+** — 纯标准库，零运行时依赖
- **OpenAI 兼容 API** — 支持 DeepSeek、Qwen、Claude、GPT 等
- **Docker** — 测试沙箱隔离（可选）
- **AST + regex** — 代码静态分析
- **BM25** — 记忆搜索算法

---

## License

MIT
