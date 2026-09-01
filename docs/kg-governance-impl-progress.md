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
