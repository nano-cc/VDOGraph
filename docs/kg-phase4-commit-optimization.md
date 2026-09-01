# KG Commit 四期优化设计：两跳消歧 + 锁时分离 + 实体级细粒度锁

> 版本：v1.0（2026-08-31 讨论定稿）
> 前置：docs/kg-pipeline-architecture.md、docs/async-and-lock-design.md、docs/kg-reference-research-notes.md
> 原型验证：ai-service/experiment/intra_video_merge_test.py、two_hop_disambiguation_test.py
> 关联任务：#68（算法增强）、四期（乐观两阶段演进）

---

## 1. 背景与目标

### 现状问题（联调实测）

| 问题 | 实测数据 |
|---|---|
| commit 持 per-user 锁全程，LLM 调用在锁内 | 32 片段视频 commit 80+ 分钟，同用户多视频全串行排队 |
| 消歧逐实体调 LLM | 366 实体视频需 300+ 次调用 |
| 社区归属逐实体 LLM | 每新实体 1 次 |

### 目标

1. **LLM 调用降一个数量级**：消歧批量判重（实测省 95%）
2. **锁内时间从分钟压到秒**：决策全部移出锁，锁内只做写入
3. **同用户并发提交并行化**：实体级细粒度锁取代 per-user 全局串行
4. **图质量不降**：误判率不升（宁缺毋滥原则不变）

### 核心思想

> **把"想"和"写"分开**：想（消歧决策、归属判断、LLM 调用）在锁外并行做；写（MERGE、社区应用）在锁内秒级做。锁的粒度从"用户全图"细化到"本次写集涉及的实体/社区 id"。

---

## 2. 总体架构

```text
analyze（不变）：解析 → 逐片段抽取 → raw_extraction 落库

commit 拆两阶段：

prepare_commit（无锁，多视频并行）
  ① 第一跳：视频内预合并（零 LLM 零图访问）
  ② 对图 L1/L2（只读）
  ③ 第二跳：批量 LLM 判重（只读图 + LLM）
  ④ 社区归属决策（邻居投票/向量召回+LLM，只读）
  产出：CommitPlan {决议表, 写集 id 列表, 快照时间}

apply_commit（细粒度多锁，秒级）
  ⑤ KgMultiLock 按序加锁（写集 id 排序）
  ⑥ 幻影检查 + 增量复查
  ⑦ MERGE 实体/关系/MENTIONED_IN（确定性 id 幂等）
  ⑧ 社区应用
  ⑨ 按序放锁
```

---

## 3. 第一跳：视频内预合并（零 LLM）

输入：本视频全部 Segment 的 raw_extraction 摊平（实体 + 关系）。
对齐 Graphiti L1/L2（`dedup_helpers.py`），中文适配。

### 3.1 L1 精确分组

- 规范化：小写 + 压缩空白（对齐 `_normalize_string_exact`）
- 规范化相同 → 同组（哈希表 O(n)）
- 实测对 ASR 大小写不稳定收益巨大（IPHONE/iphone/Iphone 合一）

### 3.2 L2 模糊合并（保守）

- 规范化：保留 `[a-z0-9' 一-鿿]`（Graphiti 原版不含中文，必须适配）
- 门控：Shannon 熵 ≥ 1.5 且（长度 ≥6 或词数 ≥2）——短名/低熵名直接拒判
- 相似度：去空格切 3-gram shingle 集合，Jaccard ≥ 0.9
- 补充规则（视频场景扩展）：包含关系（短名 ⊂ 长名）且通过熵门控 → 合并
- 单字差异对（养牛/养猪、CAT/RAT MEAT 类）**不合并**，记入 borderline_pairs 交第二跳 LLM
- 并查集（union-find + 路径压缩）做传递闭包：A≈B, B≈C → 同组
- MinHash/LSH 不引入：它是大池子加速层，视频内规模 O(n²) 精确算即可

### 3.3 组内聚合

每组产出一个视频级实体：

| 字段 | 规则 |
|---|---|
| name | 组内最完整（最长）的名字 |
| type | 组内众数 |
| descriptions | 全部保留（归并门控留给图级） |
| surface_forms | 全部名字变体（后续进 aliases） |
| sources | 全部来源片段编号 |
| mention_count | 组内成员数（证据强度） |

### 3.4 关系预合并

- 端点按组映射（含全部 surface_forms）
- 同 (组A, 组B) 对内：描述相似度 ≥0.8 合并；strength 取最大；source_count 累计；sources 并集
- 端点映射不上的丢弃（计数+日志）

### 3.5 实测效果（experiment/intra_video_merge_test.py + two_hop_disambiguation_test.py）

| 视频 | 原始实体 | 第一跳后 | 压缩 |
|---|---|---|---|
| media 7（12 段） | 174 | 128 | 26% |
| media 10（32 段） | 444 | 247 | 44% |
| media 12（52 段） | 552 | 366 | 34% |

---

## 4. 第三层：对图 L1/L2（免费，新增）

第二跳 LLM 之前，先对图做免费级联——**实测"并入图级"的实体里大部分是同名的，不该花 LLM**：

```text
每个视频级实体组 vs 本 group 图级实体（一次读入内存建索引）：
  L1：组的全部 surface_forms 逐一查图的规范化哈希索引 → 命中即并入
  L2：组的全部 surface_forms 的 shingle 与图级实体算 Jaccard，取最高分 ≥0.9 → 并入
      （366 × 1000 规模精确算 < 1s；图上 10 万实体后再引入 LSH）
通过的组：决议=并入，不再进 LLM
```

---

## 5. 第二跳：批量 LLM 判重（对齐 Graphiti dedupe_nodes）

### 5.1 候选构造（按实体挂候选，不用共享大池子）

共享池实测会膨胀到近千个候选（media 12：632 图级 + 366 视频内），prompt 8 万 token。
改为**每个待判实体自带 Top-K 候选**：

```text
待判实体 X 的候选 =
    图级向量召回 Top-5~8（X 的名字 embedding，cosine ≥ 0.5，group 闭包）
  + 视频内相似实体（X 与其他视频级实体名字向量余弦 > 0.5 的）
  + borderline_pairs 中涉及 X 的对端（强制进候选，防漏判）
```

候选描述截断 120 字。每批 20 个待判实体一次调用 → prompt ≈ 1 万 token（可控）。

### 5.2 prompt（对齐 Graphiti dedupe_nodes.py:117-178）

四件套：视频上下文（首片段 transcript 截断 800 字）+ 待判实体（id 编号，含 surface_forms）+ 各自候选（candidate_id 编号）+ few-shot。

few-shot 覆盖四类边界（实测问题驱动）：
- 缩写/变体（NYC → New York City）
- 同名异物（Java 语言 vs 爪哇岛 → -1）
- 同义称呼（毛主席 → 毛泽东）
- **单字语义翻转（养牛上市公司 vs 养猪上市公司 → -1）**——实测误伤驱动新增

约束：必须返回恰好 N 条判决；只有指向同一真实对象才判重；related but distinct 绝不判重；返回更完整的标准名。

### 5.3 防御性后处理（对齐 Graphiti node_operations.py:467-625）

- id 越界 / 重复 id / 无效 candidate_id → 按新建处理
- 漏判 → 按新建处理
- LLM 调用失败 → 本批全部新建
- **原则：错放过（软错误）优于错合并（硬错误），LLM 失效安全降级为新建**

### 5.4 链式决议闭环

LLM 可能判 A→视频内 B，而 B 自己判→图级 C。应用前并查集二次闭包，A 直达 C，不出现中间态。

### 5.5 实测效果（two_hop_disambiguation_test.py）

| 视频 | 视频级实体 | LLM 调用（批量） | LLM 调用（逐实体旧方案） | 节省 |
|---|---|---|---|---|
| media 7 | 128 | 7 | ~128 | 95% |
| media 10 | 247 | 13 | ~247 | 95% |
| media 12 | 366 | 19 | ~366 | 95% |

叠加第三层对图 L1/L2 后，LLM 调用还会再降（同名实体免费判掉，预估再减 60%）。

---

## 6. 社区分配：三层兜底链（对齐 Graphiti determine_entity_community）

**只有新建实体需要社区分配**（并入已有实体的不动）。

```text
新实体：
  ① 邻居众数投票（零 LLM，对齐 Graphiti）
     MATCH (c:Community)<-[:BELONGS_TO]-(m:Entity)-[:RELATES_TO]-(新实体)
     邻居的社区众数，票数 ≥ 2（阈值可调）→ 加入
     前提：apply 内先写关系边再做社区分配（顺序保证邻居存在）
  ② 向量召回 Top-3 社区 + LLM 判归属（现有逻辑保留，作第二兜底）
  ③ 单实体社区保底（防漏）
     id 确定性：community_{group}_{entity_id}；摘要=实体描述（不调 LLM 防幻觉）
```

加入已有社区时：LLM 归并摘要（pair-wise，对齐 Graphiti summarize_pair）+ 重算摘要向量 + BELONGS_TO 边。

孤儿残留风险（两个并发视频的孤儿各建相似社区）：接受，定期 Leiden 全量重建合并。

---

## 7. 并发与锁设计

### 7.1 读写模型

| 操作 | 锁 |
|---|---|
| prepare 全程（第一跳/对图匹配/LLM 判重/归属决策） | 无锁 |
| 问答查询 | 无锁（现状不变） |
| apply（写图） | 细粒度多锁（写集 id） |

### 7.2 写集与锁集合

prepare 结束时写集完全可枚举：

```text
锁集合 = 合并目标实体 id
       ∪ 新建实体的确定性 id（entity_{group}_{规范化名}，创建前即可算）
       ∪ 涉及社区 id（加入目标的社区 id / 新建社区确定性 id）
关系不单独加锁（rel_{src}_{tgt}_{desc_hash} 确定性 id + MERGE 幂等天然安全）
```

### 7.3 KgMultiLock（kglock.py 扩展）

- 写集 id 按字典序排序后逐把 SET NX PX（防死锁）
- 任何一把失败 → 释放已获得全部 → 退避重试（避免占锁干等）
- 统一 watchdog：持锁期间对所有 key 按 ttl/3 续期
- 持锁进程崩溃 → TTL 过期自动释放；MERGE 幂等保证部分写入可重跑

### 7.4 幻影处理

| 幻影类型 | 解法 |
|---|---|
| 同名幻影（并发新建同名实体） | 确定性 id 同一把锁串行化，后到者 MERGE 降级为合并 |
| 语义幻影（并发新建近似名实体） | apply 锁内增量复查：查 snapshot_time 之后本 group 新建实体，与我的新建实体做向量比对，撞上了补 LLM 判重/合并 |
| 残余漏网 | 定期 Leiden 全量重跑兜底合并（Graphiti 同款取舍） |

### 7.5 单视频内串行，跨视频并行

- 同一视频的消歧/社区处理在 prepare 内部保持串行（视频内实体要互相看见）
- 不同视频的 prepare 完全并行；apply 只在写集交集上排队

### 7.6 版本校验（v2 可选，先不做）

Entity/Community 加 version 属性，prepare 记版本、apply 校验、变了局部重做。
v1 靠"确定性 id + 增量复查 + MERGE 幂等"三件套，监控（#67 锁等待时长/冲突率）说需要了再加。

---

## 8. 幂等与故障恢复

| 故障点 | 恢复 |
|---|---|
| 第一跳/判重中断 | raw_extraction 还在，重跑 prepare 即可（纯计算） |
| apply 写一半崩溃 | 确定性 id + MERGE，重跑自愈；锁 TTL 自动释放 |
| LLM 批次失败 | 该批降级新建（软错误） |
| 重复执行 | 全链路幂等：视频内合并确定性、判重输入确定性、MERGE 幂等、社区 BELONGS_TO 已有跳过 |

---

## 9. 实施计划

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | `app/services/intra_video_merger.py`（第一跳，纯函数） | 单测 + 实测脚本复跑（对比 §3.5 数据） |
| 2 | `disambiguator.py` 加 `disambiguate_batch`（第三层对图 L1/L2 + 第四层批量 LLM），旧逐实体路径保留，开关切换 `KG_TWO_HOP=true` | 对比测试：同视频新旧路径各跑到独立 group，比实体数/判决抽样 |
| 3 | `community_updater.py` 拆 plan/apply + 邻居投票优先 | 单测：构造邻居场景/孤儿场景 |
| 4 | `kglock.py` 加 KgMultiLock | 单测：并发写集相交/不相交、死锁防护 |
| 5 | `video_processing_pipeline.py` commit 拆 prepare_commit/apply_commit | — |
| 6 | `kg_task_runner._run_commit` 接入；Python 槽位占位修复（已改未部署）随本次部署 | — |
| 7 | eval 回归（eval_harness Hit@k/MRR 不回退）+ 联调 | docs/kg-governance-impl-progress.md 记录 |

兼容：开关默认关，新旧路径并存；数据模型不变（Entity/Community 结构不变，无迁移）。

---

## 10. 已知取舍

1. 语义幻影有残余漏网率（增量复查阈值外的近似实体并存）→ Leiden 重跑兜底，Graphiti 同款取舍
2. 孤儿实体的社区可见性延迟（邻居投票+LLM 都不中时单实体社区保底，此问题已最小化）
3. 社区摘要"遗忘"问题不变（删实体摘要不自动遗忘，Leiden 重跑兜底）
4. 批量判重的批次内 LLM 全局视野弱于共享大池子（按实体挂候选的取舍：成本/质量平衡，实测验证）
5. 锁数量增加（每 apply 几十把 Redis 锁，~1ms/把，可接受；部分失败回滚重试）

## 11. 参考源码位置

| 主题 | 位置 |
|---|---|
| Graphiti L1/L2（熵门控/shingle/Jaccard/MinHash） | .external-research/graphiti/graphiti_core/utils/maintenance/dedup_helpers.py |
| Graphiti 批量判重（prompt + 防御解析） | graphiti_core/utils/maintenance/node_operations.py:467-625、prompts/dedupe_nodes.py |
| Graphiti 并查集批量去重 | graphiti_core/utils/bulk_utils.py:374-486 |
| Graphiti 邻居投票社区 | graphiti_core/utils/maintenance/community_operations.py:274-367 |
| Graphiti 摘要 pair-wise 归并 | community_operations.py:141-155 |
| GraphRAG 文本单元合并 | graphrag/index/operations/extract_graph/extract_graph.py:104-129 |
| GraphRAG Leiden 固定 seed | graphrag/graphs/hierarchical_leiden.py:11-26 |
