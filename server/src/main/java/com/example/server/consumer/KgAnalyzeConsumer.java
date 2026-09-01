package com.example.server.consumer;

import com.example.server.client.AiServiceClient;
import com.example.server.dto.KgAnalyzeMsg;
import com.example.server.entity.KgBuildTask;
import com.example.server.entity.MediaFile;
import com.example.server.mapper.MediaFileMapper;
import com.example.server.service.KgTaskStateService;
import org.apache.rocketmq.spring.annotation.RocketMQMessageListener;
import org.apache.rocketmq.spring.core.RocketMQListener;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpClientErrorException;

import java.util.Map;

import static com.example.server.service.KgTaskStateService.*;

/**
 * KG analyze 阶段消费者（#66 拆分）
 * 职责极薄：预检 → 调 Python analyze-async（202 投递即返回）→ ACK
 * - 预检幂等：SUCCESS/ANALYZED/COMMITTING/COMMIT_FAILED 说明 analyze 已完成，跳过；
 *   ANALYZING + 心跳新鲜 + attempt 归属当前 → 有人在跑，跳过
 * - 删除竞态：media_files 不存在 → 任务已取消，跳过
 * - Python 忙（429）/不可达 → 抛异常让 broker 延迟重投；耗尽进 DLQ 后由 Watcher 兜底
 */
@Component
@RocketMQMessageListener(
        topic = "${rocketmq.topic.kg-analyze:kg-analyze}",
        consumerGroup = "kg-analyze-consumer",
        maxReconsumeTimes = 2)
public class KgAnalyzeConsumer implements RocketMQListener<KgAnalyzeMsg> {

    private static final Logger log = LoggerFactory.getLogger(KgAnalyzeConsumer.class);
    /** 心跳新鲜度阈值（与 Python FRESH_HEARTBEAT_S 对齐） */
    private static final long FRESH_HEARTBEAT_MS = 120_000;

    private final AiServiceClient aiServiceClient;
    private final KgTaskStateService stateService;
    private final MediaFileMapper mediaFileMapper;

    public KgAnalyzeConsumer(AiServiceClient aiServiceClient, KgTaskStateService stateService,
                             MediaFileMapper mediaFileMapper) {
        this.aiServiceClient = aiServiceClient;
        this.stateService = stateService;
        this.mediaFileMapper = mediaFileMapper;
    }

    @Override
    public void onMessage(KgAnalyzeMsg msg) {
        if (msg == null || msg.getMediaId() == null || msg.getVideoUrl() == null || msg.getVideoUrl().isBlank()) {
            log.error("kg_analyze_poison_message payload={}", msg == null ? "null" : "mediaId=" + msg.getMediaId());
            return;
        }
        Long mediaId = msg.getMediaId();

        // 删除竞态：视频已删 → 任务取消，不重投不执行
        MediaFile media = mediaFileMapper.selectById(mediaId);
        if (media == null) {
            log.info("kg_analyze_skipped_deleted mediaId={}", mediaId);
            stateService.cancel(mediaId);
            return;
        }

        // 状态预检（MySQL 事实源）
        KgBuildTask task = stateService.getTask(mediaId);
        if (task != null) {
            String status = task.getStatus();
            if (SUCCESS.equals(status) || ANALYZED.equals(status)
                    || COMMITTING.equals(status) || COMMIT_FAILED.equals(status) || CANCELLED.equals(status)) {
                log.info("kg_analyze_skipped_state mediaId={} status={}", mediaId, status);
                return;
            }
            if (ANALYZING.equals(status) && heartbeatFresh(mediaId, task.getAnalyzeAttempt())) {
                log.info("kg_analyze_skipped_running mediaId={}", mediaId);
                return;
            }
        }

        try {
            aiServiceClient.analyzeAsync(mediaId, msg.getVideoUrl(), msg.getAttempt(), msg.getUserId());
            log.info("kg_analyze_dispatched mediaId={} attempt={}", mediaId, msg.getAttempt());
        } catch (HttpClientErrorException.TooManyRequests e) {
            log.info("kg_analyze_busy_retry mediaId={}", mediaId);
            throw new IllegalStateException("Python analyze 并发已满，等待重投", e);
        } catch (Exception e) {
            log.warn("kg_analyze_dispatch_failed mediaId={}: {}", mediaId, e.getMessage());
            throw new IllegalStateException("KG analyze 投递失败，交由 RocketMQ 重试", e);
        }
    }

    /** 心跳新鲜且归属当前 attempt（防旧 attempt 僵尸心跳误报存活） */
    private boolean heartbeatFresh(Long mediaId, Integer currentAttempt) {
        Map<Object, Object> redis = stateService.readRedisState(mediaId);
        if (redis.isEmpty()) return false;
        if (currentAttempt != null) {
            long redisAttempt = parseLong(redis.get("attempt"), -1);
            if (redisAttempt != -1 && redisAttempt != currentAttempt) {
                return false;  // 心跳是旧 attempt 的僵尸写的
            }
        }
        long updatedAt = parseLong(redis.get("updated_at"), 0) * 1000;
        return updatedAt > 0 && System.currentTimeMillis() - updatedAt < FRESH_HEARTBEAT_MS;
    }

    private static long parseLong(Object o, long def) {
        try {
            return o == null ? def : Long.parseLong(o.toString());
        } catch (Exception e) {
            return def;
        }
    }
}
