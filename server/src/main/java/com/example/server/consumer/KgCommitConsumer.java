package com.example.server.consumer;

import com.example.server.client.AiServiceClient;
import com.example.server.dto.KgCommitMsg;
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
 * KG commit 阶段消费者（#66 拆分）
 * 职责极薄：预检 → 调 Python commit-async（202 投递即返回）→ ACK
 * - 只接受 ANALYZED/COMMIT_FAILED 状态：别的状态说明顺序乱了或已办结
 * - ANALYZING/QUEUED 收到 commit 消息（乱序）→ 抛异常延迟重投，等 analyze 完成
 * - 删除竞态：media_files 不存在 → 任务取消
 */
@Component
@RocketMQMessageListener(
        topic = "${rocketmq.topic.kg-commit:kg-commit}",
        consumerGroup = "kg-commit-consumer",
        maxReconsumeTimes = 2)
public class KgCommitConsumer implements RocketMQListener<KgCommitMsg> {

    private static final Logger log = LoggerFactory.getLogger(KgCommitConsumer.class);
    private static final long FRESH_HEARTBEAT_MS = 120_000;

    private final AiServiceClient aiServiceClient;
    private final KgTaskStateService stateService;
    private final MediaFileMapper mediaFileMapper;

    public KgCommitConsumer(AiServiceClient aiServiceClient, KgTaskStateService stateService,
                            MediaFileMapper mediaFileMapper) {
        this.aiServiceClient = aiServiceClient;
        this.stateService = stateService;
        this.mediaFileMapper = mediaFileMapper;
    }

    @Override
    public void onMessage(KgCommitMsg msg) {
        if (msg == null || msg.getMediaId() == null) {
            log.error("kg_commit_poison_message payload={}", msg == null ? "null" : "mediaId=" + msg.getMediaId());
            return;
        }
        Long mediaId = msg.getMediaId();

        MediaFile media = mediaFileMapper.selectById(mediaId);
        if (media == null) {
            log.info("kg_commit_skipped_deleted mediaId={}", mediaId);
            stateService.cancel(mediaId);
            return;
        }

        KgBuildTask task = stateService.getTask(mediaId);
        if (task == null) {
            log.warn("kg_commit_no_task mediaId={}（无任务记录，丢弃）", mediaId);
            return;
        }
        String status = task.getStatus();
        if (SUCCESS.equals(status) || CANCELLED.equals(status)) {
            log.info("kg_commit_skipped_state mediaId={} status={}", mediaId, status);
            return;
        }
        if (COMMITTING.equals(status) && heartbeatFresh(mediaId, task.getCommitAttempt())) {
            log.info("kg_commit_skipped_running mediaId={}", mediaId);
            return;
        }
        // 乱序：analyze 还没完成就收到 commit → 延迟重投等 analyze 推进
        if (QUEUED.equals(status) || ANALYZING.equals(status) || ANALYZE_FAILED.equals(status)) {
            log.info("kg_commit_out_of_order mediaId={} status={}，延迟重投", mediaId, status);
            throw new IllegalStateException("commit 消息早于 analyze 完成到达，延迟重投");
        }
        // 正常受理窗口：ANALYZED / COMMIT_FAILED / COMMITTING(心跳死)

        try {
            aiServiceClient.commitAsync(mediaId, msg.getAttempt(), msg.getUserId());
            log.info("kg_commit_dispatched mediaId={} attempt={}", mediaId, msg.getAttempt());
        } catch (HttpClientErrorException.TooManyRequests e) {
            log.info("kg_commit_busy_retry mediaId={}", mediaId);
            throw new IllegalStateException("Python commit 并发已满，等待重投", e);
        } catch (Exception e) {
            log.warn("kg_commit_dispatch_failed mediaId={}: {}", mediaId, e.getMessage());
            throw new IllegalStateException("KG commit 投递失败，交由 RocketMQ 重试", e);
        }
    }

    private boolean heartbeatFresh(Long mediaId, Integer currentAttempt) {
        Map<Object, Object> redis = stateService.readRedisState(mediaId);
        if (redis.isEmpty()) return false;
        if (currentAttempt != null) {
            long redisAttempt = parseLong(redis.get("attempt"), -1);
            if (redisAttempt != -1 && redisAttempt != currentAttempt) {
                return false;
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
