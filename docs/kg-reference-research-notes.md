# Graphiti / GraphRAG 源码调研笔记 —— 可借鉴点清单

> 日期：2026-08-31
> 源码位置：`.external-research/graphiti`（getzep/graphiti 浅克隆）、`.external-research/graphrag`（microsoft/graphrag 浅克隆），该目录已 gitignore
> 用途：#66 KG 治理专项之后的算法增强参考；本文只记录结论与关键位置，细节回源码查

---

## 1. 三方对比速查

### 实体抽取

| | 本项目 | GraphRAG | Graphiti |
|---|---|---|---|
| prompt 格式 | `<\|>` 分隔 tuple | `<\|>` tuple + `##` 记录分隔 + `<\|COMPLETE\|>` 结束符 | 结构化 JSON |
| 多轮补抽 | ❌ 单轮 | ✅ gleaning：CONTINUE_PROMPT 补抽 + LOOP_PROMPT 问 Y/N，默认 1 轮 | 单次 |
| 实体类型约束 | prompt 约束 | prompt 约束，解析不强制过滤 | Pydantic schema 强约束 |
| 结束标记 | 无 | `<\|COMPLETE\|>`（可区分抽完/被截断） | — |

GraphRAG 关键位置：`prompts/index/extract_graph.py`（GRAPH_EXTRACTION_PROMPT）、`index/operations/extract_graph/graph_extractor.py:85-122`（gleaning 循环）、分隔符硬编码在 31-33 行。

### 描述归并

| | 本项目 | GraphRAG |
|---|---|---|
| 策略 | 三级门控（字面包含跳过 / 余弦≥0.88 跳过 / 追加）+ 超 800 字 LLM 压缩 | 同名实体描述直接 LLM summarize，限 500 词，超 4000 token 分段递归压缩 |
| LLM 成本 | 大多数零调用（更省，保持） | 每个多描述实体一次 |

GraphRAG 关键位置：`prompts/index/summarize_descriptions.py`、`index/operations/summarize_descriptions/description_summary_extractor.py:75-118`。

### 消歧（实体解析）

| | 本项目 | Graphiti |
|---|---|---|
| 结构 | L1 精确 / L2 模糊 / L3 向量+LLM | 同为三级级联（resolve_extracted_nodes） |
| L2 算法 | SequenceMatcher 编辑相似度，阈值 0.85 | 3-gram shingle + MinHash + LSH + Jaccard，阈值 0.9 |
| 熵门控 | 长度≥6 或词数≥2 | Shannon entropy ≥1.5 + 长度≥6 + 词数≥2（更精细） |
| L3 候选 | Top-5，cosine>0.5，内存+Neo4j 两路 | Top-15，cosine≥0.6，只走向量 |
| type promotion | ❌ | ✅ 命中后把具体类型（如 Person）合并到泛化 Entity 上 |

Graphiti 关键位置：`utils/maintenance/node_operations.py:627-708`、`utils/maintenance/dedup_helpers.py:220-279`、prompt 在 `prompts/dedupe_nodes.py:117-178`（注意其反例约束："related but distinct 绝不能判重"）。

### 社区检测与更新

| | 本项目 | GraphRAG | Graphiti |
|---|---|---|---|
| 算法 | Leiden（igraph+leidenalg，缺装静默回退连通分量） | Leiden（graspologic_native，seed=0xDEADBEEF 固定可复现） | 标签传播 |
| 层次 | level 0/1/2 递归细分，max_cluster_size=10 | hierarchical_leiden 原生多层，同参数 | 无层次 |
| 增量分配 | 向量召回 Top-3 + LLM 判归属 | delta concat + id 偏移（较糙） | 邻居社区众数投票（零 LLM） |
| 社区报告 | 50-100 字纯文本摘要 | title + summary + rating(0-10) + findings 数组 + 引用溯源 | LLM pair-wise merge |

GraphRAG 关键位置：`graphs/hierarchical_leiden.py:11-26`、`prompts/index/community_report.py:5-153`（COMMUNITY_REPORT_PROMPT）。
Graphiti 关键位置：`utils/maintenance/community_operations.py:274-338`（determine_entity_community 邻居众数）。

### 检索

| | 本项目 | GraphRAG | Graphiti |
|---|---|---|---|
| local 召回 | 4 路（名称向量/描述向量/BM25/BFS depth=2）+ RRF(k=60) + BGE 精排 | 实体向量召回 → 拼社区报告+关系+text unit 原文 | BM25+向量+BFS(depth=3) + RRF(rank_const=1) + 5 种 reranker |
| global 问答 | 社区向量+BM25 召回 → 直接问 LLM | map-reduce：分批 map 打分(0-100) → 滤 0 分 → 排序聚合 reduce（max_data_tokens=12000） | 社区走混合检索 |
| token 预算 | 无明确分配 | community 15% / text_unit 50% / local 35% | — |

GraphRAG 关键位置：`query/structured_search/global_search/search.py:142-431`（map-reduce）、`local_search/mixed_context.py:91-222`（预算分配）。
Graphiti 关键位置：`search/search.py:98-250`、reranker 配置 `search/search_config.py:53-78`。

---

## 2. 可借鉴点行动清单（按价值排序）

| 优先级 | 借鉴点 | 来源 | 改动量 | 收益 |
|---|---|---|---|---|
| ⭐⭐⭐ | gleaning 多轮补抽（CONTINUE_PROMPT + LOOP_PROMPT，默认+1 轮） | GraphRAG | 小：entity_extractor 加循环 | ASR 噪声文本的抽取召回率 |
| ⭐⭐⭐ | `<\|COMPLETE\|>` 结束标记 | GraphRAG | 极小 | 区分"抽完"与"被 max_tokens 截断" |
| ⭐⭐⭐ | global search map-reduce（分批打分→滤零分→排序聚合） | GraphRAG | 中：重写 graph_retriever.global_search | 社区多时不爆 context、答案质量 |
| ⭐⭐ | 社区报告加 findings 结构化字段（summary+explanation 数组） | GraphRAG | 中 | global 问答信息密度 |
| ⭐⭐ | Leiden 固定随机种子 | GraphRAG | 极小 | 可复现、可回归测试 |
| ⭐⭐ | 社区增量分配加"邻居众数投票"零成本预筛，高置信直接入、不确定才调 LLM | Graphiti | 中 | 减少 LLM 归属判断调用 |
| ⭐ | local search token 预算分配（15%/50%/35%） | GraphRAG | 中 | context 利用率 |
| ⭐ | L3 候选 5→15、阈值 0.5→0.6 | Graphiti | 极小 | 消歧召回 |
| ⭐ | type promotion（消歧命中后合并具体类型） | Graphiti | 小 | 实体类型准确性 |
| 暂缓 | covariates/claim 抽取、DRIFT search、边的时序失效（valid_at/invalid_at）、prompt tune、NLP 快速抽取路径 | 两者 | 大 | 当前场景用不上 |

---

## 3. 我们的差异化优势（保持，不要回退）

1. **per-user group_id 隔离 + 分布式写锁**：Graphiti 无锁接受竞态（uuid 唯一+后写覆盖兜底），我们 per-user 串行锁更严格，符合"正确性优先于吞吐"
2. **ASR+OCR 双通道视频解析**：两者都是纯文本输入，视频源处理是我们独有
3. **时间戳锚点 + 证据帧溯源**：GraphRAG 的 text_unit 没有视频时间点概念
4. **三级门控描述归并**：比 GraphRAG 每实体 LLM summarize 省成本

---

## 4. 其他备忘

- Graphiti 写并发无锁，靠 Neo4j execute_write 事务保证单次写入原子，读-判-写不原子——我们方案更严
- Graphiti group_id 即租户分区键，所有查询带过滤（与我们三期 group_id 隔离同思路，互相印证）
- GraphRAG 增量更新本质是"delta 跑标准流程 + 各表 concat/id 映射"，不如我们的增量社区归并精细
- GraphRAG 索引中间产物全落 parquet 表（documents/text_units/entities/relationships/covariates/communities/community_reports），双向 text_unit_ids 溯源可参考
- community_detector 依赖 igraph+leidenalg 但未列入 requirements.txt，缺装会静默回退连通分量（已知技术债）
