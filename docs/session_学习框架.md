# session.py 学习框架

## 文件定位

会话持久化与恢复模块。提供会话的保存、加载、checkpoint 创建、rewind 回退、增量 delta 持久化等功能。

**核心问题解决：** 重启后如何恢复对话状态？文件编辑出错如何撤销？高频自动保存如何避免 IO 爆炸？

---

## 一、数据结构（3 个 dataclass）

### 1. SessionMetadata（L45）★☆☆☆
**会话的轻量级索引元数据**，存储在 `sessions_index.json` 中，用于快速列出所有会话而不加载完整 JSON。

```
session_id         # 会话唯一 ID
created_at         # 创建时间
updated_at         # 最后更新时间
first_message      # 首条用户消息（截断）
last_message       # 最后一条消息（截断）
message_count      # 消息总数
workspace          # 工作目录
checkpoint_count   # checkpoint 数量
runtime_summary    # 运行时摘要
```

### 2. FileCheckpoint（L65）★★★☆
**文件修改前的快照**。每次 write_file/edit_file 执行前记录原文件内容。

```
checkpoint_id      # 唯一 ID（12 位十六进制）
file_path          # 被修改的文件路径
existed            # 文件之前是否存在
previous_content   # 修改前的文件内容快照
kind               # 类型：edit（普通）| rewind（反向安全快照）
group_id           # 分组 ID（原子回退组，同一组分一起回退）
```

### 3. SessionData（L78）★★★☆
**完整会话状态**，包括对话消息、转录、技能、MCP、扩展等全部信息。

```
session_id         # 会话 ID
messages           # 对话消息列表（核心）
transcript_entries # 转录条目列表
checkpoints        # FileCheckpoint 列表
metadata           # SessionMetadata 元数据

# 增量追踪字段（关键设计）
_last_saved_msg_count        # 上次保存时的消息数量
_last_saved_transcript_count # 上次保存时的转录数量
_last_saved_checkpoint_count # 上次保存时的检查点数量
_delta_save_count            # 增量保存累积次数（用于触发全量合并）
```

---

## 二、核心函数详解（按重要性排序）

### 🔴 第一梯队：★★★★（必须读懂代码）

| 方法 | 行号 | 调用位置 |
|------|:----:|---------|
| `save_session()` | L602 | create_file_checkpoint / rewind_session_data / AutosaveManager / tui/session_flow |
| `load_session()` | L732 | get_latest_session / rewind_session / main.py / cli_commands / tui/session_flow |
| `create_file_checkpoint()` | L1052 | file_review.py / paper_a_task_completion_eval.py |
| `rewind_session_data()` | L1162 | rewind_session / cli_commands |
| `rewind_session()` | L1272 | main.py / cli_commands |

#### 1. save_session() — 保存会话

```
全量 vs 增量决策树：

当前满足任一条件？
  A) force_full=True（显式保存命令）
  B) _delta_save_count == 0（首次保存）
  C) _delta_save_count >= FULL_SAVE_INTERVAL（10 次增量后合并）
  D) _delta_save_count >= MAX_DELTA_FILES（50 个 delta 上限）
  │
  ├─ 是 → 全量保存
  │     ├─ 序列化整个 SessionData（messages/transcripts/checkpoints/全部字段）
  │     ├─ 写入 <session_id>.json
  │     ├─ 更新追踪计数器
  │     ├─ 计算内容哈希
  │     └─ _consolidate_deltas() 清理 delta 文件
  │
  └─ 否 → 增量 delta 保存
        ├─ 计算新增消息：messages[_last_saved_msg_count:]
        ├─ 计算新增转录：transcripts[_last_saved_transcript_count:]
        ├─ 计算新增 checkpoint
        ├─ 写入 deltas/<id>/delta_NNNN.json
        └─ _delta_save_count++

最后：更新 sessions_index.json 索引
```

#### 2. load_session() — 加载会话

```
第1步: 检查 <session_id>.json 是否存在 → 不存在返回 None
第2步: 读取 JSON，重建 SessionData 对象
第3步: 扫描 deltas/<session_id>/ 目录
第4步: 按文件名顺序应用 delta 文件：
  ├─ 用 msg_offset 判断偏移
  ├─ 偏移 >= 当前长度 → 直接 extend
  ├─ 部分重叠 → 只追加新增部分
  ├─ 损坏的 delta → 跳过
第5步: 更新追踪计数器
第6步: 返回 SessionData（含已合并的 delta）
```

#### 3. create_file_checkpoint() — 创建检查点

```
第1步: session 为 None → 返回 None
第2步: 生成 checkpoint_id（uuid）
第3步: 构建 FileCheckpoint（含前置内容快照）
第4步: 追加到 session.checkpoints 列表
第5步: save_session(force_full=False) 增量保存
```

#### 4. rewind_session_data() — 回退执行（核心）

```
第1步: _select_checkpoints_to_rewind()
  ├─ 按 steps 回退：从末尾取 N 个，留意 group_id 分组
  └─ 按 checkpoint_id 回退：从后向前查找

第2步: 生成 rewind_group_id（标记本轮回退操作）

第3步: 创建反向安全快照（逆序遍历选中检查点）
  ├─ 去重：同一文件只保留一个反向快照
  ├─ 读取当前文件内容作为 previous_content
  └─ 生成 kind="rewind" 的反向 FileCheckpoint

第4步: 恢复磁盘文件（逆序遍历）
  ├─ 原文件存在 → 恢复 previous_content
  └─ 原不存在且当前存在 → 删除

第5步: 替换检查点列表
  ├─ 删除被恢复的选中检查点
  └─ 追加反向安全快照

第6步: save_session(force_full=True) 全量保存
```

#### 5. rewind_session() — 高层回退入口

```
第1步: load_session(session_id) 加载会话 + delta
第2步: rewind_session_data(session, steps, checkpoint_id) 执行回退
第3步: 返回 (session, selected_checkpoints)
```

---

### 🟡 第二梯队：★★★☆（理解设计意图）

| 方法 | 行号 | 调用位置 |
|------|:----:|---------|
| `create_new_session()` | L972 | tui/session_flow / paper_a_task_completion_eval |
| `get_latest_session()` | L1016 | main.py / cli_commands / tui/session_flow |
| `format_rewind_preview()` | L1324 | cli_commands（--preview-rewind） |
| `_select_checkpoints_to_rewind()` | L1122 | rewind_session_data / format_rewind_preview |

#### 6. create_new_session() — 创建新会话
```
第1步: 生成 12 位随机 uuid
第2步: 构造 SessionData（空消息列表）
第3步: 返回
```

#### 7. get_latest_session() — 获取最近会话
```
第1步: list_sessions() 获取所有会话（按时间倒序）
第2步: 返回第一个匹配 workspace 的会话
```

#### 8. _select_checkpoints_to_rewind() — 选择回退检查点
```
按 steps 回退：
  start = max(len(checkpoints) - steps, 0)
  如果最后一个有 group_id → 向前扩展到同组首个
  
按 checkpoint_id 回退：
  从后向前找到匹配的 checkpoint
  如果有 group_id → 向前扩展到同组首个
```

---

### 🔵 第三梯队：★★☆☆（知道存在即可）

| 方法 | 行号 | 说明 |
|------|:----:|------|
| `list_sessions()` | L886 | 从索引读取所有会话元数据，按时间排序 |
| `_save_delta()` | L520 | 增量保存实现 |
| `_consolidate_deltas()` | L569 | 合并清理 delta 文件 |
| `AutosaveManager` | L1411 | 自动保存管理器（脏标记 + 时间间隔） |
| `delete_session()` | L919 | 删除会话文件 + delta 目录 + 索引 |
| `cleanup_old_sessions()` | L947 | 清理超量旧会话 |

---

### ★☆☆☆（知道名字即可）

| 方法 | 行号 | 说明 |
|------|:----:|------|
| `_session_file()` | L434 | 返回会话文件路径 |
| `_session_delta_dir()` | L446 | 返回 delta 目录路径 |
| `_session_index_file()` | L458 | 返回索引文件路径 |
| `_load_session_index()` | L467 | 加载索引 JSON |
| `_save_session_index()` | L487 | 保存索引 JSON |
| `_serialize_checkpoint()` | L390 | checkpoint → dict |
| `_deserialize_checkpoint()` | L410 | dict → checkpoint |
| `_compute_content_hash()` | L174 | 计算内容 MD5 |
| 所有 `format_*` 函数 | L1568+ | 纯展示格式化 |
| 所有 `_summarize_*` 函数 | L287+ | 摘要字符串生成 |

---

## 三、数据流图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            会话生命周期                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  启动时:                                                                 │
│    create_new_session() → SessionData(空)                                │
│      或                                                                  │
│    load_session(session_id) → SessionData(含历史消息 + delta 合并)        │
│                                                                         │
│  运行中:                                                                 │
│    每步对话 → session.messages.append(msg)                               │
│    每步工具 → session.transcript_entries.append(entry)                   │
│    写文件前 → create_file_checkpoint() → session.checkpoints.append()    │
│                                                                         │
│  自动保存:                                                               │
│    AutosaveManager.save_if_needed()                                      │
│      → save_session(force_full=False)                                    │
│        → should_full_save?                                               │
│          ├─ 是 → 全量 JSON → <id>.json                                  │
│          └─ 否 → 增量 delta → deltas/<id>/delta_NNNN.json               │
│                                                                         │
│  回退时:                                                                 │
│    rewind_session(session_id, steps=N)                                   │
│      → load_session(session_id)                                          │
│      → rewind_session_data(session, steps=N)                             │
│        → 创建反向快照 → 恢复文件 → 全量保存                              │
│                                                                         │
│  退出时:                                                                 │
│    AutosaveManager.force_save() → 全量保存                               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 四、关键设计总结

| 设计 | 解决什么问题 | 怎么实现的 |
|------|------------|-----------|
| **增量 delta 保存** | 高频自动保存导致 IO 压力 | 只保存新增消息/转录/checkpoint，追踪 `_last_saved_*` 偏移 |
| **定期全量合并** | delta 文件碎片化 | `FULL_SAVE_INTERVAL=10`，每 10 次增量后全量合并 |
| **force_full 开关** | 显式保存需要一致性 | `save_session(force_full=True)` 跳过增量判断 |
| **反向安全快照** | 回退后还能再回退 | 回退前创建 kind="rewind" 的反向 checkpoint |
| **group_id 分组** | 相关操作一起回退 | 同 group_id 的 checkpoint 在回退时一起选 |
| **两种回退方式** | 灵活撤销 | steps 回退最后 N 个 / checkpoint_id 回退到指定点 |
| **sessions_index.json** | 快速列出会话不加载完整 JSON | 轻量级元数据索引文件 |
