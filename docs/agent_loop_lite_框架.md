# agent_loop_lite.py 整体框架

## 文件定位

执行引擎 / 调度器。负责"什么时候做什么"——调模型、执行工具、管理循环、发事件。

---

## 一、模块结构

```
┌────────────────────────────────────────────────────────────────┐
│                       agent_loop_lite.py                        │
│                                                                │
│  import 区域：                                                  │
│  ├─ 标准库：concurrent.futures / inspect / re / time / Path    │
│  ├─ 子系统：config / context_manager / model_registry / ...    │
│  ├─ turn_kernel（决策引擎）：TurnRecurrentState / decide_xxx   │
│  └─ 其他：hooks / ErrorClassifier / NudgeGenerator / ...       │
│                                                                │
│  常量区域（6 个 nudge 提示 + 2 组错误关键词）：                  │
│  ├─ NUDGE_CONTINUE                  - 模型只发进度没调工具     │
│  ├─ NUDGE_AFTER_TOOL_RESULT        - 模型拿到结果后继续       │
│  ├─ NUDGE_AFTER_EMPTY_RESPONSE     - 模型返回空响应           │
│  ├─ NUDGE_AFTER_EMPTY_NO_TOOLS     - 模型空响应且没用过工具   │
│  ├─ RESUME_AFTER_PAUSE             - 暂停后恢复               │
│  ├─ RESUME_AFTER_MAX_TOKENS        - token 截断后恢复         │
│  ├─ _MODEL_FALLBACK_ERROR_HINTS    - 可恢复的错误关键词       │
│  └─ _MODEL_FALLBACK_BLOCK_HINTS    - 不可恢复的错误关键词     │
│                                                                │
│  辅助函数区域（19 个）：                                        │
│  ├─ L131  _upsert_stable_task_state_message() - 替换任务状态   │
│  ├─ L157  _should_attempt_model_fallback()    - 是否 fallback  │
│  ├─ L178  _looks_like_provider_availability_error() - 服务端?  │
│  ├─ L198  _summarize_model_api_failure()      - 错误消息翻译   │
│  ├─ L247  _extract_model_id_from_provider_error() - 提取模型名 │
│  ├─ L264  _infer_active_model_id()            - 推断当前模型   │
│  ├─ L291  _is_empty_assistant_response()      - 是否空响应     │
│  ├─ L306  _extract_task_description()         - 提取原始任务   │
│  ├─ L329  _build_work_chain_task()            - 构建 TaskObject│
│  ├─ L359  _build_layered_context()            - 构建分层上下文 │
│  ├─ L399  _register_tool_capabilities()       - 注册能力索引   │
│  ├─ L447  _execute_single_tool()              - 执行单个工具   │
│  ├─ L529  _format_diagnostics()               - 格式化诊断     │
│  ├─ L553  _is_recoverable_thinking_stop()     - 可恢复思考?    │
│  ├─ L575  _should_treat_assistant_as_progress() - 进度消息?    │
│  ├─ L598  _is_at_blocking_limit()             - 上下文快满?    │
│  ├─ L628  _compute_effective_blocking_limit() - 计算阻塞阈值   │
│  ├─ L639  _try_compact_with_breaker()         - 熔断器压缩     │
│  └─ L661  _model_next()                       - 调模型        │
│                                                                │
│  核心函数区域：                                                 │
│  └─ L688  run_agent_turn() - 单轮 agent 交互循环               │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 二、run_agent_turn 执行流程

```
┌────────────────────────────────────────────────────────────────────────┐
│                        run_agent_turn()                                 │
│                        输入: messages → 输出: messages                  │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ════════════════════ Prelude ═══════════════════════════════════════    │
│                                                                        │
│  [P1] 复制消息列表、空值兜底                                            │
│                                                                            │
│  [P2] 确定模型名                                                        │
│       configured_runtime_model = runtime.configuredModel / model /      │
│                                  model.model_id（三选一）                │
│                                                                            │
│  [P3] 确定 profile 并创建 turn_state                                     │
│       runtime_profile = resolve_runtime_profile(runtime)                │
│       turn_state = TurnRecurrentState(max_steps, widen_after_step, ...) │
│                                                                            │
│  [P4] 定义 emit_runtime_event() 内部函数                                  │
│       └─ 统一事件通知入口                                                │
│                                                                            │
│  [P5] 创建工具调度器 + 前奏状态                                           │
│       tool_scheduler = ToolScheduler(metrics_collector)                 │
│       prelude = TurnPreludeState(auditor=...)                           │
│                                                                            │
│  [P6] 构建任务（enable_work_chain=True 时）                              │
│       ├─ _build_work_chain_task() → TaskObject                          │
│       ├─ 创建 TaskGraph、assign_slot、start_task                        │
│       ├─ _build_layered_context()                                       │
│       └─ _register_tool_capabilities()                                  │
│                                                                            │
│  [P7] 上下文预检 + 微压缩                                                │
│       ├─ micro_compactor.compact() ← 裁剪冗余                           │
│       └─ should_auto_compact() → compact_messages()  ← 压力大就压      │
│                                                                            │
├════════════════ Recurrent Kernel ════════════════════════════════════    │
│  while turn_state.has_remaining_steps():                                 │
│    step = turn_state.begin_step()                                        │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step A: 策略推导                                                 │ │
│    │  ├─ derive_turn_step_policy(turn_state) → TurnStepPolicy         │ │
│    │  ├─ render_turn_policy_message(prev, curr) → str|None             │ │
│    │  ├─ 激进压缩（策略要求时）                                        │ │
│    │  ├─ build_stable_task_pack(...) → StableTaskPack                  │ │
│    │  ├─ _upsert_stable_task_state_message(...) ← 替换旧状态           │ │
│    │  └─ fire_hook_sync(AGENT_START)                                   │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step B: 模型调用                                                 │ │
│    │  ├─ Layer 0: 预判式上下文守卫                                     │ │
│    │  │  _is_at_blocking_limit(当前token, 窗口) → 满了就 return       │ │
│    │  ├─ _model_next(model, messages, ...) → AgentStep                 │ │
│    │  │  └─ AgentStep.type = "assistant" 或 "tool_calls"               │ │
│    │  └─ 异常处理：                                                    │ │
│    │     ├─ ConnectionError → return "网络错误"                        │ │
│    │     ├─ TimeoutError  → return "超时"                              │ │
│    │     └─ Exception → 413 错误? → 压缩重试(continue) / 否则 return  │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step C: 处理模型返回（type="assistant" 时）                      │ │
│    │  ├─ decide_assistant_turn(...) → AssistantTurnDecision           │ │
│    │  │  └─ 返回 kind (由 turn_kernel 决策)                           │ │
│    │  │                                                                │ │
│    │  ├─ kind="progress" → 追加进度消息 → continue                    │ │
│    │  ├─ kind="retry"    → 塞 nudge → continue                        │ │
│    │  ├─ kind="fallback"                                              │ │
│    │  │  ├─ widen_needed → activate_widening + nudge → continue       │ │
│    │  │  └─ 其他 → 追加消息 → return（结束）                          │ │
│    │  └─ kind="final"   → 追加答案 + 保护答案 → return ✅             │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step D: 执行工具（type="tool_calls" 时）                         │ │
│    │  ├─ 单工具? → 串行 _execute_single_tool                          │ │
│    │  ├─ 多工具? → ToolScheduler 分类：                                │ │
│    │  │  ├─ 只读工具 → 并行（线程池，store=None）                     │ │
│    │  │  └─ 写入工具 → 串行（store 有值，实时 UI）                    │ │
│    │  ├─ 处理结果：                                                    │ │
│    │  │  ├─ fire_hook(POST_TOOL_USE)                                  │ │
│    │  │  ├─ turn_state.record_tool_result(ok, summary)                │ │
│    │  │  ├─ decide_tool_turn() → ToolTurnDecision                     │ │
│    │  │  ├─ ErrorClassifier + NudgeGenerator 处理错误                  │ │
│    │  │  └─ 追加 tool_call + tool_result 到消息列表                    │ │
│    │  └─ await_user? → return / 否则 → continue                       │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  步数用尽退出 while:                                                    │
│  └─ return "Reached the maximum tool step limit for this turn."        │
│                                                                        │
├═══════════════════ Coda ═════════════════════════════════════════════    │
│  finally:                                                               │
│  ├─ fire_hook_sync(AGENT_STOP)  ← 触发停止钩子                         │
│  ├─ metrics_collector.end_turn() ← 记录指标                            │
│  ├─ build_turn_coda_summary() → TurnCodaSummary                        │
│  └─ finalize_work_chain_task() ← 更新任务状态                          │
│     ├─ COMPLETED → task_graph.complete_task()                          │
│     ├─ PAUSED    → task_graph.slots.state = QUEUED                     │
│     └─ FAILED    → task_graph.fail_task()                              │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 三、关键设计点

| 设计 | 为什么要这样 |
|------|------------|
| `store` 和回调传 None | 并行执行时避免多线程竞态，UI 更新延迟到主线程统一处理 |
| `_execute_single_tool` 用 ThreadPoolExecutor | 跨平台超时保护，防止工具死循环卡死整个 agent |
| `_model_next` 动态检查签名 | 不同适配器参数不同，统一兼容层 |
| `_is_at_blocking_limit` 提前拦截 | 比等 API 报 413 更省时省钱 |
| 6 层防御体系 | 从预检→微压缩→PID→主动→反应式→全局异常，逐层兜底 |
| `_upsert_stable_task_state_message` | 用替换代替追加，防止状态消息无限堆积 |

---

## 四、_execute_single_tool 完整执行流程

```
调用位置: Step D 中执行每个工具时调用（串行和并行都走它）
输入: call（工具名+参数）, tools, cwd, permissions, store, ...
输出: ToolResult（ok=True 成功 / ok=False 失败含错误）

┌──────────────────────────────────────────────────────────────────────┐
│            _execute_single_tool 完整 7 层流程                         │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第1层: 提取参数                                                 │  │
│  │ └─ tool_name = call["toolName"]   → "read_file"               │  │
│  │ └─ tool_input = call["input"]     → {"path": "main.py"}       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第2层: try 开始 — 全局异常安全网                                 │  │
│  │ └─ 捕获 (KeyboardInterrupt/SystemExit) 和 Exception            │  │
│  │    ├─ KeyboardInterrupt/SystemExit → 不捕获，直                │  │
│  │    │  接往上抛，保证 Ctrl+C 能退出                              │  │
│  │    └─ Exception → 最后一道防线                                  │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第3层: 前置处理（仅串行模式 — store 和回调都有值）               │  │
│  │ ├─ if on_tool_start: on_tool_start(tool_name, tool_input)     │  │
│  │ │   └─ 通知 TUI："read_file 开始执行了，参数是 path=main.py"   │  │
│  │ └─ if store: store.set_state(set_busy(tool_name))              │  │
│  │     └─ 全局状态设为 busy(read_file)，TUI 显示加载动画           │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第4层: 确定超时时间                                              │  │
│  │ ├─ _base_timeout = env MINICODE_TOOL_TIMEOUT 或 120 秒         │  │
│  │ └─ TOOL_TIMEOUT =                                               │  │
│  │     ├─ tool_scheduler._force_tool_timeout? → 用动态超时         │  │
│  │     └─ 没有? → 用 _base_timeout（默认 120s）                   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第5层: 带超时保护的执行 — ThreadPoolExecutor                    │  │
│  │ ├─ 创建 1 个临时工的线程池                                      │  │
│  │ ├─ pool.submit(tools.execute, tool_name, tool_input, context)  │  │
│  │ │   把任务丢给临时工去干，立刻返回 future（取货凭证）           │  │
│  │ ├─ future.result(timeout=TOOL_TIMEOUT)                         │  │
│  │ │   主线程拿着凭证等结果，最多等 N 秒                          │  │
│  │ │                                                                │  │
│  │ ├─ 正常: → 拿到 ToolResult，跳出第5层                           │  │
│  │ ├─ TimeoutError: → ToolResult(ok=False, "超时了")              │  │
│  │ └─ Exception: → 同步执行一次做兜底                              │  │
│  │    tools.execute(...) ← 线程可能只是调度问题，                         │  │
│  │                      同步重试可能成功                            │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第6层: 后置处理（仅串行模式）                                    │  │
│  │ ├─ if store:                                                    │  │
│  │ │   ├─ store.set_state(increment_tool_calls()) → 工具计数+1    │  │
│  │ │   └─ store.set_state(set_idle()) → 状态设回 idle             │  │
│  │ └─ if on_tool_result:                                           │  │
│  │     └─ on_tool_result(tool_name, output, is_error)              │  │
│  │        └─ 通知 TUI："read_file 执行完了，这是结果"              │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 返回: 正常路径 return result                                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 第7层: 全局异常兜底                                              │  │
│  │ └─ except Exception as exc: ← 第2层捕获                         │  │
│  │    ├─ 提取最后 3 行堆栈 (tb_excerpt)                           │  │
│  │    ├─ 记日志                                                    │  │
│  │    ├─ if store: set_idle() → 强制重置状态                       │  │
│  │    └─ return ToolResult(ok=False, "管线崩溃了"+堆栈)            │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ═══════════════════════════════════════════════════════════════════  │
│  串行 vs 并行模式的关键区别:                                          │
│                                                                      │
│               串行（单工具/串行批次）   并行（并发批次-线程池）       │
│  ──────────── ─────────────────────── ───────────────────────        │
│  store          有值 → 实时更新 UI      None → 跳过，延迟处理        │
│  on_tool_start  有值 → 实时通知         None → 跳过，延迟处理        │
│  on_tool_result 有值 → 实时通知         None → 跳过，延迟处理        │
│  ═══════════════════════════════════════════════════════════════════  │
└──────────────────────────────────────────────────────────────────────┘
```
