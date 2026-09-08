# KG 治理专项（#66）实现与联调进度

> 任务：#66 KG analyze/commit 拆分 + kg_build_tasks 任务表 + Python 回调 Java
> 开始：2026-08-31
> 中断恢复指引：读本文档「当前进度」→ 找到下一个未勾选项 → 按「设计要点」继续
> 设计全貌见任务 #66 描述及本文档第 2 节

---

## 1. 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | Flyway 迁移 V5：kg_build_tasks 表 | ✅ 完成 |
| 2 | Java：KgBuildTask 实体 + Mapper（含对账 SQL） | ✅ 完成 |
| 3 | Java：KgTaskStateService（MySQL 条件更新 + Redis 快照 + Cache-Aside 查询） | ✅ 完成 |
| 4 | Java：triggerBuild 改造（先建档后发 kg-analyze，主键防重取代 kg:building） | ✅ 完成 |
| 5 | Java：KgAnalyzeMsg/KgCommitMsg + 两个 Consumer（含 attempt 归属心跳校验、media_files 存活预检、commit 乱序延迟重投）；旧 KgBuildConsumer/KgBuildMsg 已删除 | ✅ 完成 |
| 6 | Java：KgInternalController（/internal/kg/state、analyzed、finished、failed；X-Internal-Key 鉴权，409 中止） | ✅ 完成 |
| 7 | Java：KgTaskWatcher 重写（MySQL 名单、四卡点、attempt 归属校验、指数退避、kg:retry:lock 重投锁、Redis 集体失心跳保护） | ✅ 完成 |
| 8 | Java：KgReconcileTask 对账（每 10min 扫 media_files×kg_build_tasks） | ✅ 完成 |
| 9 | Java/Python 错误分类（Python classify_error：FileNotFoundError/ValueError/404/ffprobe 等 → retryable=false） | ✅ 完成 |
| 10 | Java 编译通过（mvn compile） | ✅ 完成 |
| 11 | Python：java_callback.py（重试 3 次指数退避，409→CallbackRejected 中止） | ✅ 完成 |
| 12 | Python：kg_task_runner 拆 start_analyze/start_commit，独立注册表+并发上限（analyze 3 / commit 2），心跳带 attempt | ✅ 完成 |
| 13 | Python：pipeline 端点 analyze-async / commit-async（build-async 保留兼容） | ✅ 完成（py_compile 通过） |
| 14 | 联调：上传 → analyze → 回调 → commit → SUCCESS 全链路 | ✅ 完成（21/21 SUCCESS） |
| 15 | 联调：异常路径（杀 Python、停 MQ、删除视频中途） | ⏸ 部分覆盖（Python 重启/投递失败/误操作自愈已验证；删除中途取消未测） |

**当前进度：阶段 1-13 完成（代码全部写完，Java 编译通过，Python 语法通过）。**
**下一步：阶段 14 全链路联调。**

### 联调前检查清单
1. 基础设施在跑：MySQL(3307)、Redis(6379)、RocketMQ(9876)、MinIO(9000)、Neo4j(7687)
2. MySQL 需执行 V5 迁移（Flyway 随 Java 启动自动跑）
3. RocketMQ 需有 kg-analyze / kg-commit topic（首次发消息自动创建或手动建）
4. Java 配置 `kg.internal.api-key`（空=放行，本地可空）；Python `.env` 配 `JAVA_BACKEND_URL=http://localhost:9090`
5. 启动顺序：Python ai-service → Java server → 上传视频 → 观察 kg:task:{mediaId} + kg_build_tasks 表

### 联调观察点
- MySQL：status 应按 QUEUED → ANALYZING → ANALYZED → COMMITTING → SUCCESS 推进，analyze_attempt/commit_attempt 递增正确
- Redis：kg:task:{mediaId} 的 updated_at 持续刷新，attempt 字段与当前阶段一致
- 前端：/kg/status 返回 RUNNING + phase（ANALYZING/COMMITTING）+ progress
- 异常路径：kill Python → 30min 心跳超时 Watcher 重投（测试时可临时调小 HEARTBEAT_STALE_MS）

---

## 2. 设计要点（实施时遵守）

### 状态机
```
QUEUED → ANALYZING → ANALYZED → COMMITTING → SUCCESS
QUEUED/ANALYZING 失败 → ANALYZE_FAILED；COMMITTING 失败 → COMMIT_FAILED
删除视频 → CANCELLED
```

### 存储分工
- MySQL `kg_build_tasks`：唯一事实源。UNIQUE(media_id)。
- Redis `kg:task:{mediaId}`：热缓存（心跳/进度/前端轮询），TTL 7 天。
- 关键状态转换：先写 MySQL（条件更新 WHERE status IN (...)），成功后删 Redis。
- 心跳/进度：只写 Redis；任何写 Redis 状态 hash 的操作必须同时刷新 updated_at。

### MQ
- 新 topic：`kg-analyze`、`kg-commit`（旧 `kg-build` 保留一段时间做兼容，新代码不再投递）。
- Python 不直连 MQ；analyze 完成后回调 Java `POST /internal/kg/analyzed`，由 Java 原子完成「写 MySQL ANALYZED + 发 kg-commit」。

### Watcher 四卡点
| 卡点 | 依据 | 阈值 | 动作 |
|---|---|---|---|
| QUEUED 超时 | MySQL updated_at 年龄 | 5min | 重发 kg-analyze |
| ANALYZING 心跳超时 | Redis updated_at（带 attempt 校验） | 30min | 标 ANALYZE_FAILED → 重发 kg-analyze |
| ANALYZED 超时 | MySQL updated_at 年龄 | 5min | 重发 kg-commit |
| COMMITTING 心跳超时 | Redis updated_at（带 attempt 校验） | 30min | 标 COMMIT_FAILED → 重发 kg-commit |

重投前三关：① media_files 存活 ② attempt < 上限(2) ③ 重读最新状态。

### P0 纪律
1. UNIQUE(media_id) + INSERT ... ON DUPLICATE 防重（取代 kg:building）
2. MySQL 状态转换全部条件更新，禁止裸写
3. 心跳值带 attempt，Watcher 校验归属
4. Consumer/Watcher 执行前查 media_files 存活
5. 错误分类：永久错误（文件 404/损坏/参数非法）直接 FAILED 不重试
6. 进度更新兼任心跳（写 progress 必须同时刷新 updated_at）

### 兼容性
- 前端 `/kg/status` 返回格式不变（RUNNING/SUCCESS/FAILED + phase + progress + message）
- Python 旧 `/pipeline/build-async` 端点保留（内部转调新逻辑），Java 新代码改用 analyze-async
- Redis 旧 key `kg:build:*` 读取兜底逻辑保留

---

## 3. 联调记录

### 2026-08-31（第一阶段联调）

**环境**：dev-up.sh 起 MySQL/Redis/Qdrant/MinIO/RocketMQ；Neo4j 单独 `docker start neo4j`（不在 compose 里）。Python/Java 后台跑，日志在 logs/ai-service-kg66.log、logs/server-kg66.log。

**已验证 ✅**
1. Flyway V5 自动执行，kg_build_tasks 表结构正确
2. 主链路：上传 complete → triggerBuild → kg-analyze MQ → KgAnalyzeConsumer → Python analyze-async 202 → ANALYZING（mediaId=36 全程走通）
3. 回调机制：Python analyze 完成/失败回调 Java 正常（transition_analyzing / failed 都验证过）
4. 对账任务：历史视频（mediaId 7-35）无任务记录 → KgReconcileTask 自动补建（20 条）
5. Watcher 四卡点之 queued-stale：批量 QUEUED 超 5min → 自动重投 kg-analyze（16:52:08 整批触发）
6. 两阶段衔接：mediaId=7 从 ANALYZING → ANALYZED → COMMITTING 全程自动推进（16:52:29）
7. attempt 归属校验：Redis 心跳带 attempt，consumer/watcher 校验归属

**发现并修复 🐛**
1. `KgBuildTask` 缺 `@TableId(type=IdType.INPUT)` → MyBatis-Plus BaseMapper 方法 BindingException。已修复重启。
   - 注意：mediaId=35 是修复前上传的，complete 成功但 triggerBuild 抛异常 → 被对账补建，顺带验证了对账价值
2. `classify_error` 误判：ASR 503（瞬时）被 ValueError isinstance 规则判成永久。已修复为**瞬时信号优先 + 默认 retryable=true**，Python 已重启
   - mediaId=36/18 已手动 UPDATE retryable=1 等 Watcher 重试
3. **commit attempt 未持久化（真 bug）**：/internal/kg/analyzed 回调发 kg-commit 时用 commit_attempt+1 作消息 attempt 但没写库 → MySQL commit_attempt=0 与 Python 心跳 attempt=1 永远 mismatch，attempt 归属校验失效；且 Python 重启后残留 COMMITTING 无法被回调接管（409）。已修复：新增 markCommitDispatched 条件自增，先发计数再发消息
4. **联调副作用**：重启 Python 导致 6 个 commit runner 被杀、状态冻结 COMMITTING → 手动 UPDATE 为 COMMIT_FAILED 由 Watcher 回收（17:01 全部以 commit_attempt=1 重新 COMMITTING）
5. **Python 并发上限被突破（真 bug，19:15 实测 4 个 commit 同时 queued，上限 2）**：start_analyze/start_commit 的"计数检查→注册任务"之间有 await，批量 MQ 消息同时到达时多个协程交错通过检查。asyncio 单线程事件循环里 await 点就是切换点。已修复：`_reserve_slot` 同步占位（检查+占位注册之间无 await）+ `_fill_slot` 填真实 Task + 异常 `_release_slot`。**代码已改未重启**（批次在跑，等完结后随下次部署生效）。实际无危害（per-user 锁兜底串行），但浪费协程且突破资源上限

**联调中观察到的设计瑕疵（记录，不阻塞）**
- ~~背压消耗重试预算~~ → **已真实发生**：Python commit 槽满 429 → MQ 消息重投耗尽进 DLQ → ANALYZED 停 5min → Watcher 重投并消耗 commit_attempt → 13 个任务 attempt 耗尽卡在 ANALYZED。已手动 `UPDATE commit_attempt=0` 解冻。
  **正式修复项（#66 遗留）**：派发计数与失败重试计数必须分离——Watcher 对 QUEUED/ANALYZED 无执行体卡点的重投属于"衔接断裂补投"（不该消耗失败重试预算），只有"执行体真实失败"才该消耗。建议加 `dispatch_count`（不设上限或上限很高）与 `retry_attempt`（上限 2）分开计数
- SiliconFlow ASR 间歇 503：analyze 失败走瞬时重试路径（符合预期）
- **联调误操作教训**：手动 UPDATE 任务状态前必须确认 runner 真死（36 实际在锁上排队被我误判为僵尸）。但也因此验证了三重防护：409 回调拒绝、Python 注册表防重（already running skip）、commit MERGE 幂等。36 会由 Watcher 重投后幂等重跑至 SUCCESS

**当前进度：阶段 1-14 完成，批次联调全部通过 ✅（21/21 SUCCESS）**
**下一步：阶段 15 异常路径深度测试（可选）+ #64/#65 部署联调（代码已改完，需重启 Java 后验证）**

### 最终批次结果（2026-08-31 19:30）
- **21/21 全部 SUCCESS**，全部有 completed_at 和 stats 归档
- 覆盖路径：正常链路、对账补建、QUEUED 超时重投、ASR 503 瞬时失败重试、Python 重启后心跳超时回收、409 回调拒绝、幂等重跑、背压（72 次 429 MQ 重投）、联调误操作自愈
- 前端 /kg/status 兼容验证通过（RUNNING + phase 映射）
- 大视频实测：12 号 333 实体 / 10 号 212 实体均成功

### 联调发现并修复的 bug（共 5 个）
1. KgBuildTask 缺 @TableId → BindingException（已修）
2. classify_error 误判 ASR 503 为永久错误（已修：瞬时信号优先+默认可重试）
3. commit attempt 未持久化导致归属校验失效（已修：markCommitDispatched 先计数再发消息）
4. Python 并发上限被突破：start_analyze/start_commit 检查-注册间有 await，批量消息交错通过（已修：_reserve_slot 同步占位，**代码已改未部署**）
5. （流程瑕疵）背压消耗重试预算 → 13 任务卡 ANALYZED，手动解冻。正式修复项见下

### 遗留修复项（随 #67 或下次迭代）
- **派发计数与失败重试计数分离**：dispatch_count（宽松）与 retry_attempt（上限 2）分开；ANALYZED/QUEUED 超时补投不消耗失败预算
- Python 槽位占位修复随下次部署生效
- #64/#65（Redisson 锁 + complete 锁）代码完成，待重启 Java 后联调

---

## 4. 其他任务的代码改动（#64/#65，已改完未联调，等批次跑完再验证）

### #64 Java 端统一 Redisson 锁（代码完成 ✅ 编译通过，未部署）
转换了 3 把真锁：
1. `UrlImportConsumer` 执行租约：`setIfAbsent+40min TTL` → `RLock.tryLock()` + finally unlock（watchdog 自动续期覆盖长下载，崩溃 30s 释放）
2. `KgTaskWatcher` 重投节流锁：`setIfAbsent 60s` → `tryLock(0, 60s lease)`（不主动释放，靠 lease 过期节流；多实例安全）
3. `S3UploadService` init 互斥锁：`setIfAbsent+sleep(2s)` → `RLock.tryLock(2s)`（等待内置在 tryLock 里，语义不变）

**保留不动**（语义是幂等标记不是互斥锁，换 RLock 会出错）：
- `AnalysisDispatchService` analysis:active 占位、`FailedAnalysisTaskService` replay 占位、`TranscriptionTaskService` transcription:active 占位、`S3UploadService` done 标记/qh 映射

已是 Redisson 的：VideoAnalysisConsumer 分析锁、AiService 上下文锁、ChunkUploadService 合并锁、ModeRouter 限流。

### #65 S3 complete-upload 分布式锁（代码完成 ✅ 编译通过，未部署）
- `completeUpload` 拆出 `doCompleteUpload` 执行体
- 外层：两道幂等检查 → `RLock upload:s3:complete:{uploadId}` tryLock(等10s, lease 10min) → 锁内复检两道幂等 → 执行 → finally unlock
- 等锁超时：轮询 done 标记 5 次（每秒一次），拿到则幂等返回，否则报"正在完成中，请勿重复提交"

---

## 5. #68 两跳消歧实现与直接测试（2026-08-31 晚）

### 实现（全部完成，语法通过，KG_TWO_HOP 开关默认关）
- `app/services/intra_video_merger.py`：第一跳。Graphiti 对齐的 L1（规范化哈希）+ L2（3-gram Jaccard≥0.9 + Shannon 熵≥1.5，中文适配+包含关系补充）+ 并查集闭包 + 边界对输出 + 关系预合并
- `app/services/batch_disambiguator.py`：第二跳。对图 L1/L2（带全组变体匹配）+ 批量 LLM（每实体 Top-K 候选：图级向量召回+视频内相似+边界对强制）+ few-shot（含养牛/养猪单字翻转示例）+ 防御解析 + 链式闭环
- `app/services/community_updater.py`：邻居众数投票优先（Graphiti determine_entity_community 对齐，阈值≥2 票）→ 向量+LLM 兜底 → 单实体保底
- `app/core/kglock.py`：KgMultiLock（字典序加锁防死锁、失败全放退避重试、统一 watchdog）
- `video_processing_pipeline.py`：prepare_commit_two_hop（锁外）/ apply_commit_two_hop（KgMultiLock 写集锁 + 幻影检查 + 增量复查 + 关系旧值并集 + per-group 社区锁）
- `neo4j_client.py`：新增 get_group_entities / get_entities_created_after
- `kg_task_runner.py`：KG_TWO_HOP 开关接入（默认 False，新旧并存）

### 直接测试（experiment/test_two_hop_commit.py，绕开 MQ/Java 用存量 raw_extraction）
**用例 1（media 7 @ user_2，已建图，测合并幂等）**：
- prepare 170ms，128 组全部 graph_l1 命中，0 次 LLM 调用
- 并入 128 / 新建 0；社区全 skip；重跑零新增 → 幂等 ✅

**用例 2（media 7 @ user_999，空图，测新建）**：
- prepare 319s（7 次批量 LLM，旧方案需 ~128 次逐实体）
- 新建 124 + 视频内互并 4；社区邻居投票命中 17（零 LLM）、LLM 判 84、单实体 25
- 落库 126 实体 / 123 关系 / 126 入社区，溯源边完整 ✅

**数字核对**：user_2 的 128 组并入 105 个不同图级实体 = 23 组共享目标（图的 aliases 吸引），非丢数据。测试后 user_999 数据已清理，Redis 无残留锁。

### 遗留事项
- KG_TWO_HOP 默认关；生产开启前先跑 eval 回归（eval_harness Hit@k/MRR 对比）
- 联调时重启 Python 生效（当前运行的 Python 是旧代码）
- 视频内链式合并的 aliases 写入存在"同 id 两 payload 后者覆盖"的小瑕疵（v1 接受）
- igraph/leidenalg 依赖仍未入 requirements.txt

### 待联调项（下次部署窗口）
~~1. 重启 Java 验证 #64/#65 行为（URL 导入、S3 init 并发、complete 并发双击）~~ ✅ 已随部署生效
~~2. 重启 Python 生效：并发槽占位修复 + KG_TWO_HOP 开关~~ ✅ 已生效（KG_TWO_HOP=true 已写入 .env）
3. 异常路径测试：kill Python 验证心跳超时重投
4. 前端实际上传 + KG 构建 UI 全流程
~~5. KG_TWO_HOP=true 后跑 eval 回归 + 一个真实新视频全链路~~ ✅ 完成（见第 6 节）

---

## 6. 两跳部署 + eval 回归 + 生产首跑（2026-09-01 06:14）

### 部署
- .env 加 KG_TWO_HOP=true；Python/Java 双双重启，#64/#65/槽位修复/两跳全部生效
- 坑：pkill java 时注意 RocketMQ broker/namesrv 也是 java 进程（在 docker 里），别误杀

### eval 回归（ai-service/eval_harness.py，修复了三期遗留 bug：没传 group_id 跑不了）
- Hit@5: 0.898→0.939 📈 MRR: 0.413→0.434 📈 F1: 0.205→0.222 📈
- 污染率 0.159→0.220 📉（图规模增长代价，盯 global 类问题）
- segment 路召回 78.6% 是短板（3 道未命中全是细节召回）

### 两跳生产首跑（mediaId=37，90s 哈夫曼树课程视频）
- 全链路：上传→analyze→回调→kg-commit→两跳 prepare（11s 零锁）→KgMultiLock apply（6s）→社区（64s）→SUCCESS，总耗时 ~2.5min
- 对图 L1 免费并入 5 个实体（同系列已有）；第二跳 7 疑难实体 1 次批量 LLM
- 实体质量抽查：核心概念（哈夫曼树/带权路径长度/叶子节点/权值）准确，关系结构合理
- 残留噪声是抽取层问题（OCR 垃圾串、幻灯片单字母节点 B/E）→ #68 剩余项（gleaning/prompt 优化）负责

### 下一步
- #68 剩余：gleaning 补抽、<|COMPLETE|> 标记、global search map-reduce（污染率治理）、社区 findings 字段
- #67 可运维性最小集
- 异常路径补测（kill Python 心跳超时）

### 大视频公平对比实测（2026-09-01 06:29，media 12 @ user_2，已建图重跑场景）
- 旧链路（8-31 同视频重跑）：~11 分钟（per-user 大锁包全程）
- 两跳链路：141 秒（prepare 0.18s 零 LLM + apply 140s），**4.7 倍**
- prepare：366 组全部对图 L1 命中，0 次 LLM；写集 328 把实体锁
- apply 大头是社区归属的 N+1 查询（366 个实体逐个 _has_community）→ 优化点：批量查询（待做）
- 首次构建场景（要付第二跳 LLM）预估 ~10 分钟内（media 10 旧方案 80+ 分钟，~8 倍）

**下一步**
- 等批量任务完结 → 检查 SUCCESS/FAILED 分布是否符合预期
- 异常路径测试：kill Python 进程验证心跳超时重投（可临时调小 HEARTBEAT_STALE_MS）
- 前端 /kg/status 验证（新状态映射 RUNNING + phase）

## 静音感知分片（2026-09-01 完成，替代 60s 固定分片）
- video_parser.py 重写：ffmpeg silencedetect(-35dB/0.4s) → 游标走查 [30s,90s] 窗口/60s 目标 → 过短合并/过长重切
- 用户定稿规则：静音切不补偿 overlap；硬切侧音频延伸 8s overlap；关键帧按核心区间 [start,end) 唯一归属（尾帧兜底+空帧取最近帧引用）
- segment_id 从 start_ms//60000 网格改顺序号 media_{id}_segment_{i}（pipeline 3 处）
- 实测：4 分钟视频 34 静音点 → 4 段全静音切（67.4/58.7/59.3/54.6s），零硬切零重叠
- analyze 全链路单测（experiment/silence_analyze_test.py，media 9999 已清理）：4 段 36 实体 28 关系，raw_extraction 全部落库
- 修复笔误：_save_media_and_segments 循环变量 i→idx（analyze 崩溃 NameError，单测抓到）
- 前端小修：lifecycleOf SUCCESS 时 currentIdx=5（全部 done 无 active，修掉就绪后 pulse 闪烁）
- 待做：前端上传新视频看完整效果（分片+两跳+生命周期展示）

## quickHash 统一判重 + 社区批量化（2026-09-01）
- 判重统一 quickHash（XXH3-128），MD5 废弃：引入 hash4j 0.30.0，新建 QuickHashUtils（流式），与前端 hash-wasm createXXHash128(0) 对拍一致（c9f5a06e...）；URL 导入/老 multipart/S3 complete 全部落 quick_hash 列，content_hash 停写（历史数据保留，contentHash() 读时优先 quick_hash 兜底旧值）
- 修复 segment_id 顺序号笔误（i→idx，analyze 直接崩 NameError，media 40 中招）
- 坑：KG_TWO_HOP=true 之前只设在 shell env，Python 重启丢失 → media 41 悄悄走旧 commit（17min 逐实体 LLM 判重）。已写入 ai-service/.env
- 坑：重启 Java 连不上 Redis 是 docker-proxy 僵死（docker restart redis 解决）；Java 本地启动必须 source .env（REDIS_PASSWORD）
- #70 旧 commit 链路社区进度回调接通（两跳链路本来就有），runner 已解析 x/y 到 Redis progress
- #71 社区归属批量化重写 community_updater.py：has_community/邻居投票/片段溯源全部 IN 批量查询；邻居投票跑 2 轮保留链式受益；LLM 判归属 10 实体/批（解析失败回退逐实体）；摘要归并按社区分组一次调用；singleton 批量 embed+UNWIND。预期 24min → 3-5min
- 待验证：media 41 跑完后用 experiment/community_batch_test.py 做幂等+性能基准

### media 42 全链路实测（2026-09-01 10:34-10:48，新代码首跑：静音分片+两跳+社区批量化，URL 导入）
- analyze 3.25min（17 段：1 静音切 + 16 硬切补偿，ASR 67s）
- 两跳 prepare 222s：119 实体/92 关系，LLM 仅 6 次调用（graph_l1=6, llm_merge_graph=4, llm_merge_video=2, new=107）
- 社区批量化 374.3s/119 实体（3.1s/实体，旧版 ~11s，3.5 倍）：assigned=28 created=81 skipped=10 neighbor_votes=0
- 端到端 13.6min vs media 41 旧链路 ~45min（3.3 倍）
- 观察：singleton 比例高（81/109），LLM 判归属偏保守（新图谱社区少+阈值），邻居投票 0 票（图被清空后邻居无社区）——图规模上来后再观察
- 注意：用户删了 logs/ 目录，本次日志从 .fuse_hidden 恢复；ai-service 日志路径需要重建

## 全量收尾 + #72 deepagents 大改造（2026-09-01 下午）
- #53 老上传协议下线：MediaController 老端点（init-upload/upload-chunk/complete-upload/upload）+ ChunkUploadService + MediaIngestService.ingestFile 删除；前端 chunkUpload.js 瘦身为纯工具函数；S3 断点续传提示改为不依赖 localStorage（quickHash 服务端找回）
- #69 收尾：POST /internal/kg/backfill-visuals 存量回填（缺时长/封面的 COMPLETED 视频），实测回填 1 条
- #68 剩余：gleaning 补抽（KG_GLEANING_ROUNDS=1，deepseek.extract_entities_gleaning + 去重合并）；<|COMPLETE|> 已有；global search map-reduce（KG_GLOBAL_MAP_REDUCE=true，5 社区/批 LLM 提取要点+打分≥50 入选，治污染率）；社区 findings 字段（模型+save_community+JSON 摘要 prompt）；Leiden 固定 seed=42；igraph/leidenalg 进 requirements
- #67 可运维性：trace_id（kg-{mediaId}-a/c{attempt}）进 Redis+stats+日志；阶段耗时 analyze_s/commit_s 落 kg_build_tasks.stats；Watcher 重试耗尽打 [ALERT]；KgLock/KgMultiLock 慢等告警（30s/15s）
- #72 deepagents 大改造：
  - 多 Agent 编排：orchestrator + detail-researcher（local+segment）+ theme-analyst（global map-reduce），task 工具并行派发
  - QuickJS：langchain-quickjs 0.3.5，CodeInterpreterMiddleware eval 工具（默认零权限，未开 PTC）
  - 上下文压缩：create_deep_agent 默认栈自带 SummarizationMiddleware（零改动获得）
  - 会话历史：V7 迁移 kg_chat_sessions/kg_chat_messages；Java KgChatService 持有事实源，每次问答加载最近 20 条透传 Python；流式 ask 从 SSE 尾部捕获 final 事件落库 assistant 回答
  - 前端：KgChatPanel.vue 组件（会话侧栏 + 多轮气泡 + 子 Agent 事件时间线），App.vue 的 KG 问答区整体替换
- 坑：SSE AsyncRequestTimeoutException——spring.mvc.async.request-timeout 被 .env AI_INTERACTIVE_TIMEOUT_MS=180000 覆盖，多 Agent 问答 3min+ 超时断流；改为 600000（.env + .env.example + properties 默认值三处）
- 实测：建会话→提问（global/local/segment 三路）→追问（"你刚才提到的狗哨理论"多轮正确）→复杂问题并行派发 2 子 Agent→6min 完成，final 落库，8 条消息齐全

## 2026-09-02：供应商切换 + 可靠性三件套 + 前端五件 + BENCH 评测体系

### 供应商切换（SiliconFlow → 阿里云百炼）
- 事故：SF 余额耗尽 402，bench 重导 7 视频 5 个烧 attempt 卡 FAILED（45 commit c2 封顶）
- config.py/runtime_config.py 支持分服务独立 key/base_url（留空回退 SF 统一 key，向后兼容）
- 选型（中档）：LLM=qwen-plus、embedding=qwen3.7-text-embedding（默认 1024 维与 bge-m3 一致）、ASR=qwen3-omni-flash、rerank=qwen3.7-text-rerank
- 坑①：该 workspace 端点无 /audio/transcriptions（404）→ aliyun_asr.py 加 chat 模式分支（base64 input_audio，按 ASR_URL 结尾自动选路），实测转写准确
- 坑②：reranker 走 compatible-api（非 compatible-mode）+ /reranks 路径 → reranker_path 可配
- 坑③：Redis config:models 运行时覆盖优先级高于 .env，里面还是旧 key → 已更新为百炼
- 坑④（严重）：KGAgent.__init__ 硬编码 siliconflow_* 未走 runtime_config → 问答全 402，已修
- classify_error 补鉴权类永久错误（402/401/403/payment/unauthorized 等不再烧重试预算）

### 可靠性三件套（#77/#78/#79）
- #79 预算/派发拆分（V8 迁移）：analyze_attempt 保留为 fencing（每派发递增），新增 analyze_retry 作预算（仅失败重投递增）；QUEUED/ANALYZED 超时补投走 queue 版只加 fencing 不烧预算（背压积压不再误伤），队列补投安全上限 10 次防刷；退避窗口改看 retry。回填 retry=GREATEST(attempt-1,0)
- #78 心跳/进度 Lua 条件写入：attempt 不匹配拒绝写并返回 -1 → Python 立即 task.cancel() 自我中止（僵尸中止从回调提前到心跳）；实测属主写真/僵尸写拒/状态零污染
- #77 手动重试：stateService.manualRetry（失败态重置 retry+错误+归位）+ POST /kg/retry（归属校验 403/非失败 400）+ 立即重新投递对应阶段消息；前端生命周期 FAILED 面板加「↻ 重试构建」按钮
- 实测：49 号模拟 ANALYZE_FAILED+retry=2（预算耗尽）→ /kg/retry → retry 清零、attempt=3（fencing 延续）、立即 ANALYZING 跑通 SUCCESS

### 前端五件（#73-#76 + 品牌）
- #75 品牌：DOVideo PRO → VideoGraph BETA；slogan GRAPH YOUR VIDEO / 视频进图谱·问答可溯源；index.html title/meta
- #73 布局：视频资料库移到问答区之上（上传→管理→问答动线）
- #76 问答面板暗色重写（原浅色 element-plus 色与暗色 app 冲突）+ markdown 列表缩进/边距/代码块/表格/引用块样式
- #74 内嵌引用锚定：ORCHESTRATOR_PROMPT 要求句末标〔媒体X mm:ss〕；三路检索 contexts 尾部追加【可标注来源】清单（媒体+时间，防编造锚点）；segment_search 上下文带媒体+mm:ss；前端 markdown.js 转 #video-cite=X:SS 链接（单点/范围两种 LLM 输出格式都兼容，范围取起始时点），点击委托按 media_id+时间范围匹配 citation 播放，匹配不到退化纯文本（校验机制），历史会话无 citations 时用锚点直接构造播放请求兜底
- 截图验证：品牌/布局/暗色气泡/锚点渲染/点击弹播放器从 0:00 播放媒体 47 ✓

### BENCH 评测体系（#80/#81/#82）
- bench 账号（id=4, group user_4）重导评测集 7 视频（media 43-49），走真实全链路（静音分片+两跳+百炼）
- eval_dataset_bench.json：旧 60s 网格段号按时间重叠率映射到静音分片新段号（84 处全部 ≥50% 重叠零复核）；harness 参数化 EVAL_GROUP_ID/EVAL_DATASET（坑：MYSQL_ROOT_PASSWORD 需 source .env，否则媒体映射空表全零分）
- 重大发现①：GraphRAG 原版英文 prompt 的 "capitalized"+英文 few-shot → qwen-plus 把中文实体翻成大写英文（向量/全文检索对中文查询失效）+ 照抄示例实体 MARTIN SMITH（描述自称"Not present in the text"）→ local 桶命中 50%。修复：语言保持规则+中文 few-shot+禁抄示例+贴原文约束，实体全中文（雅典/斯巴达/希波战争）
- 重大发现②：社区 singleton 率 99%（1069/1182），旧图同样 95%——增量三层链在稀疏图失效，非回归
- 评测对比（同图结构）：英文实体图 Hit@5 0.612/MRR 0.277/未中 19 题 → 中文实体图 0.878/0.402/未中 6 题；local 桶 50%→91.7%
- #82/#83：community_rebuilder.py（读全图→Leiden+摘要锁外算→per-group 社区锁内整体替换）+ POST /api/v1/community/rebuild（singleton 占比阈值跳过）+ Java KgCommunityRebuildTask 每天 03:17 全 group 触发

### Leiden 全量重建 A/B（2026-09-02 下午，#82/#83）
- 重建结果：1069 社区(99% singleton) → 424 社区(35% singleton)，最大 216；摘要质量抽查优（手机横评 216/LLM 技术 164/巴黎围城 125/巴黎公社 120/牛肉自由 120）
- 坑：重建漏建 Community-[:CONTAINS]->Segment 边（增量归属时建的溯源边）→ global_search 全部零引用，global 桶 0%。已修：rebuilder._replace_communities 补 CONTAINS（来源 community.source_segments），存量用 Cypher 直接补（BELONGS_TO×MENTIONED_IN 推导，1886 边零 LLM）
- A/B（同中文图谱）：Leiden 前 Hit@5 0.878/global 88.9% → Leiden 后 0.857/global 77.8%，但 MRR 0.402→0.420、global MRR 0.319→0.418（命中质量更好）；cross 桶 100%/MRR 1.0
- 解读：总分差 ±2 题属噪声级；Leiden 带来真正的跨视频主题社区（答案质量收益），检索指标基本持平。污染率 0.239→0.241 持平
- 剩余 7 道未命中：hist2×2（音乐 PPT 视频转写差，segment 路）、boston1×2（邻段错位）、llm-07/phone-07（global 宽泛概括题）、phone-05（邻段错位）
- #83 调度：Java KgCommunityRebuildTask @Scheduled 每天 03:17 → POST /community/rebuild（min_singleton_ratio=0.5），per-group 社区锁与增量更新互斥

## Tab 化布局 + 图谱可视化（2026-09-02 傍晚）
- 主页改三 Tab：上传 / 视频库 / 问答；有视频用户首次落地视频库页；上传完成自动跳视频库；通知条提到 Tab 栏上方跨 Tab 可见；问答区 v-show 保活（切 Tab 不丢会话状态）；单视频 Video Agent 侧边栏不变
- #84 图谱可视化：Python GET /query/graph（user_id+可选 media_id；全局按 source_count 截断 500 节点/1500 边，单视频全量子图）→ Java /kg/graph 代理 → GraphModal.vue（vis-network 力导图，滚轮缩放/拖拽平移/悬停看描述，按社区着色、按提及次数定大小）
- 入口：视频库标题栏「查看全局图谱」+ 每张卡片「图谱」按钮（图谱就绪才可点）
- 坑①：vis canvas 放进有 v-if 子节点的 Vue 容器 → 响应式更新把 canvas 拔掉（insertBefore null）→ vis 专用容器与 Vue 子节点分离
- 坑②：层次 Leiden 把实体挂到多层社区 → graph 查询实体重复（vis DataSet 唯一 id 报错）→ 查询侧 collect(c.id)[0] 去重
- 截图验证：三 Tab 切换、全局图谱（500 实体·694 关系力导簇）、单视频图谱（历史2：92 实体·82 关系，中文标签清晰）

## Tab 栏吸顶 + 社区面板（2026-09-02 晚）
- Tab 栏移入 navbar 第二行随其吸顶；navbar padding 收紧（0.9rem）；三个 Tab 内容区 max-height calc(100vh-118px) 各自 overflow-y 滚动；通知条改 fixed 悬浮（top 128px）滚动可见
- #85 全局图谱社区划分面板：Python GET /query/communities（level 0 按实体数降序）→ Java /kg/communities 代理 → GraphModal 左侧栏（社区名+实体数+摘要三行截断），点击社区 selectNodes+focus 聚焦成员
- 实测：196 个顶层社区，最大 216 实体；点社区 1（手机评测 216 实体）图中成员高亮聚焦
- 坑：mvnw spring-boot:run 复用旧 target/classes 不重编译（改代码后需先 mvnw compile）

## 视频级总结 + 视频语义检索基础（2026-09-03，#84）
- analyze 收尾同步生成整视频总结 → Neo4j `Media.summary` + `Media.summary_embedding`（与 title_embedding 同空间 1024 维）+ 新向量索引 media_summary_embedding_index
- 超长分层压缩：≤2 万字单次 LLM；超过 L1 按 6000 字分块出要点（并发）→ L2 聚合 150-400 字
- 护栏实测：media 47（纯音乐 PPT 视频，ASR 几乎零转写）LLM 脑补出"声音艺术冥想体验"式虚假总结 → 加有效文本量护栏（剥（音乐）（鼓声）等标记后 <300 字符不生成）+ prompt 禁脑补条款
- 幂等：Media.summary 存在即跳过生成；analyze 重跑不重复烧 LLM
- 导入时间：Neo4j 原只有 created_at（analyze 建图点，media_47 实测比真实上传晚 1.5h）→ KgAnalyzeConsumer 读 MySQL upload_time 捎进 analyze-async（upload_time_ms），Media.uploaded_at coalesce 首写为准；缓存命中分支在 analyze 尾部兜底回填
- 实测：media 44 全链路 SUCCESS，uploaded_at=2026-09-02 06:02 与 MySQL 一致；回填 43/46/48/49；语义冒烟三查询全部 Top1 命中正确视频且分差明显（欧洲历史→46:0.875 / 手机续航→49:0.871 / AI Agent→48:0.841）
- 范围外（留下一轮）：前端展示、问答/检索接入（视频级 media_search 工具、DRIFT 视频总览）

## 检索漏斗 L0 + 抽取增强 + 视频总结展示（2026-09-07）
- #85 L0 视频级检索：retriever.media_search（Media.summary_embedding 精确余弦，group 预过滤）；9 个 neo4j 检索方法加 media_ids 列表过滤（Cypher IN 列表，与单 media_id 兼容）；local/segment 透传；kg_agent 挂第 4 个工具 + ORCHESTRATOR 路由引导（不确定答案在哪先 media_search 定范围再下钻）。实测：media_search 46 命中 0.869；范围过滤后 local 引用 ⊆ 候选
- A-P2 global 下钻：命中社区附核心实体行（get_community_top_entities），agent 可据此追问细节；CONTAINS 片段证据链已有
- B1 抽取 prompt 第 5 条：【画面文字】OCR 术语为拼写基准，纠正语音同音误写。初版措辞失效（模型当两个产品），改示例驱动（"Cloud Code→Claude Code"）后实测纠正成功
- B3 gleaning 改验证式（"还有遗漏吗"+不凑数）+ 主抽取 prompt 第 6 条排代词（还原具体名称否则不抽）
- B4 OCR 垃圾串过滤（video_parser._is_useful_ocr）：全大写无空格非白名单串（OEIAFMOOC/QWERTYUIOP）丢弃、单字符丢弃、常见缩写白名单（AI/GPU/MCP 等）保留，13/13 用例过
- D 前端视频总结展示：Python GET /query/media-summaries → Java /kg/media-summaries 代理 → 卡片文件名下两行简介（hover 全文）；音乐视频按护栏无简介正确

## 异常路径终验 + 评测/展示收尾（2026-09-07 晚）
- **kill Python 心跳超时恢复全链路实测**（#66 最后一条未测异常路径）：media 44 真抽取中段（10/16）kill -9 → Watcher HEARTBEAT_TIMEOUT 判死（拨老心跳加速验证）→ ANALYZE_FAILED → 预算重投 a2（retry=1）→ 撞上死 Python 派发失败 → Watcher 队列补投 a3（fencing+1 不烧预算，#79 如实工作）→ Python 重启后 MQ 重投成功 → 抽取断点续跑（0-9 段缓存）→ COMMITTING → SUCCESS。全程 ~9min
- L2 答案层评测脚本适配 bench（EVAL_USER_ID/EVAL_DATASET 参数化 + 缓存按用户隔离 answers_cache_{uid}.json）
- README 全量重写（旧 DoVideoAI Planner/Critic 描述全部替换为 VideoGraph 现状：三 Tab、两跳消解、检索漏斗、DeepAgents、评测体系；截图 3 张：视频库卡片+简介/全局图谱+社区面板/问答锚点）

### L2 答案层 bench 首测（2026-09-07 21:05，报告 20260907-210534）
- 拒答率(oos) 0.625（8 道超纲题 5 道正确拒答，3 道没拒住）；平均工具调用 2.3 次/题，平均 26s/题
- 要点覆盖率 0.641（旧管线 0.825 ↓）；RAGAS: faithfulness 0.675 / answer_relevancy 0.926 / context_recall 0.779（旧 0.817/0.933/0.867）
- 注意：与旧管线不可直接比（图谱/分片/embedding/抽取/Agent 模型全换），但 faithfulness 与覆盖率下滑是真实信号，归因方向：global 检索短板（77.8%）传导 + agent 模型 qwen-plus vs DeepSeek-V3.2 差异 + 答案引证纪律
- 待办：oos 未拒住的 3 道逐题看；faithfulness 低的答案抽查无据断言；后续可把 agent 模型换强（qwen3-max）做 A/B
- oos 拒答率 0.625 归因：2/3 是检测器词表漏（"知识图谱中没有"≠词表"知识库中没有"、"无任何片段提及"≠"未提及"），真实拒答≈7.5/8；1/3 真泄漏（oos-06 拒答后补"注：现实中约4小时18分钟"世界知识）→ 待办：补检测词表 + orchestrator prompt 强化"拒答后禁止补充通用知识"
