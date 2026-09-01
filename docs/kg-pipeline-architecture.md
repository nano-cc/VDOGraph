# 视频解析与知识图谱构建 总体设计方案

> 版本：v1.1（2026-08-19，补删除两阶段流程、analyze 写锁豁免论证、观测性指标，修正性能预算矛盾）
> 范围：视频上传 → 解析分片 → 图谱构建（抽取/消歧/社区）→ 入库 → 问答检索
> 重点：并发架构（分布式锁、读写模型）、分布式限流、幂等与故障恢复

---

## 1. 目标与设计原则

**目标**：用户上传视频后，系统自动完成视频解析与知识图谱构建，构建结果支持可溯源的图谱问答。

**原则**：
1. **正确性优先于吞吐**：图写操作全局有序，宁可排队不可竞态
2. **能并行的充分并行**：无共享状态的环节（解析、抽取）最大化并发
3. **一切可断点续跑**：每个阶段的产出持久化，崩溃后重跑不重复劳动
4. **单机起步，分布式就绪**：编排层（Java）承载分布式原语，扩容不改 Python 内部逻辑

---

## 2. 总体架构

```
用户上传（前端）
   │
   ▼
Java 后端（Spring Boot）
   │  ① 视频入 MinIO，媒体记录入 MySQL
   │  ② KgBuildService 触发构建（单机直调 / 分布式走 RocketMQ）
   ▼
Python AI 服务（FastAPI）
   ├─ /pipeline/analyze  解析 + 抽取（无锁并发）
   │     ├─ 视频解析：FFmpeg 分片 → ASR（并发）→ 关键帧 → OCR（进程池并发）→ 对齐合并
   │     ├─ 立即保存 Media + Segment（断点续跑基础）
   │     └─ 实体关系抽取：每片段 1 次 LLM（并发），raw_extraction 逐片段落库
   │
   └─ /pipeline/commit   图写入（全局互斥，Redisson 锁）
         ├─ 实体消歧：三层级联（L1 精确 → L2 模糊 → L3 向量+LLM）
         ├─ 关系对齐去重：canonical_id 对齐 + 相似度合并
         ├─ 实体/关系 MERGE 入 Neo4j
         └─ 社区增量更新：向量召回候选 → LLM 归属判断 → 归并摘要 / 新建社区
   │
   ▼
Neo4j 5.18（系统记录：图数据 + 向量索引 + 全文索引）
   ▲
   │  只读无锁（读已提交，最终一致）
   ▼
问答检索：4 路召回（名称向量/描述向量/BM25/BFS）→ RRF 融合 → BGE 精排 → DeepAgents
```

---

## 3. Neo4j 数据模型

```
(Media {id, title, path, duration_ms, title_embedding})
   │ HAS_SEGMENT
   ▼
(Segment {id, media_id, segment_index, start_ms, end_ms,
          transcript, ocr_texts, frame_urls,
          transcript_embedding, ocr_embedding,
          raw_extraction, extracted_at})
   ▲ MENTIONED_IN {segment_index, confidence, source_type, surface_form}
   │
(Entity {id, name, type, description, aliases, source_count,
         name_embedding, description_embedding})
   │ RELATES_TO {id, description, strength, confidence,
   │             description_embedding, source_count, source_segment_ids}
   ▼
(Entity) ──BELONGS_TO──► (Community {id, level, parent_id, entity_count,
                                     summary, summary_embedding})
                           │ CONTAINS
                           ▼
                        (Segment)
```

**索引**：7 个向量索引（实体名/实体描述/关系描述/社区摘要/片段 ASR/片段 OCR/媒体标题）+ 4 个全文索引（实体/关系/片段/社区）+ 普通 id 索引。

**溯源链**：任意实体/关系/社区 → 片段（时间戳 + 证据帧 URL）→ 原视频。

---

## 4. 阶段一：analyze（解析 + 抽取，无锁并发）

### 4.1 视频解析
1. 视频源：本地路径或 HTTP（MinIO URL 自动下载到临时文件）
2. FFmpeg 音视频分离，音频按 60s 分段 → ASR 转写（**4 路并发**，段间独立）
3. 视频全局关键帧提取（场景检测 + 感知哈希去重）→ Tesseract OCR（**进程池并发**）→ 帧上传 MinIO（**按 media_id 命名空间** `frames/{media_id}/`，避免跨视频覆盖）
4. 60s 窗口对齐合并 → VideoContext（每片段 = transcript + ocr_texts + evidence_frames）
5. **立即保存 Media + Segment 到 Neo4j**（含 transcript/ocr embedding）——断点续跑基础

### 4.2 实体关系抽取
- 每片段 1 次 LLM 调用（GraphRAG 式 prompt，实体+关系一次输出），**5 路并发**（片段间零共享）
- 每片段抽完立即将原始结果写入 `Segment.raw_extraction`
- **幂等**：`raw_extraction` 存在的片段直接跳过（重跑/断点续跑零重复 LLM 调用）

### 4.3 并发安全性
- 无共享状态：ASR/OCR/抽取均为纯外部调用 + 各自片段的写
- 同视频防重：Redis `SETNX kg:building:{mediaId}` 原子占位
- **analyze 阶段写 Neo4j（Media/Segment/raw_extraction）为何豁免写锁**：写入的节点集合与写锁保护的共享结构（Entity/Relationship/Community）不相交——Media/Segment 是视频私有的（media_id 由 MySQL 自增，新上传必为新 id，不存在同 id 并发写）；消歧 L3 只查 Entity 向量索引，不受新 Segment 影响；删除操作的孤儿清理以 MENTIONED_IN 为前提，也不会误伤正在写入的 Segment

---

## 5. 阶段二：commit（图写入，全局互斥）

进入条件：Java 编排点持有 Redisson 锁 `kg:graph:write`。

### 5.1 实体消歧（三层级联，串行）
- L1 精确匹配（规范化名字哈希表，O(1)）
- L2 字符串模糊（SequenceMatcher ≥ 0.85，熵门控）
- L3 LLM 判断：embedding → 两路候选（内存中本视频已处理实体 + Neo4j 向量索引查历史视频实体）取 Top-5 → LLM 判重（带理由）
- 每个原始实体盖 `canonical_id` 章；合并保留原名入 `aliases`；`sources` 记录 `surface_form`
- **描述归并（三级门控，2026-08-30 起）**：判同命中后新描述先收集，入库前批量门控——① 字面包含跳过 ② 与现有描述 embedding 余弦 ≥ 0.88 判语义重复跳过 ③ 有新信息追加（零 LLM）；追加后超 800 字的实体在 commit 末尾批量 LLM 压缩（每实体最多 1 次）。参照 Graphiti（追加+超阈值压缩）与 GraphRAG（多描述 LLM 归并）的折中，变更过的描述重算 embedding 保证与文本一致
- **为何串行**：检索-判断-写入是 check-then-act，并发必竞态（两实体同时未检到对方 → 重复新建）

### 5.2 关系对齐去重（串行）
- 降级链：片段内 canonical_id（消歧盖章）→ 全局规范化别名表 → 丢弃（计数+日志）
- 同实体对 + 描述相似度 ≥ 0.8 合并；关系 id = 实体对 + 描述哈希（重跑幂等）

### 5.3 入库（MERGE 幂等）
- 全部 MERGE 按确定性 id：重复执行不产生重复节点/边

### 5.4 社区增量更新（Graphiti 式，串行）
- 新实体 → 向量召回 Top-3 候选社区 → LLM 归属判断
  - 命中：BELONGS_TO + LLM 归并社区摘要 + 重算摘要向量 + CONTAINS 片段
  - 未命中/无候选：新建单实体社区（摘要=实体描述，不调 LLM 防幻觉）
- 成本优化：Top-1 候选相似度低于阈值时跳过 LLM 判断直接新建（阈值经历史数据分布统计确定，向量粗筛 + LLM 细判的两层结构与消歧 L3 一致）
- 已有社区的实体跳过（天然幂等）
- **为何串行**：摘要归并是读-改-写，并发 lost update；新建社区有顺序依赖（先建者的社区可被后来者并入）
- 定期 Leiden 全量重跑纠偏（层次化社区重建，持同一把写锁）

### 5.5 删除流程（两阶段）

图写操作，与构建/全量重跑共享 `kg:graph:write` 锁。

**阶段一（持锁毫秒级，用户无感）**：
- 删 Media 节点、删该视频的 Segment（DETACH，MENTIONED_IN/HAS_SEGMENT 边随之消失）
- MySQL 媒体记录、MinIO 视频文件由 Java 侧删除
- 至此用户视角"视频已删除"，释放锁

**阶段二（异步排队清理，持锁执行）**：
1. 关系边：`source_segment_ids` 过滤该视频片段，空则删边
2. 孤儿实体（无任何 MENTIONED_IN）DETACH DELETE，连带其 RELATES_TO/BELONGS_TO
3. 空社区（成员清零）DETACH DELETE；存活社区重算 entity_count
4. MinIO 证据帧按前缀 `frames/{media_id}/` 清理
5. 重算实体/关系 source_count

**删除的并发交互**：

| 场景 | 处理 |
|---|---|
| 删除 vs 构建/全量重跑（写-写） | 全局写锁互斥（孤儿清理、空社区清理会触碰共享实体/社区，必须串行） |
| 删除 vs 查询（读-写） | 无锁；读者引用变少，不会看到错数据；溯源播放 404 按加载失败处理 |
| 同一视频重复删除 | 幂等（MATCH 无行为空操作） |
| 构建中途删除（同视频） | 协作式取消：Redis 标记，pipeline 步骤边界检查后中止并清现场 |

**已知取舍**：
- **社区摘要内容残留**：存活社区的摘要由增量归并生成，可能仍描述已删除实体的信息，entity_count 重算但摘要不自动"遗忘"。由定期 Leiden 全量重跑重新生成摘要兜底（增量模型的固有特性，Graphiti 相同）
- **阶段二失败**：异步清理失败会留孤儿数据（可观测：日志 + 定期对账任务兜底），不影响主流程

---

## 6. 并发架构

### 6.1 读写模型

| 操作对 | 是否互斥 | 依据 |
|---|---|---|
| 写-写（构建/删除/全量重跑 之间） | **必须互斥**（全局写锁） | 共享图状态的 check-then-act、读-改-写 |
| 读-写（查询 vs 一切写操作） | **不需要互斥** | Neo4j 读已提交：读者只见已提交数据，稍旧但不脏 |
| 读-读 | 无锁 | — |

- 全量重跑/删除媒体的"先删后建"窗口：读者看到降级结果（社区暂时为空），不产错误答案；前端可用 `rebuilding` 标记提示
- 删除媒体同样持全局写锁（孤儿清理、空社区清理会触碰共享实体/社区）

### 6.2 分布式锁（Redisson）

- 锁名：`kg:graph:write`，Java 编排点获取（覆盖 commit 全程）
- 崩溃安全：watchdog 自动续期；持锁实例崩溃 → 锁过期自动释放，不死锁
- 多 Java 实例、多 Python worker 均正确（锁在 Redis，与进程无关）
- Python 内部无锁（所有写路径必经 Java 编排点；API key 限制调用方）

### 6.3 阻塞调用与事件循环
- 锁内所有阻塞调用（LLM/embedding/Neo4j）走线程池（`asyncio.to_thread`），避免持锁期间冻结事件循环导致查询接口停摆

### 6.4 已知边界与对策

| 边界情况 | 对策 |
|---|---|
| MinIO 帧跨视频覆盖 | media_id 命名空间 |
| 同视频重复触发/双击重试 | Redis SETNX 原子占位 |
| 构建中途删除视频 | 协作式取消：步骤边界检查 Redis 取消标记，命中则中止并清现场 |
| 锁内崩溃半成品 | MERGE 幂等 + raw_extraction 断点续跑，重跑自愈 |
| 多视频 commit 顺序与上传顺序颠倒 | 接受（只影响标准实体命名，不影响正确性） |
| 锁公平性（Redisson 默认非公平锁） | 当前并发度无感；量级上来后换 Redisson fair lock |

---

## 7. 分布式限流（外部 LLM/embedding API）

SiliconFlow 是全局共享资源，多副本并发必须统一限流，三层防护：

1. **分布式信号量（在途并发）**：Redis INCR/DECR + 许可租约（TTL 60s，持有者崩溃自动归还）
2. **令牌桶（RPM）**：Redis Lua 原子取令牌，所有副本共享一桶
3. **429 退避重试**：指数 backoff + jitter（同时修复 embedding 无重试的已知缺陷）

---

## 8. 幂等与故障恢复

| 故障点 | 恢复机制 |
|---|---|
| 解析中断 | 重跑整个 analyze（FFmpeg/ASR/OCR 重跑，分钟级可接受） |
| 抽取中断（12 段跑了 7 段） | `raw_extraction` 已落库，重跑只处理剩余段，零重复 LLM |
| commit 中断 | MERGE 幂等 + canonical_id 可重放，重跑自愈 |
| 同视频重跑 | 解析/抽取全跳过；消歧对已有实体合并率 ~100%，图零增长 |
| 删除视频 | 共享实体/关系只移除该视频来源，空来源才删；孤儿实体/空社区连带清理 |

---

## 9. 性能预算（12 分钟视频，实测基线 → 目标）

| 阶段 | 基线 | 优化后 | 手段 |
|---|---|---|---|
| 解析 | 195s | ~60s | ASR 4 并发 + OCR 进程池 |
| 抽取 | 529s | ~110s | 5 路并发 |
| commit（消歧+去重+入库） | 1055s | ~905s | LLM 串行保留；embedding 批量（-135s）+ Neo4j 批量写（-45s） |
| 社区增量 | 780s | ~500s | 无候选跳过 LLM 判断（阈值经分布统计确定） |
| **单视频总计** | ~45min | **~26min** | — |
| 多视频并发总吞吐 | 串行 45min/个 | analyze 并行 + commit 排队 | — |

---

## 10. 部署形态演进

**单机（当前）**：Java 直调 Python 两接口；Redisson 锁在单 Java 实例内生效（Redis 已就位，天然分布式）。

**多机（需要时）**：
```
Java ×N（Redisson 锁/状态）→ RocketMQ kg-build 队列
  → Python worker ×N（analyze 无状态水平扩展）
  → commit 经 Redisson 全局写锁全序化 → Neo4j（系统记录）
```
- analyze 崩溃：MQ 重投（断点续跑幂等）
- commit 崩溃：锁过期释放 + MERGE 重跑自愈
- commit 串行是全局图谱的固有天花板；进一步提速的演进选项：锁外 LLM 出提案、锁内只做应用+复查（两阶段提交）

---

## 11. 明确不做（防止为优化而优化）

- 微服务注册发现/配置中心（业务量不需要）
- gRPC 替换 REST（内部调用，REST 足够）
- 读写锁/读阻塞（Neo4j 事务模型已保证读者安全）
- 分库分表/图分区（单图规模远未到）

---

## 12. 观测性（随实施补齐）

架构落地时应同步建设的度量指标（现有日志已覆盖大半，缺聚合）：

| 指标 | 用途 |
|---|---|
| 各阶段耗时（解析/抽取/消歧/社区/入库） | 性能回归基准，验证 §9 预算 |
| LLM/embedding 调用次数与 token 消耗 | 成本核算；验证跳过类优化（社区判断跳过）收益 |
| 写锁等待时长 / 持有时长 | commit 串行瓶颈观测；决定是否需要两阶段提交演进 |
| 关系丢弃数（未映射实体名） | 抽取质量监控，异常升高说明 prompt/模型退化 |
| 429 次数与退避总时长 | 限流参数调优依据 |
| 社区并入率 / 新建率 | 增量更新健康度；新建率异常高说明归属判断或阈值有问题 |
