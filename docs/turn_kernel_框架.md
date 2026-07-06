# turn_kernel.py 整体框架

## 文件定位

决策引擎 / 状态机。负责"怎么判断、怎么决策"——算策略、判断 4 种类型、推导状态、汇总结果。

---

## 一、模块结构

```
┌────────────────────────────────────────────────────────────────┐
│                        turn_kernel.py                           │
│                                                                │
│  类型别名：                                                     │
│  ├─ TurnStopReason = "done"|"max_steps"|"await_user"|          │
│  │                   "blocked"|"verification_failed"|          │
│  │                   "widen_needed"                            │
│  └─ TurnStepPhase = "explore"|"execute"|"verify"              │
│                                                                │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  数据结构区（7 个 dataclass）                               ││
│  │  作用：定义 agent 循环中所有状态的结构                      ││
│  └────────────────────────────────────────────────────────────┘│
│                                                                │
│  TurnBudgetSignals      - L31  预算信号                            │
│  TurnVerificationState  - L47  验证状态                           │
│  TurnStepPolicy         - L66  步骤策略                          │
│  StableTaskPack         - L117 稳定任务包                        │
│  TurnPreludeState       - L180 前奏状态                          │
│  TurnRecurrentState     - L200 循环状态 ★核心                    │
│  AssistantTurnDecision  - L467 助手决策结果                      │
│  ToolTurnDecision       - L486 工具决策结果                      │
│  TurnCodaSummary        - L502 收尾摘要                          │
│                                                                │
│  TurnRecurrentState 方法区：                                      │
│  ├─ L236  has_remaining_steps()        - 还有步数吗              │
│  ├─ L250  begin_step()                 - 步数+1                  │
│  ├─ L266  can_retry_empty_response()   - 可以重试空响应吗        │
│  ├─ L280  record_empty_response_retry()- 记录空响应重试          │
│  ├─ L294  can_retry_recoverable_thinking() - 可恢复思考重试?     │
│  ├─ L311  record_recoverable_thinking_retry() - 记录可恢复重试   │
│  ├─ L322  record_tool_result()         - 记录工具结果            │
│  ├─ L348  set_progress_summary()       - 设置进度摘要            │
│  ├─ L362  set_stop_reason()            - 设置停止原因            │
│  ├─ L375  has_verification_evidence()  - 有验证证据吗            │
│  ├─ L389  activate_widening()          - 激活拓宽模式            │
│  ├─ L416  final_task_state()           - 推导最终任务状态        │
│  └─ L445  _refresh_budget_signals()    - 刷新预算信号（内部）    │
│                                                                │
│  TurnStepPolicy 方法区：                                         │
│  └─ L88   terminal_summary()           - 生成策略摘要字符串     │
│                                                                │
│  StableTaskPack 方法区：                                         │
│  └─ L141  to_protected_text()          - 转文本格式注入系统提示 │
│                                                              │
│  SessionData 方法区（session.py）：                                │
│  ├─ L106  __post_init__()              - 初始化自动创建 metadata  │
│  ├─ L123  update_metadata()            - 刷新元数据               │
│  ├─ L166  has_delta / L174 _compute_content_hash() - 增量追踪    │
│                                                                │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  策略函数区                                                ││
│  │  作用：决定每步怎么跑                                      ││
│  └────────────────────────────────────────────────────────────┘│
│  ├─ L683  derive_turn_step_policy()      ★★★★ → TurnStepPolicy  │
│  ├─ L561  _derive_widening_signal()      ★★★☆ → (bool, r, e)   │
│  └─ L847  render_turn_policy_message()   ★★★☆ → str | None     │
│                                                                │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  决策函数区                                                ││
│  │  作用：判断模型返回是什么类型                              ││
│  └────────────────────────────────────────────────────────────┘│
│  ├─ L1365 decide_assistant_turn()        ★★★★ → AssistantTurn  │
│  └─ L1588 decide_tool_turn()            ★★★★ → ToolTurnDecision│
│                                                                │
│  ┌────────────────────────────────────────────────────────────┐│
│  │  工具函数区                                                ││
│  │  作用：辅助组装/收尾                                       ││
│  └────────────────────────────────────────────────────────────┘│
│  ├─ L1068 build_stable_task_pack()        ★★★☆ → StableTask   │
│  ├─ L1200 build_turn_coda_summary()       ★★★☆ → TurnCoda     │
│  ├─ L1287 finalize_work_chain_task()      ★★★☆ → None         │
│  ├─ L524  _summarize_task_graph()         ★★☆☆ → str          │
│  ├─ L894  _step_aware_followup_nudge()    ★★☆☆ → str          │
│  ├─ L936  _content_mentions_evidence()    ★★☆☆ → bool         │
│  ├─ L984  build_verification_evidence_nudge() ★★☆☆ → str      │
│  └─ L1014 build_widening_transition_nudge()  ★★★☆ → str       │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 二、核心函数执行流程

### 1. derive_turn_step_policy() — 推导当前步骤策略

```
调用位置: agent_loop_lite.py Step A 每步调用
输入: turn_state
输出: TurnStepPolicy（同时更新 turn_state 中的策略字段）

第1步: 计算基础上下文
  └─ step = max(turn_state.step, 1)
  └─ max_steps = turn_state.max_steps or 0
  └─ remaining_steps = turn_state.budget_signals.remaining_steps
  └─ evidence_ready = turn_state.has_verification_evidence()

第2步: 计算阶段切换阈值
  ├─ verify_after = max(3, ceil(max_steps * 0.7))   ← 默认 70% 步数后进入验证
  │   └─ strict 模式 → verify_after = min(verify_after, 4)  ← 严格模式更早验证
  └─ execute_after = 2（single-deep）或 1（其他）   ← 探索阶段持续多久

第3步: 确定当前阶段 (phase)
  ├─ widening_active 且还有步数? → "execute"
  ├─ step <= execute_after?      → "explore"
  ├─ step >= verify_after / 剩余步数<=2 / strict+有结果+步数够了? → "verify"
  └─ 否则 → "execute"

第4步: 推导拓宽信号
  └─ _derive_widening_signal() → (allow_widening, reason, evidence)
      ├─ 工具错误 > 0?              → 允许 widen
      ├─ 有证据 + 空响应重试过?      → 允许 widen
      ├─ 无结果 + 空响应重试耗尽?    → 允许 widen
      ├─ 无结果 + 思考重试耗尽?      → 允许 widen
      └─ 否则                      → 不允许 widen

第5步: 设定 guidance 和 verification_focus
  ├─ widening_active → "compare alternative approaches..." / normal
  ├─ explore         → "inspect, decompose, and anchor..." / light
  ├─ execute         → "prefer concrete tool use..." / normal
  └─ verify          → "verify changes, test evidence..." / strict 或 normal

第6步: 构建 TurnStepPolicy 并更新 turn_state
  └─ 构造 policy 对象 → 存储到 turn_state.step_policy
  └─ 更新验证状态（requires_explicit_final / requires_evidence 等）
  └─ 生成 last_verification_note

第7步: 返回 policy
```

### 2. decide_assistant_turn() — 判断助手响应的类型

```
调用位置: agent_loop_lite.py Step C 每步调用
输入: turn_state, step_content, is_empty, stop_reason, block_types, ...
输出: AssistantTurnDecision(kind, assistant_content, user_content, ...)

第1步: treat_as_progress=True?
  └─ 是 → kind="progress" + 附带步骤感知 nudge

第2步: 可恢复思考中断 + 还有重试次数?
  ├─ 是 → record_recoverable_thinking_retry()
  │       kind="progress" + runtime_event_category="recovery"
  │       max_tokens → resume_after_max_tokens
  │       pause_turn → resume_after_pause
  └─ 否 → 进入第3步

第3步: 空响应 + 还有重试次数?
  ├─ 是 → record_empty_response_retry()
  │       kind="retry" + 根据阶段选 nudge
  │       ├─ verify 阶段 → 验证模式空响应提示
  │       ├─ allow_widening → 拓宽比较提示
  │       ├─ saw_tool_result → nudge_after_empty_response
  │       └─ 无工具结果 → nudge_after_empty_no_tools
  └─ 否 → 进入第4步

第4步: 空响应但重试耗尽?
  ├─ 确定 stop_reason：
  │   ├─ late_verify + 有结果 → "verification_failed"
  │   ├─ widen_ready → "widen_needed"
  │   └─ 其他 → "blocked"
  └─ kind="fallback" + 根据有无工具结果和错误数构建 fallback 文本

第5步: 验证守卫被触发?
  ├─ verify 阶段 + requires_evidence + 内容未引用证据
  └─ 是 → kind="progress" + runtime_event_category="guard"
       └─ 附带 build_verification_evidence_nudge()

第6步: 默认 → kind="final" + protect_final_answer=True + stop_reason="done"
```

### 3. decide_tool_turn() — 判断工具结果的下一步

```
调用位置: agent_loop_lite.py Step D 每个工具结果处理
输入: tool_name, result_output, await_user
输出: ToolTurnDecision(kind, progress_summary)

第1步: await_user=True?
  ├─ 是 → kind="await_user" + stop_reason="await_user"
  │       progress_summary="awaiting user after {tool_name}"
  └─ 否 → kind="continue"
          progress_summary="processed tool result from {tool_name}"
```

---

## 三、9 个数据结构速查

| 数据结构 | 星级 | 创建时机 | 被谁使用 | 核心字段 |
|:--------:|:----:|---------|---------|---------|
| `TurnRecurrentState` | ★★★☆ | Prelude | 全程 Step A/B/C/D | step/max_steps/error_count/widening_active |
| `TurnStepPolicy` | ★★★☆ | Step A | Step A + C | phase/guidance/verification_focus/widening |
| `TurnVerificationState` | ★★★☆ | TurnRecurrentState 持有 | Step A + C | strict/evidence_summary/requires_explicit_final |
| `AssistantTurnDecision` | ★★★☆ | decide_assistant_turn 返回 | Step C | kind/assistant_content/user_content |
| `ToolTurnDecision` | ★★★☆ | decide_tool_turn 返回 | Step D | kind/stop_reason/progress_summary |
| `TurnCodaSummary` | ★★★☆ | Coda | Coda | step/tool_error_count/success/stop_reason |
| `TurnPreludeState` | ★★☆☆ | Prelude | Step A | task/task_graph/auditor |
| `StableTaskPack` | ★★☆☆ | Step A | Step A（塞进消息） | task_title/progress_summary/evidence |
| `TurnBudgetSignals` | ★★☆☆ | TurnRecurrentState 刷新 | Step A | remaining_steps/hit_max_steps |

---

## 四、agent_loop 与 turn_kernel 对应关系

```
agent_loop_lite.py 调用点                turn_kernel 提供
─────────────────────────                ────────────────
Prelude: turn_state = TurnRecurrentState  ← TurnRecurrentState 数据结构

Step A: derive_turn_step_policy()        → TurnStepPolicy
Step A: render_turn_policy_message()     → str | None（策略有变化才返回）
Step A: build_stable_task_pack()         → StableTaskPack（任务摘要塞进消息）

Step C: decide_assistant_turn()          → AssistantTurnDecision
                                          kind="progress" / "retry" / "fallback" / "final"

Step D: decide_tool_turn()              → ToolTurnDecision
                                          kind="continue" / "await_user"

Coda: build_turn_coda_summary()          → TurnCodaSummary
Coda: finalize_work_chain_task()         → None（更新任务状态）

Widen: build_widening_transition_nudge() → str（拓宽提示文本）
Verify: build_verification_evidence_nudge() → str（验证证据提示文本）
```
