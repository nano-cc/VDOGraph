# 异步化与并发控制设计方案（v1.0）

> 整理日期：2026-08-28
> 状态：设计已定稿，待实施（上传链路已完成，本文档承接其后）
> 关联文档：kg-pipeline-architecture.md（图谱架构）、上传链路健壮性设计（已完成）

---

## 0. 背景与痛点

知识图谱构建（analyze + commit）当前是"线程池 + 同步 HTTP"：
- Java 后台线程被长 HTTP 调用占住几分钟~几小时（同步阻塞等 Python 返回）
- 读超时被迫放宽到 3h（media 10/12 踩过 1h 超时坑）
- Java 报 FAILED 但 Python 继续跑，状态分裂
- commit 全局串行（Redisson kg:graph:write），多用户/多视频排队，吞吐天花板 ~50min/个

目标：异步化（投递即返回）+ 锁粒度细化 + 吞吐按用户扩展。

---

## 1. 总体异步架构（一期二期已落地）

> 实施时按项目实际收敛为：一期 HTTP 异步（202 后台任务），二期单 topic `kg-build` MQ 化。
> 下图为最初的双 topic 设计稿，保留作将来 Python 直连 MQ 时的参考；现行实现见第 6 节二期落地要点。

### 1.1 消息队列拓扑（RocketMQ，设计稿）

| Topic | 消费形态 | 用途 |
|---|---|---|
| `kg-build-analyze` | 消费组并行（4 队列） | analyze 任务（宽阶段，吞吐扩展） |
| `kg-commit` | **单消费者** | commit 任务（串行语义白拿，免显式锁） |
| `kg-build-dlq` | 死信 | 失败沉淀，人工/定时重放（复用 video-analysis-dead 模式） |

队列数 4 足够：真正瓶颈是下游 LLM API 速率（令牌桶 ~2/s），不是 worker 数。

### 1.2 消息粒度（已定：按视频）

- analyze：**一条视频一条消息** `{taskId, mediaId, videoUrl, userId, attempt}`
  - 不选按片段：解析前置阶段是整视频操作（FFmpeg/关键帧/合并），且真正瓶颈是 LLM 限流非 worker 数；按片段是文档化的远期扩展路径（大 worker 组/小时级视频时再切）
- commit：**一条视频一条消息** `{taskId, mediaId, attempt}`
  - **不能按片段**：实体消歧需要视频级全局视图（片段 3 的「澳洲」与片段 5 的「澳大利亚」判同），是视频级闭包操作

### 1.3 流程

```
上传完成 → Java KgBuildService.triggerBuild
  ├─ SETNX 防重 + 写 Redis 任务状态机（kg:task:{mediaId}）
  └─ 投递 kg-build-analyze → 立即返回（<50ms，线程释放）
        ↓
Python analyze worker（消费组并行；单 worker 内 asyncio 并发：ASR 4 路/OCR 6 路/抽取 5 路）
  ├─ 逐片段写进度到 Redis（"7/12"）
  └─ 完成 → 投递 kg-commit {taskId, mediaId}
        ↓
Python commit（单消费者 = 免费全局串行）
  └─ 消歧 → 去重 → 入库 → 社区增量 → 写 SUCCESS/FAILED 到 Redis
        ↓
Java KgTaskWatcher（@Scheduled 15s）
  ├─ 同步状态给前端 /kg/status
  ├─ 失败 attempts<2 自动重投；仍失败 → DLQ
  └─ 心跳停滞（>30min）判定死亡 → 重投
```

### 1.4 状态机

```
UPLOADED → QUEUED → ANALYZING → COMMITTING → SUCCESS
                     │              │
                     └──────┬───────┘
                            ▼
                  FAILED（attempts<2 自动重投）→ DLQ
                  CANCELLED（删除视频协作取消）
```

进度语义：`ANALYZING 7/12`（片段级）、`COMMITTING 45/103`（实体级），前端徽章升级为百分比。

---

## 2. 背压限流体系（MQ 即限流器）

### 2.1 核心思想

- **任务级限流 = worker 数量**（架构属性，无需代码）：LLM 调用速率 = worker 数 × 单 worker 速率，天然受限
- MQ 是缓冲池：生产端随便冲（上传 20 个视频 = 20 条消息堆着），下游按能力消化，压力停在消息里不占线程
- 对比令牌桶：令牌桶让**线程等**（占着资源等令牌），MQ 让**消息等**（不占资源）

### 2.2 三层限流（保留必要部分）

| 层 | 机制 | 说明 |
|---|---|---|
| 任务级 | MQ worker 数 | 替代现在的分布式在途信号量（可删） |
| API 级 | Redis Lua 令牌桶（**保留**） | 防 429；与 Redisson RRateLimiter 同算法，Python 侧这就是标准实现 |
| 故障级 | 指数退避重试 + jitter | 防抖动 |

注意：worker 少且固定（1~3 个）时，本地令牌桶静态切分额度即可，**不需要 Redis 分布式桶**；动态扩容时再升级（Redisson RRateLimiter 仅 Java 可用，Python 用 Redis Lua 等价实现）。

### 2.3 生产限流实践备忘

- 算法：令牌桶（允许突发）/漏桶（平滑）/固定窗口/滑动窗口；限并发≠限速率，慢调用场景限并发常够用
- 工具：Guava/Resilience4j RateLimiter、Redisson RRateLimiter、Sentinel、网关（Kong/Nginx）
- 下游保护三件套：限流 + 429 退避重试 + 熔断

---

## 3. 锁体系设计

### 3.1 各端实现（已定）

| 端 | 用途 | 选型 |
|---|---|---|
| Java | kg:graph:write 全局写锁 | Redisson（已就位，watchdog 防死锁） |
| Python | commit 自持锁（异步化后） | redis-py Lock / aioredlock（PX + 看门狗续期） |

锁的位置跟着执行方走：异步化后 commit 在 Python 后台执行，锁从 Java Redisson 迁移到 Python redis 锁。

### 3.2 锁粒度演进（关键性能设计）

```
现在：  kg:graph:write（系统级全局锁）→ 多用户/多视频全排队
未来：  kg:graph:write:{userId}（per-user）→ 用户间并行，用户内串行
```

**为什么现在必须全局**：图谱全局共享（无 group_id），跨用户实体是同一节点，check-then-act 竞态跨用户存在。
**为什么 per-user 可行**：group_id 隔离后图不相交，竞态消失。

### 3.3 kg-commit 单消费者 = 免费串行

commit topic 单消费者消费 → 同一时刻全局只有一个 commit 执行 → 串行性内建于消费形态，显式分布式锁只留兜底。

---

## 4. group_id 用户隔离（必做，最大头）

### 4.1 方案（Graphiti 式）

- 所有节点（Media/Segment/Entity/Community）和边加 `group_id` 属性（= userId）
- 向量索引/全文索引查询加 group 过滤（消歧 L3 候选、检索三路都限定 group）
- 消歧、社区均在 group 内闭包
- 锁按 `kg:graph:write:{userId}` 分

### 4.2 双收益

1. 数据隔离正确性：用户 A 的内容不再被用户 B 搜到（当前是产品级 bug）
2. 并行度按用户扩展：不同用户的 commit 完全并行

---

## 5. 乐观并发与免锁写入（性能密码）

### 5.1 两阶段乐观并发（OCC）

把 50 分钟临界区压到毫秒级：

```
锁外（并行，99% 时间）：消歧的检索 + LLM 判断 → 产出"提案"（新建 X / 并入 Y）
锁内（毫秒级）：应用提案 + L1 精确复查（哈希 O(1)）
  → 并发窗口冒出同名实体 → 改判并入；否则 MERGE 写入
```

竞态窗口从 50 分钟缩到毫秒，复查兜底。

### 5.2 确定性 ID + 数据库 MERGE

```
L1 精确判重：id = hash(规范化实体名) → Neo4j MERGE 原子幂等
  → 同名列写入委托数据库，免应用层锁
L2/L3 模糊判重：走 5.1 乐观两阶段
```

原则：能委托数据库唯一性/原子性的，不在应用层加锁。

### 5.3 per-community 细粒度锁

社区摘要归并（读-改-写）从全局锁拆出：`kg:lock:community:{communityId}`，不同社区归并并行。

### 5.4 组合效果

```
group_id 分片（用户间并行）
× 确定性 ID + MERGE（L1 免锁）
× 乐观两阶段（L2/L3 锁外提案 + 锁内毫秒应用）
× per-community 锁（摘要归并细粒度）
= 全局锁名存实亡：锁数量 1→N（用户级），持锁时间 50min→毫秒级
```

---

## 6. 实施分期

| 期 | 内容 | 验证标准 |
|---|---|---|
| **一期（核心异步化）** ✅ 已实现（2026-08-28） | Python `build-async`（202 后台任务 + Redis `kg:task:{mediaId}` 状态机 + 逐片段/分阶段进度 + KgLock 自持锁）+ Java KgTaskWatcher | 投递 13ms 返回；QUEUED→ANALYZING→COMMITTING→SUCCESS 状态机可见；重复投递幂等；/kg/status 新旧状态兼容 |
| **二期（消息化解耦）** ✅ 已实现（2026-08-28） | RocketMQ `kg-build` topic 生产消费替换直调 + 失败重投（broker 重试 + watcher 兜底）+ broker DLQ | Java/Python 宕机不丢任务；投递 3ms 级 |

二期落地要点（与设计稿的差异）：
- **单 topic `kg-build`**，不是 analyze/commit 双 topic。原因：RocketMQ 5.3.4 未部署 gRPC proxy，Python 没有可用的 5.x 客户端；项目既有范式（VideoAnalysisConsumer）就是"Java 消费 → HTTP 调 Python"。commit 串行仍由 Python KgLock 保证（单 Python 实例天然单执行体）。将来 Python 多 worker 扩展时，加 rmq-proxy + Python gRPC 客户端，再把 kg-commit 拆成独立 topic
- 消费端职责极薄：校验 → 调 `build-async`（202）→ ACK。执行可靠性仍由状态机+watcher 负责；消费失败（Python 不可达/429 背压）抛异常走 broker 重投（maxReconsumeTimes=2 后进 broker DLQ）
- **背压**：Python `MAX_CONCURRENT_BUILDS=3`，超限返回 429 → 消费端重投，任务堆在 broker 不占资源
- **排过的坑**：消费端/执行端的"进行中跳过"预检不能覆盖 QUEUED 态——初始 QUEUED 是投递方在发消息*之前*写的，还没有执行体，跳过等于把首次投递吞掉（E2E 实测踩中，media 20 卡排队，修复后恢复）。只有 ANALYZING/COMMITTING + 新鲜心跳才是"有人在跑"的铁证
- watcher 重投从直调改为发 MQ 消息（attempt+1）
| **三期（隔离+优化）** ✅ 已实现（2026-08-29） | group_id 全链路 + per-user 锁 | 双用户测试通过（见下） |

三期落地要点（与设计稿的差异）：
- **向量检索不用 ANN 索引**：Neo4j 5.18 向量索引不支持元数据预过滤，改为「group_id B-tree 预过滤 + `vector.similarity.cosine` 精确计算」——单用户实体 10³ 量级精确算是毫秒级，召回反而无损（ANN 是近似）。向量索引保留但不再被检索路径使用；将来百万级规模走 Qdrant（已部署）payload 过滤
- **实体 id 嵌入 group**：`entity_{group_id}_{规范化名}`，跨用户同名实体物理上是两个节点，全局 MATCH by id 不会误伤；关系 id 由实体 id 派生，天然继承隔离
- **存量迁移**：全部回填 group_id='user_2'（9 media/172 片段/1056 实体/211 社区/1393 关系），旧格式 id 保留可用
- **锁**：`kg:graph:write:{group_id}`，commit/delete 全部 per-user；删除的孤儿实体/空社区清理限定 group（防误删其他用户 commit 中途的暂态孤儿）
- **消息**：KgBuildMsg 带 userId，watcher 从 Redis hash 的 group_id 解析重投
- 验证：检索确定性隔离（同 query user_2 引用 media[8,12]、user_3 引用 media[22]）、B 问 A 内容答"没有相关信息"、同名实体双节点、双用户并行构建各持各锁、B 删视频 A 数据零影响
| **四期（性能精化）** | 乐观两阶段 + per-community 锁 + 协作取消（删除视频即时中止） | 临界区毫秒级 |

一期落地要点（与上文设计稿的差异）：
- 端点定为 `POST /pipeline/build-async`（analyze+commit 一条后台任务串完，不拆两个端点；二期接 MQ 时再拆 analyze/commit 两条消息）
- 同步 `/pipeline/commit` 与 `DELETE /pipeline/media/{id}` 也改为 Python 自持 `kg:graph:write` 锁（KgLock），Java 侧 Redisson 锁完全下线——所有图写执行都在 Python，锁跟执行方走
- Java `getStatus` 读 `kg:task` hash；进行中态（QUEUED/ANALYZING/COMMITTING）映射 RUNNING 兼容前端 + 回退旧 `kg:build:` key 兼容历史数据
- KgTaskWatcher：FAILED 或心跳停滞（>30min）且 attempt<2 → 带 video_url 重投（video_url 存在任务 hash 里）；超限保持 FAILED 等人工（二期 DLQ）

---

## 7. 已完成的上传链路（本文档的前置）

- S3 multipart 直传（预签名 URL，数据不过 Java）+ xxHash64 秒传 + 断点续传（quickHash 找回）
- 健壮性：complete 幂等（Redis done 标记 + MySQL quickHash 双检查）、init 互斥（SETNX 归并）、活跃任务上限（≤5/用户）、大小/分片数校验（2GB）、分片重试+过期重签、字节数校验、孤儿 multipart 定时清扫（@Scheduled）
- ffprobe 内容校验（complete 时伪装/损坏拦截）
- 测试：test_s3_upload.py（T1-T8 全过）、test_upload_robust.py（R1-R4 全过）

## 8. 待办（todo 清单）

- #53 下线老分片上传协议（/media/init-upload 等老端点，新链路稳定后）
- 异步化一~四期（见第 6 节）
- group_id 实施（三期主线）
- CORS 白名单（上线前）
