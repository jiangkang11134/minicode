# agent_loop 与 turn_kernel 联合架构图

## 一、整体关系：执行引擎 vs 决策引擎

```
┌────────────────────────────────────────────────────────────────────────┐
│                         agent_loop_lite.py                             │
│                        （执行引擎 / 调度器）                             │
│                                                                        │
│  run_agent_turn()                                                      │
│    ├─ Prelude（准备阶段）                                               │
│    ├─ Recurrent Kernel（Step A→B→C→D 循环）                            │
│    └─ Coda（收尾阶段）                                                  │
│                                                                        │
│  调用 turn_kernel 的：                                                  │
│    ├─ 数据结构 → TurnRecurrentState / TurnVerificationState 等          │
│    ├─ 策略函数 → derive_turn_step_policy / render_turn_policy_message  │
│    ├─ 决策函数 → decide_assistant_turn / decide_tool_turn              │
│    └─ 工具函数 → build_stable_task_pack / build_turn_coda_summary 等   │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │ 调用
                                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         turn_kernel.py                                  │
│                        （决策引擎 / 状态机）                             │
│                                                                        │
│  数据结构（7 个 dataclass）：                                           │
│    ├─ TurnBudgetSignals       - 预算信号                               │
│    ├─ TurnVerificationState   - 验证状态                               │
│    ├─ TurnStepPolicy           - 步骤策略                              │
│    ├─ StableTaskPack           - 稳定任务包                            │
│    ├─ TurnPreludeState         - 前奏状态                              │
│    ├─ TurnRecurrentState       - 循环状态 ★核心                        │
│    ├─ AssistantTurnDecision    - 助手决策结果                          │
│    ├─ ToolTurnDecision         - 工具决策结果                          │
│    └─ TurnCodaSummary          - 收尾摘要                              │
│                                                                        │
│  策略函数：                                                             │
│    ├─ derive_turn_step_policy()       → TurnStepPolicy                 │
│    ├─ _derive_widening_signal()       → (bool, reason, evidence)       │
│    └─ render_turn_policy_message()    → str | None                     │
│                                                                        │
│  决策函数：                                                             │
│    ├─ decide_assistant_turn()  → AssistantTurnDecision                 │
│    └─ decide_tool_turn()       → ToolTurnDecision                     │
│                                                                        │
│  工具函数：                                                             │
│    ├─ build_stable_task_pack() → StableTaskPack | None                 │
│    ├─ build_turn_coda_summary() → TurnCodaSummary                     │
│    ├─ finalize_work_chain_task() → None                                │
│    ├─ build_widening_transition_nudge() → str                         │
│    └─ build_verification_evidence_nudge() → str                       │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 二、run_agent_turn 完整流程（含两个文件的交互）

```
┌─────────────────────────────────────────────────────────────────────────┐
│  run_agent_turn()                                                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ═══════════════════════ Prelude ════════════════════════════════════     │
│                                                                         │
│  ① 复制消息列表、空值兜底                                               │
│     current_messages = list(messages)                                   │
│     runtime = runtime or {}                                             │
│                                                                         │
│  ② 创建 turn_state（TurnRecurrentState） ← turn_kernel 的数据结构       │
│     └─ 存着 max_steps、step、error_count、widening_active 等所有状态     │
│                                                                         │
│  ③ 定义 emit_runtime_event（事件通知）                                  │
│                                                                         │
│  ④ 如果 enable_work_chain：                                              │
│     ├─ _build_work_chain_task() → 解析意图 → 构建 TaskObject            │
│     └─ _register_tool_capabilities()                                    │
│                                                                         │
│  ⑤ 上下文预检 + 微压缩                                                  │
│     ├─ micro_compactor.compact()  ← 裁剪冗余                            │
│     └─ should_auto_compact()      ← 压力大就压                          │
│                                                                         │
├═══════════════════ Recurrent Kernel ════════════════════════════════════┤
│  while turn_state.has_remaining_steps():                                 │
│    step = turn_state.begin_step()                                       │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step A: 策略推导                                                 │ │
│    │  ┌─────────────────────────────────────────────────────────────┐ │ │
│    │  │ agent_loop 做的事：                                          │ │ │
│    │  │   current_policy = derive_turn_step_policy(turn_state)       │ │ │
│    │  │   policy_message = render_turn_policy_message(...)           │ │ │
│    │  │   build_stable_task_pack(...)                                │ │ │
│    │  │   _upsert_stable_task_state_message(...)                     │ │ │
│    │  │   fire_hook_sync(AGENT_START)                                │ │ │
│    │  │                                                              │ │ │
│    │  │ turn_kernel 提供：                                            │ │ │
│    │  │   derive_turn_step_policy(turn_state) → TurnStepPolicy       │ │ │
│    │  │     ├─ 第1步: 计算基础上下文（step/max_steps/evidence）      │ │ │
│    │  │     ├─ 第2步: 计算阶段切换阈值（verify_after/execute_after） │ │ │
│    │  │     ├─ 第3步: 确定 phase（explore/execute/verify）           │ │ │
│    │  │     ├─ 第4步: _derive_widening_signal() → 是否允许 widen     │ │ │
│    │  │     ├─ 第5步: 设定 guidance 和 verification_focus              │ │ │
│    │  │     └─ 第6步: 构建 TurnStepPolicy，更新 turn_state           │ │ │
│    │  │                                                              │ │ │
│    │  │   render_turn_policy_message(prev, curr) → str | None        │ │ │
│    │  │     ├─ 跟上一步策略一样？→ return None（不浪费 token）       │ │ │
│    │  │     └─ 不一样？→ 拼字符串 "Runtime phase: execute..."        │ │ │
│    │  │                                                              │ │ │
│    │  │   build_stable_task_pack(...) → StableTaskPack               │ │ │
│    │  │     └─ 把 task/protected_context/progress/verification/budget│ │ │
│    │  │        打包成稳定状态，塞进系统提示                           │ │ │
│    │  └─────────────────────────────────────────────────────────────┘ │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step B: 模型调用                                                 │ │
│    │  ┌─────────────────────────────────────────────────────────────┐ │ │
│    │  │ ① Layer 0: 预判式上下文守卫                                   │ │ │
│    │  │   _is_at_blocking_limit(当前token, 上下文窗口) → 满了就 return │ │ │
│    │  │                                                              │ │ │
│    │  │ ② _model_next(model, messages, ...) → AgentStep               │ │ │
│    │  │    └─ AgentStep.type = "assistant"（说话）或 "tool_calls"（调工具）│ │
│    │  │                                                              │ │ │
│    │  │ ③ 异常处理：                                                  │ │ │
│    │  │    ├─ ConnectionError → return "网络错误"                     │ │ │
│    │  │    ├─ TimeoutError → return "超时"                            │ │ │
│    │  │    └─ Exception → 尝试压缩后重试，失败则 return 错误         │ │ │
│    │  └─────────────────────────────────────────────────────────────┘ │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step C: 处理模型返回（模型说了话 → 走这里）                       │ │
│    │  ┌─────────────────────────────────────────────────────────────┐ │ │
│    │  │ agent_loop 做的事：                                          │ │ │
│    │  │   assistant_decision = decide_assistant_turn(...)            │ │ │
│    │  │   └─ 根据 decision.kind 分 4 种路径处理                       │ │ │
│    │  │                                                              │ │ │
│    │  │ turn_kernel 提供：                                            │ │ │
│    │  │   decide_assistant_turn(turn_state, step_content, is_empty,  │ │ │
│    │  │       stop_reason, block_types, treat_as_progress,           │ │ │
│    │  │       is_recoverable_thinking_stop, nudge 常量们, ...)        │ │ │
│    │  │                                                              │ │ │
│    │  │   第1步: treat_as_progress? → kind="progress"                 │ │ │
│    │  │   第2步: 可恢复思考中断? → kind="progress"+ recovery         │ │ │
│    │  │   第3步: 空响应+还可重试? → kind="retry"+ nudge              │ │ │
│    │  │   第4步: 空响应+重试耗尽? → kind="fallback"                  │ │ │
│    │  │     ├─ late_verify + saw_tool_result → "verification_failed" │ │ │
│    │  │     ├─ widen_ready → "widen_needed"                          │ │ │
│    │  │     └─ 其他 → "blocked"                                      │ │ │
│    │  │   第5步: 验证守卫（verify+需证据+没引用证据）→ kind="progress"│ │ │
│    │  │   第6步: 默认为 final → kind="final"                          │ │ │
│    │  │                                                              │ │ │
│    │  │   返回 AssistantTurnDecision {                               │ │ │
│    │  │     kind: "progress"|"retry"|"fallback"|"final",             │ │ │
│    │  │     assistant_content,  ← 要追加的消息                        │ │ │
│    │  │     user_content,       ← 要给模型的 nudge                   │ │ │
│    │  │     stop_reason,                                              │ │ │
│    │  │     protect_final_answer,                                    │ │ │
│    │  │   }                                                          │ │ │
│    │  └─────────────────────────────────────────────────────────────┘ │ │
│    │                                                                   │ │
│    │  然后 agent_loop 根据 kind：                                       │ │ │
│    │    ├─ progress → 追加进度消息 → continue                          │ │ │
│    │    ├─ retry → 塞 nudge → continue                                │ │ │
│    │    ├─ fallback(widen_needed) → 扩大范围 → continue               │ │ │
│    │    ├─ fallback(其他) → return（结束）                            │ │ │
│    │    └─ final → 追加答案 + 保护答案 → return ✅                    │ │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│    ┌──────────────────────────────────────────────────────────────────┐ │
│    │  Step D: 执行工具（模型要调工具 → 走这里）                         │ │ │
│    │  ┌─────────────────────────────────────────────────────────────┐ │ │
│    │  │ agent_loop 做的事：                                          │ │ │
│    │  │   ① 单工具 → 串行 _execute_single_tool                      │ │ │
│    │  │   ② 多工具 → ToolScheduler 分类：                            │ │ │
│    │  │      ├─ 只读工具 → 并行（线程池）                            │ │ │
│    │  │      └─ 写入工具 → 串行                                      │ │ │
│    │  │   ③ 处理结果：                                               │ │ │
│    │  │      ├─ fire_hook(POST_TOOL_USE)                             │ │ │
│    │  │      ├─ turn_state.record_tool_result(ok, summary)            │ │ │
│    │  │      ├─ tool_decision = decide_tool_turn(...)                 │ │ │
│    │  │      ├─ ErrorClassifier + NudgeGenerator 处理错误            │ │ │
│    │  │      ├─ 追加 tool_call + tool_result 到消息列表              │ │ │
│    │  │      └─ await_user? → return / 否则 → continue               │ │ │
│    │  │                                                              │ │ │
│    │  │ turn_kernel 提供：                                            │ │ │
│    │  │   decide_tool_turn(tool_name, result_output, await_user)     │ │ │
│    │  │     → ToolTurnDecision {                                     │ │ │
│    │  │         kind: "continue"|"await_user",                       │ │ │
│    │  │         progress_summary: "processed tool result from xxx"   │ │ │
│    │  │       }                                                      │ │ │
│    │  └─────────────────────────────────────────────────────────────┘ │ │
│    └──────────────────────────────────────────────────────────────────┘ │
│                                                                         │
├════════════════════ Coda ════════════════════════════════════════════════┤
│  finally:                                                               │
│    ① fire_hook_sync(AGENT_STOP)                                        │
│    ② build_turn_coda_summary(turn_state, context_usage)                │
│       └─ turn_kernel 提供：                                             │
│          └─ 根据 stop_reason 生成 TurnCodaSummary                       │
│    ③ finalize_work_chain_task(task, auditor, coda_summary)             │
│       └─ turn_kernel 提供：                                             │
│          ├─ task_state=COMPLETED? → complete_task                      │
│          ├─ task_state=PAUSED? → 设为 QUEUED                           │
│          └─ 其他 → fail_task                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 三、关键数据结构（turn_kernel）与 agent_loop 的对应关系

```
┌─────────────────────────┬──────────────────────┬─────────────────────────┐
│ 数据结构                │ 谁创建/更新           │ agent_loop 哪里用         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnRecurrentState      │ Prelude 中创建        │ 全程：Step A/B/C/D 读写  │
│   - 核心状态跟踪器       │                       │                         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnStepPolicy          │ Step A 中              │ Step A：设置当前策略     │
│   - 当前步骤的策略       │ derive_turn_step_policy│ Step C：传到             │
│                         │                        │   decide_assistant_turn  │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnVerificationState   │ TurnRecurrentState 持有│ Step A：更新验证状态     │
│   - 验证模式/证据状态    │ Step A 中更新           │ Step C：决定是否放行 final │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ AssistantTurnDecision   │ turn_kernel 返回       │ Step C：4 种决策分支     │
│   - 助手响应的决策结果   │ decide_assistant_turn  │                         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ ToolTurnDecision        │ turn_kernel 返回       │ Step D：继续/等用户      │
│   - 工具结果的决策       │ decide_tool_turn      │                         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ StableTaskPack          │ Step A 中              │ Step A：塞进系统提示     │
│   - 稳定任务状态摘要     │ build_stable_task_pack │                         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnCodaSummary         │ Coda 中                │ Coda：生成收尾摘要       │
│   - 轮次结果汇总         │ build_turn_coda_summary│   + 更新任务图           │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnBudgetSignals       │ TurnRecurrentState 刷新 │ Step A：策略函数读取     │
│   - 步数预算信号         │ _refresh_budget_signals│                         │
├─────────────────────────┼──────────────────────┼─────────────────────────┤
│ TurnPreludeState        │ Prelude 中构建         │ Step A：读取 task/task_graph │
│   - 前奏阶段的一次性数据  │                       │                         │
└─────────────────────────┴──────────────────────┴─────────────────────────┘
```

---

## 四、数据流：一句话串联

```
用户输入
  │
  ▼
Prelude: 创建 TurnRecurrentState（状态记账本）
  │
  ▼
while 循环：
  │
  Step A: 调用 derive_turn_step_policy(turn_state) → 拿到 TurnStepPolicy
  │        调用 build_stable_task_pack(...) → 塞进消息
  │
  Step B: 调 model.next() → 拿到 AgentStep（assistant 或 tool_use）
  │
  Step C: 如果是 assistant：
  │        调用 decide_assistant_turn(...) → AssistantTurnDecision
  │        progress → continue / retry → continue
  │        fallback(widen) → activate_widening → continue
  │        fallback(其他) → return / final → return ✅
  │
  Step D: 如果是 tool_use：
  │        调用 decide_tool_turn(...) → ToolTurnDecision
  │        await_user? → return / 否则 → continue
  │
  ▼
Coda: build_turn_coda_summary(turn_state) → finalize_work_chain_task
  │
  ▼
返回更新后的 messages
```

---

## 五、一句话总结分工

| 角色 | agent_loop_lite.py | turn_kernel.py |
|:----:|-------------------|----------------|
| **是什么** | 执行引擎 / 调度器 | 决策引擎 / 状态机 |
| **负责** | "什么时候做什么" | "怎么判断、怎么决策" |
| **干的事** | 调模型、执行工具、管理循环、发事件 | 算策略、判断 4 种类型、推导状态 |
| **类似** | 司机踩油门打方向盘 | 导航仪说"前方右转、当前限速60" |
