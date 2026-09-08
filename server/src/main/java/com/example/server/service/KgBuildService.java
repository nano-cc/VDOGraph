package com.example.server.service;

import com.example.server.client.AiServiceClient;
import com.example.server.dto.KgAnalyzeMsg;
import com.example.server.dto.KgCommitMsg;
import com.example.server.entity.KgBuildTask;
import org.apache.rocketmq.spring.core.RocketMQTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

/**
 * 知识图谱构建任务服务（#66 两阶段拆分版）
 * 上传完成后：MySQL 建档（QUEUED，主键唯一防重）→ 发 kg-analyze 消息即返回。
 * analyze 完成后由 Python 回调 /internal/kg/analyzed，Java 原子推进 ANALYZED + 发 kg-commit。
 * 图写锁由 Python 自持（kg:graph:write:{group}），Java 不占线程池线程。
 * 投递/执行失败由 KgTaskWatcher 兜底（MySQL 名单 + Redis 心跳）。
 */
@Service
public class KgBuildService {

    private static final Logger log = LoggerFactory.getLogger(KgBuildService.class);
    private static final int MAX_ATTEMPTS = 2;

    private final AiServiceClient aiServiceClient;
    private final ThreadPoolTaskExecutor executor;
    private final RocketMQTemplate rocketMQTemplate;
    private final KgTaskStateService stateService;
    private final String kgAnalyzeTopic;
    private final String kgCommitTopic;

    @Value("${kg.auto-build:true}")
    private boolean autoBuild;

    public KgBuildService(AiServiceClient aiServiceClient,
                          @Qualifier("aiTaskExecutor") ThreadPoolTaskExecutor executor,
                          RocketMQTemplate rocketMQTemplate,
                          KgTaskStateService stateService,
                          @Value("${rocketmq.topic.kg-analyze:kg-analyze}") String kgAnalyzeTopic,
                          @Value("${rocketmq.topic.kg-commit:kg-commit}") String kgCommitTopic) {
        this.aiServiceClient = aiServiceClient;
        this.executor = executor;
        this.rocketMQTemplate = rocketMQTemplate;
        this.stateService = stateService;
        this.kgAnalyzeTopic = kgAnalyzeTopic;
        this.kgCommitTopic = kgCommitTopic;
    }

    /**
     * 触发知识图谱构建：先建档后发消息（记录先行，消息随后，失败由 Watcher 对账兜底）
     * 幂等：MySQL 主键唯一约束防重；已有任务按状态决定跳过或复投
     */
    public void triggerBuild(Long mediaId, String videoUrl, Long userId) {
        if (!autoBuild) {
            log.info("KG auto-build disabled, skip mediaId={}", mediaId);
            return;
        }

        boolean created = stateService.createTask(mediaId, userId, videoUrl);
        if (!created) {
            // 已建档：进行中/已成功 → 跳过；失败可重试 → 重新投递
            KgBuildTask existing = stateService.getTask(mediaId);
            if (existing == null) return;
            String status = existing.getStatus();
            if (KgTaskStateService.ACTIVE_STATES.contains(status)
                    || KgTaskStateService.SUCCESS.equals(status)
                    || KgTaskStateService.CANCELLED.equals(status)) {
                log.info("KG build already exists mediaId={} status={}, skip", mediaId, status);
                return;
            }
            // 失败态复投（#79 预算判断看 analyze_retry，不看派发计数）
            if (Boolean.FALSE.equals(existing.getRetryable())
                    || (existing.getAnalyzeRetry() != null && existing.getAnalyzeRetry() >= MAX_ATTEMPTS)) {
                log.info("KG build failed permanently mediaId={} status={}, skip", mediaId, status);
                return;
            }
            if (!stateService.prepareAnalyzeRedispatch(mediaId, MAX_ATTEMPTS)) {
                return;  // 状态被别人改了，放弃本次触发
            }
            log.info("KG build re-dispatch after failure mediaId={} status={}", mediaId, status);
        }

        // 发 kg-analyze（投递失败不上抛：上传已成功，标 FAILED 等 Watcher 兜底）
        KgBuildTask task = stateService.getTask(mediaId);
        int attempt = task.getAnalyzeAttempt() + 1;
        try {
            rocketMQTemplate.convertAndSend(kgAnalyzeTopic,
                    new KgAnalyzeMsg(mediaId, videoUrl, attempt, userId));
            stateService.markAnalyzeDispatched(mediaId);
            log.info("KG analyze message sent for mediaId={} attempt={}", mediaId, attempt);
        } catch (Exception e) {
            stateService.markFailed(mediaId, KgTaskStateService.ANALYZE_FAILED, "dispatch",
                    "MQ_SEND_FAILED", "构建任务投递失败，等待自动重投", true,
                    List.of(KgTaskStateService.QUEUED));
            log.error("Failed to send KG analyze message for mediaId={}: {}", mediaId, e.getMessage());
        }
    }

    /**
     * 删除视频的图谱数据（两阶段；图写锁在 Python 自持，Java 不再加锁）
     * 阶段一同步快速执行（用户无感）；阶段二异步排队清理；任务状态标 CANCELLED（进行中任务回调会被 409 中止）
     */
    public void deleteGraph(Long mediaId, Long userId) {
        stateService.cancel(mediaId);
        aiServiceClient.deleteMediaGraph(mediaId, 1, userId);

        // 阶段二：异步深度清理
        executor.execute(() -> aiServiceClient.deleteMediaGraph(mediaId, 2, userId));
    }

    /** 查询构建状态（Redis 优先，MySQL 兜底回填，旧 kg:build:* 再兜底） */
    public Map<String, String> getStatus(Long mediaId) {
        return stateService.getStatusForApi(mediaId);
    }

    /**
     * #77 手动重试失败任务：校验归属 → 重置预算/错误 → 立即重新投递对应阶段消息
     * （不等 Watcher 巡检，用户点了就要马上动）
     */
    public com.example.server.common.Result<String> manualRetry(Long mediaId, Long userId) {
        KgBuildTask task = stateService.getTask(mediaId);
        if (task == null) {
            return com.example.server.common.Result.error(400, "任务不存在");
        }
        if (!task.getUserId().equals(userId)) {
            return com.example.server.common.Result.error(403, "无权操作他人任务");
        }
        String failedStatus = task.getStatus();
        if (!KgTaskStateService.ANALYZE_FAILED.equals(failedStatus)
                && !KgTaskStateService.COMMIT_FAILED.equals(failedStatus)) {
            return com.example.server.common.Result.error(400, "仅失败状态的任务可手动重试（当前：" + failedStatus + "）");
        }
        if (!stateService.manualRetry(mediaId)) {
            return com.example.server.common.Result.error(409, "状态已变化，请刷新后重试");
        }

        // 立即重新投递：analyze 失败发 kg-analyze；commit 失败发 kg-commit
        try {
            if (KgTaskStateService.ANALYZE_FAILED.equals(failedStatus)) {
                KgBuildTask latest = stateService.getTask(mediaId);
                int attempt = latest.getAnalyzeAttempt() + 1;
                rocketMQTemplate.convertAndSend(kgAnalyzeTopic,
                        new KgAnalyzeMsg(mediaId, latest.getVideoUrl(), attempt, userId));
                stateService.markAnalyzeDispatched(mediaId);
                log.info("KG manual retry analyze mediaId={} attempt={}", mediaId, attempt);
            } else {
                KgBuildTask latest = stateService.getTask(mediaId);
                int attempt = latest.getCommitAttempt() + 1;
                rocketMQTemplate.convertAndSend(kgCommitTopic,
                        new KgCommitMsg(mediaId, attempt, userId));
                stateService.markCommitDispatched(mediaId);
                log.info("KG manual retry commit mediaId={} attempt={}", mediaId, attempt);
            }
            return com.example.server.common.Result.ok("已重新投递");
        } catch (Exception e) {
            log.error("KG manual retry dispatch failed mediaId={}: {}", mediaId, e.getMessage());
            return com.example.server.common.Result.error(500, "重新投递失败，系统将自动重试");
        }
    }
}
