# memory.py 学习框架
推荐学（按顺序）：
  ① working_memory.py — 小文件，和 agent_loop 耦合多，快速看完
  ② memory_pipeline.py — 理解记忆从"检索→重排→注入"的完整流程
  ③ memory_reranker.py — 理解 LLM 如何重排检索结果
## 文件定位

**三层分层记忆系统**，用于跨会话的知识保留。让 agent 在不同粒度上持久化、检索和利用历史知识——用户偏好、项目规范、代码模式等。

---

## 一、核心数据结构

### 1. MemoryScope（L668）★★☆☆
记忆作用域枚举，决定记忆的**生命周期和可见范围**。

| 作用域 | 路径 | 生命周期 | 用途 |
|:------:|------|:--------:|------|
| `USER` | `~/.mini-code/memory/` | 永久 | 跨项目用户偏好 |
| `PROJECT` | `.mini-code-memory/` | 随项目 | 团队共享规范，可版本控制 |
| `LOCAL` | `.mini-code-memory-local/` | 随项目 | 本地临时笔记，不纳入 VCS |

### 2. MemoryTier（L675）★★☆☆
**记忆层级**（受 Atkinson-Shiffrin 记忆模型启发）

```
WORKING → SHORT_TERM → LONG_TERM → ARCHIVAL
（当前会话） （<7天）    （<30天）    （永久，摘要存储）
```

### 3. MemoryEntry（L694）★★★☆
单条记忆条目。

| 字段 | 说明 | 用途 |
|:----:|------|------|
| `id` | 唯一 ID | 索引查找 |
| `scope` | 作用域（user/project/local） | 隔离不同层级 |
| `category` | 分类（architecture/decision/test 等） | 自动分类 |
| `content` | 记忆内容 | 核心数据 |
| `tags` | 标签列表 | 快速检索 |
| `usage_count` | 被引用次数 | 影响搜索排序 |
| `domains` | 领域分类 | 领域相关性评分 |
| `tier` | 记忆层级 | 升降级管理 |
| `last_accessed` | 最后访问时间 | 时效性衰减 |
| `related_to` | 关联条目 ID 列表 | 记忆关联图 |

### 4. MemoryFile（L791）★★★☆
表示一个作用域下的记忆文件，支持 BM25 搜索。

| 内部结构 | 说明 |
|---------|------|
| `entries` | MemoryEntry 列表 |
| `_id_index` | ID → Entry 索引 |
| `_tag_index` | 标签 → Entry 索引 |
| `_category_index` | 分类 → Entry 索引 |
| `_idf_cache` | 预计算 IDF |
| `_avgdl_cache` | 平均文档长度 |

---

## 二、核心函数详解

### 🔴 ★★★★

| 方法 | 行号 | 调用位置 |
|------|:----:|---------|
| `MemoryManager` 类 | L1069 | 被 agent_loop 和 main.py 创建和使用 |
| `get_relevant_context()` | L1577 | 被 `build_system_prompt_bundle()` 调用，每次 LLM 调用前注入记忆 |

#### 1. MemoryManager 类 — 三层记忆管理器

【为什么需要】大模型智能体在跨会话协作时面临"记忆断层"问题——每次对话都是独立的，无法利用过往决策和项目规范。

**数据流：**

```
add_entry(content)
  │ 自动分类（关键词启发式规则）
  │ 写入 MemoryFile，增量更新索引
  ▼
BM25 索引构建
  │ 构建 ID / 标签 / 分类 三层索引
  │ 预计算 IDF 与 avgdl
  ▼
get_relevant_context(query)
  │ 按 LOCAL > PROJECT > USER 优先级遍历作用域
  │ 每层调 search() → BM25 + 子串 + 标签 + 领域评分
  │ token 预算截断 → 拼接为 MEMORY.md 格式
  ▼
inject_memory_into_prompt()
  └── 将最终上下文追加到 system_prompt 末尾
```

**三层作用域检索优先级：** LOCAL（最贴近当前工作区）→ PROJECT → USER（全局兜底）

#### 2. get_relevant_context(L1577) — 获取相关记忆上下文

```
第1步: scope 优先级排序 — LOCAL → PROJECT → USER
第2步: BM25 检索评分
  score = BM25(q, d) + substring_match + tag_match + domain_score + usage_bonus + recency_bonus
第3步: 相关性过滤 — 归一化后剔除低分结果
第4步: 结果合并去重 — 按前100字符去重，降序排列
第5步: token 预算截断 — 逐条检查 token，超预算的单条跳过
```

---

### 🟡 ★★★☆

| 方法 | 行号 | 说明 |
|------|:----:|------|
| `MemoryEntry` | L694 | 单条记忆条目，含完整字段 |
| `MemoryFile` | L791 | 记忆文件，含 BM25 搜索 |
| `MemoryFile.search()` | L895 | BM25 + 子串 + 标签 + 领域综合搜索 |
| `MemoryFile.add_entry()` | L845 | 添加条目，增量更新索引 |
| `MemoryManager.add_entry()` | L1326 | 添加新记忆（支持自动分类） |
| `MemoryManager.search()` | L1476 | 跨作用域搜索 |
| `_score_entry()` | L1529 | 综合评分（BM25+子串+标签+使用频率+时效性） |
| `MemoryManager.__init__()` | L1104 | 初始化，加载所有作用域 |
| `_tokenize()` | L441 | 分词（含 CJK 支持） |
| `_bm25_score()` | L517 | BM25 算法实现 |

#### 3. BM25搜索流程

```

用户输入查询 → _tokenize() 分词
  │
  ▼
_expand_query_terms() 中英文术语扩展
  │
  ▼
_compute_idf() 计算 IDF
_compute_avgdl() 计算平均文档长
  │
  ▼
_bm25_score(query_tokens, doc_tokens, idf, avgdl)
  └─ score = sum(IDF(qi) * (tf(qi,d) * (k1+1)) / (tf(qi,d) + k1*(1-b+b*|d|/avgdl)))
  │
  ▼
综合评分 = BM25 + 子串匹配 + 标签匹配 + 领域评分 + 使用频率 + 时效性
```

#### 4. MemoryFile.search() L895 — 综合搜索

```
输入: query
输出: 排序后的 MemoryEntry 列表

BM25 评分
  ├─ 词频缩放 k1=1.5
  └─ 文档长度归一化 b=0.75

子串匹配加分
  ├─ 完全匹配 +2.0
  └─ 部分匹配 +1.0

标签匹配加分
  ├─ 完全匹配 +5.0
  └─ 部分匹配 +1.5

领域评分（Jaccard 相似度）
  └─ soft blend: BM25×0.7 + domain×0.3

使用频率加分 log1p(usage_count)×0.3
时效性加分 1/(1+age_hours/24)×0.5
```

---

### 🔵 ★★☆☆

| 方法 | 行号 | 说明 |
|------|:----:|------|
| `_auto_classify_content()` | L623 | 关键词启发式自动分类 |
| `_enforce_limits()` | L987 | 超限时移除最旧条目 |
| `format_as_markdown()` | L997 | 格式化为 MEMORY.md |
| `handle_user_memory_input()` | L1803 | 处理 `/memory add` 命令 |
| `MemoryFile.update_entry()` | L861 | 更新条目 |
| `MemoryFile.delete_entry()` | L873 | 删除条目 |
| `_summarize_content()` | L2187 | 内容摘要（降级时用） |
| `inject_memory_into_prompt()` | L2228 | 将记忆注入系统提示 |

---

### ⚪ ★☆☆☆
所有 `_` 开头的私有方法、`_compute_tf`、`_compute_idf`、`_compute_avgdl`、`_recover_scope`、`_load_scope`、`_parse_memory_md`、`_save_scope`、`_atomic_write`、`get_stats`、`format_stats`、`clear_scope`、`check_integrity`、`compress_scope`、`detect_conflicts`、`decay_memories`、`promote_memories`、`link_memories`、`get_linked_memories`、`_jaccard_similarity`、`_merge_entry_content` 等。

---

## 三、数据流图

```
用户/agent 产生知识
  │
  ├─ 用户主动记忆：/memory add 或 # 指令
  │   → handle_user_memory_input()
  │
  └─ agent 自动沉淀：任务自省/经验提取
      → MemoryManager.add_entry()
        ├─ 自动分类（_auto_classify_content）
        ├─ 写入 MemoryFile
        ├─ 增量更新索引
        └─ _save_scope() → 原子写入磁盘
  │
  ▼
检索阶段（build_system_prompt_bundle 时调用）
  │
  MemoryManager.get_relevant_context(query)
  │  └─ 遍历 LOCAL → PROJECT → USER
  │     └─ MemoryFile.search(query)
  │        └─ BM25 评分 + 子串 + 标签 + 领域 + 使用频率 + 时效性
  │
  ▼
inject_memory_into_prompt()
  └─ 将记忆追加到 system_prompt 末尾
```

## 四、关键设计总结

| 设计 | 解决什么问题 | 怎么实现的 |
|------|------------|-----------|
| 三层作用域 | 不同粒度的知识隔离 | USER/PROJECT/LOCAL 分别存不同路径 |
| BM25 搜索 | 语义相关性检索 | TF-IDF + 文档长度归一化 |
| 中英文术语扩展 | 跨语言搜索 | 300+ 条中英文对照表 |
| 自动分类 | 免手动标签 | 关键词启发式规则匹配 |
| 原子写入 | 防止写入中断导致数据损坏 | 先写 temp 文件，再 os.replace |
| 缓存索引 | 加速频繁搜索 | ID/标签/分类 三层索引 + LRU 缓存 |
| 综合评分 | 不止文本匹配 | BM25 + 子串 + 标签 + 领域 + 使用频率 + 时效性 |
| 反向快照 | 回退安全网 | rewind 前读取当前内容保存为 kind="rewind" |
