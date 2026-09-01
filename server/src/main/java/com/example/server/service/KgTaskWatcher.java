package com.example.server.service;

import com.example.server.dto.KgAnalyzeMsg;
import com.example.server.dto.KgCommitMsg;
import com.example.server.entity.KgBuildTask;
import com.example.server.entity.MediaFile;
import com.example.server.mapper.MediaFileMapper;
import org.apache.rocketmq.spring.core.RocketMQTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.List;
import java.util.Map;

import static com.example.server.service.KgTaskStateService.*;

/**
 * KG 任务看门狗（#66 重写：MySQL 出名单，Redis 出心跳）
 * MQ 重试是主力，watcher 补 MQ 覆盖不到的盲区。四卡点：
 * - QUEUED 超时（投递成功但没人消费）：看 MySQL updated_at 年龄
 * - ANALYZED 超时（analyze 完成但 commit 没接上）：看 MySQL updated_at 年龄
 * - ANALYZING/COMMITTING 心跳超时（Python 执行体死亡）：看 Redis updated_at 心跳 + attempt 归属校验
 * - 失败态（retryable 且 attempt 未超限）：按指数退避重投
 * 重投前三关：media_files 存活、attempt 上限、条件更新占位（多实例并发安全，SETNX 重投锁）
 */
@Component
public class KgTaskWatcher {

    private static final Logger log = LoggerFactory.getLogger(KgTaskWatcher.class);

    private static final int MAX_ATTEMPTS = 2;
    /** 无执行体状态（QUEUED/ANALYZED）停留超时：正常秒级推进，5min 没动说明衔接断了 */
    private static final long NO_EXECUTOR_STALE_MS = 5 * 60 * 1000;
    /** 有执行体状态（ANALYZING/COMMITTING）心跳停滞超时：心跳 30s 一次，30min 无更新判定死亡 */
    private static final long HEARTBEAT_STALE_MS = 30 * 60 * 1000;
    private static final Duration RETRY_LOCK_TTL = Duration.ofSeconds(60);
    /** Redis 集体失心跳识别：一轮巡检中无心跳的进行中任务达到该数，判定 Redis 异常，跳过本轮 */
    private static final int MASS_HEARTBEAT_LOSS_THRESHOLD = 3;

    private final StringRedisTemplate redisTemplate;
    private final RocketMQTemplate rocketMQTemplate;
    private final KgTaskStateService stateService;
    private final MediaFileMapper mediaFileMapper;
    private final org.redisson.api.RedissonClient redissonClient;
    private final String kgAnalyzeTopic;
    private final String kgCommitTopic;

    public KgTaskWatcher(StringRedisTemplate redisTemplate,
                         RocketMQTemplate rocketMQTemplate,
                         KgTaskStateService stateService,
                         MediaFileMapper mediaFileMapper,
                         org.redisson.api.RedissonClient redissonClient,
                         @Value("${rocketmq.topic.kg-analyze:kg-analyze}") String kgAnalyzeTopic,
                         @Value("${rocketmq.topic.kg-commit:kg-commit}") String kgCommitTopic) {
        this.redisTemplate = redisTemplate;
        this.rocketMQTemplate = rocketMQTemplate;
        this.stateService = stateService;
        this.mediaFileMapper = mediaFileMapper;
        this.redissonClient = redissonClient;
        this.kgAnalyzeTopic = kgAnalyzeTopic;
        this.kgCommitTopic = kgCommitTopic;
    }

    @Scheduled(fixedDelay = 15_000)
    public void watch() {
        List<KgBuildTask> tasks;
        try {
            tasks = stateService.listUnfinished();
        } catch (Exception e) {
            log.warn("KgTaskWatcher roster query failed: {}", e.getMessage());
            return;
        }
        if (tasks.isEmpty()) return;

        // Redis 集体失心跳保护：先统计有执行体任务里有多少拿不到心跳
        int runningCount = 0, missingHeartbeat = 0;
        for (KgBuildTask t : tasks) {
            if (ANALYZING.equals(t.getStatus()) || COMMITTING.equals(t.getStatus())) {
                runningCount++;
                if (stateService.readRedisState(t.getMediaId()).isEmpty()) {
                    missingHeartbeat++;
                }
            }
        }
        if (runningCount >= MASS_HEARTBEAT_LOSS_THRESHOLD && missingHeartbeat == runningCount) {
            log.error("KG watcher: {} 个进行中任务集体无心跳，疑似 Redis 异常，跳过本轮巡检", runningCount);
            return;
        }

        long now = System.currentTimeMillis();
        for (KgBuildTask task : tasks) {
            try {
                check(task, now);
            } catch (Exception e) {
                log.warn("KgTaskWatcher error on mediaId={}: {}", task.getMediaId(), e.getMessage());
            }
        }
    }

    private void check(KgBuildTask task, long now) {
        Long mediaId = task.getMediaId();
        String status = task.getStatus();
        long mysqlAgeMs = now - task.getUpdatedAt().atZone(ZoneId.systemDefault()).toInstant().toEpochMilli();

        switch (status) {
            case QUEUED -> {
                // 投递成功但没人消费（MQ 丢消息/Consumer 全挂）
                if (mysqlAgeMs > NO_EXECUTOR_STALE_MS) {
                    redispatchAnalyze(mediaId, task, "queued-stale");
                }
            }
            case ANALYZED -> {
                // analyze 完成但 commit 没接上（回调写库后发 MQ 前 Java 崩溃）
                if (mysqlAgeMs > NO_EXECUTOR_STALE_MS) {
                    redispatchCommit(mediaId, task, "analyzed-stale");
                }
            }
            case ANALYZING -> {
                if (heartbeatDead(mediaId, task.getAnalyzeAttempt(), now)) {
                    boolean marked = stateService.markFailed(mediaId, ANALYZE_FAILED, "analyze",
                            "HEARTBEAT_TIMEOUT", "执行体心跳停滞，判定死亡", true, List.of(ANALYZING));
                    if (marked) redispatchAnalyze(mediaId, task, "analyze-heartbeat-dead");
                }
            }
            case COMMITTING -> {
                if (heartbeatDead(mediaId, task.getCommitAttempt(), now)) {
                    boolean marked = stateService.markFailed(mediaId, COMMIT_FAILED, "commit",
                            "HEARTBEAT_TIMEOUT", "执行体心跳停滞，判定死亡", true, List.of(COMMITTING));
                    if (marked) redispatchCommit(mediaId, task, "commit-heartbeat-dead");
                }
            }
            case ANALYZE_FAILED -> {
                if (Boolean.TRUE.equals(task.getRetryable()) && backoffElapsed(task, now)) {
                    redispatchAnalyze(mediaId, task, "analyze-failed-retry");
                }
            }
            case COMMIT_FAILED -> {
                if (Boolean.TRUE.equals(task.getRetryable()) && backoffElapsed(task, now)) {
                    redispatchCommit(mediaId, task, "commit-failed-retry");
                }
            }
            default -> { /* SUCCESS/CANCELLED 不在名单内 */ }
        }
    }

    /** 心跳死亡判定：key 不存在或停滞超阈值；attempt 不匹配 = 旧执行体僵尸心跳，同样判死 */
    private boolean heartbeatDead(Long mediaId, Integer currentAttempt, long now) {
        Map<Object, Object> redis = stateService.readRedisState(mediaId);
        if (redis.isEmpty()) return true;
        if (currentAttempt != null && currentAttempt > 0) {
            long redisAttempt = parseLong(redis.get("attempt"), -1);
            if (redisAttempt != -1 && redisAttempt != currentAttempt) return true;
        }
        long updatedAt = parseLong(redis.get("updated_at"), 0) * 1000;
        return updatedAt <= 0 || now - updatedAt > HEARTBEAT_STALE_MS;
    }

    /** 指数退避：15s × 2^(attempt-1)，避免固定 15s 打爆未恢复的下游 */
    private boolean backoffElapsed(KgBuildTask task, long now) {
        int attempt = ANALYZE_FAILED.equals(task.getStatus())
                ? task.getAnalyzeAttempt() : task.getCommitAttempt();
        long backoffMs = 15_000L * (1L << Math.max(0, Math.min(attempt, 6)));
        long updatedMs = task.getUpdatedAt().atZone(ZoneId.systemDefault()).toInstant().toEpochMilli();
        return now - updatedMs > backoffMs;
    }

    private void redispatchAnalyze(Long mediaId, KgBuildTask task, String reason) {
        if (!precheck(mediaId, reason)) return;
        // 条件自增占位：并发 watcher/重复巡检只有一个能成功
        if (!stateService.prepareAnalyzeRedispatch(mediaId, MAX_ATTEMPTS)) {
            if (ANALYZE_FAILED.equals(task.getStatus())) {
                log.error("KG analyze mediaId={} 已达重试上限，保持 FAILED 等人工", mediaId);
            }
            return;
        }
        KgBuildTask latest = stateService.getTask(mediaId);
        try {
            rocketMQTemplate.convertAndSend(kgAnalyzeTopic,
                    new KgAnalyzeMsg(mediaId, task.getVideoUrl(), latest.getAnalyzeAttempt(), task.getUserId()));
            log.warn("KG analyze mediaId={} redispatch (reason={}, attempt={})", mediaId, reason, latest.getAnalyzeAttempt());
        } catch (Exception e) {
            log.error("Redispatch analyze failed for mediaId={}: {}", mediaId, e.getMessage());
        }
    }

    private void redispatchCommit(Long mediaId, KgBuildTask task, String reason) {
        if (!precheck(mediaId, reason)) return;
        if (!stateService.prepareCommitRedispatch(mediaId, MAX_ATTEMPTS)) {
            if (COMMIT_FAILED.equals(task.getStatus())) {
                log.error("KG commit mediaId={} 已达重试上限，保持 FAILED 等人工", mediaId);
            }
            return;
        }
        KgBuildTask latest = stateService.getTask(mediaId);
        try {
            rocketMQTemplate.convertAndSend(kgCommitTopic,
                    new KgCommitMsg(mediaId, latest.getCommitAttempt(), task.getUserId()));
            log.warn("KG commit mediaId={} redispatch (reason={}, attempt={})", mediaId, reason, latest.getCommitAttempt());
        } catch (Exception e) {
            log.error("Redispatch commit failed for mediaId={}: {}", mediaId, e.getMessage());
        }
    }

    /** 重投前三关：删除竞态 + 重投锁（Redisson 显式 lease 节流，多实例并发安全；#64） */
    private boolean precheck(Long mediaId, String reason) {
        MediaFile media = mediaFileMapper.selectById(mediaId);
        if (media == null) {
            log.info("KG task mediaId={} 视频已删除，标 CANCELLED 不重投（{}）", mediaId, reason);
            stateService.cancel(mediaId);
            return false;
        }
        // 节流锁不主动释放，靠 60s lease 过期：多实例下同一任务的重复巡检只有一个能重投
        try {
            return redissonClient.getLock("kg:retry:lock:" + mediaId)
                    .tryLock(0, RETRY_LOCK_TTL.toSeconds(), java.util.concurrent.TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private static long parseLong(Object o, long def) {
        try {
            return o == null ? def : Long.parseLong(o.toString());
        } catch (Exception e) {
            return def;
        }
    }
}
