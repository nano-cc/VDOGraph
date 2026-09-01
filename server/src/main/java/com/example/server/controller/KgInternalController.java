package com.example.server.controller;

import com.example.server.dto.KgCommitMsg;
import com.example.server.entity.KgBuildTask;
import com.example.server.service.KgTaskStateService;
import org.apache.rocketmq.spring.core.RocketMQTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

import static com.example.server.service.KgTaskStateService.*;

/**
 * Python → Java 内部回调（#66 方案 C：Python 不直连 MQ，状态推进由 Java 原子完成）
 * 鉴权：X-Internal-Key（kg.internal.api-key，空则放行，与 Python 端 auth 同策略，仅内网调用）
 * 幂等：所有状态转换走条件更新，重复回调返回 409 让 Python 中止当前执行体
 */
@RestController
@RequestMapping("/internal/kg")
public class KgInternalController {

    private static final Logger log = LoggerFactory.getLogger(KgInternalController.class);

    private final KgTaskStateService stateService;
    private final RocketMQTemplate rocketMQTemplate;
    private final String kgCommitTopic;
    private final String internalKey;

    public KgInternalController(KgTaskStateService stateService,
                                RocketMQTemplate rocketMQTemplate,
                                @Value("${rocketmq.topic.kg-commit:kg-commit}") String kgCommitTopic,
                                @Value("${kg.internal.api-key:}") String internalKey) {
        this.stateService = stateService;
        this.rocketMQTemplate = rocketMQTemplate;
        this.kgCommitTopic = kgCommitTopic;
        this.internalKey = internalKey;
    }

    private void checkKey(String key) {
        if (internalKey == null || internalKey.isBlank()) return;  // 本地开发放行
        if (!internalKey.equals(key)) {
            throw new SecurityException("invalid internal key");
        }
    }

    /**
     * 通用状态推进：Python 进入某阶段时调用（ANALYZING / COMMITTING）
     * 409 = 状态已被别人改过（如已 CANCELLED），Python 应中止执行
     */
    @PostMapping("/state")
    public ResponseEntity<Map<String, Object>> transition(
            @RequestHeader(value = "X-Internal-Key", required = false) String key,
            @RequestBody Map<String, Object> body) {
        checkKey(key);
        Long mediaId = asLong(body.get("media_id"));
        String to = String.valueOf(body.get("to"));
        String message = body.get("message") == null ? null : String.valueOf(body.get("message"));

        List<String> from = switch (to) {
            case ANALYZING -> List.of(QUEUED, ANALYZE_FAILED);
            case COMMITTING -> List.of(ANALYZED, COMMIT_FAILED);
            default -> throw new IllegalArgumentException("unsupported transition target: " + to);
        };
        boolean ok = stateService.transition(mediaId, to, from, message);
        if (!ok) {
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("accepted", false, "reason", "state precondition not met"));
        }
        return ResponseEntity.ok(Map.of("accepted", true));
    }

    /**
     * analyze 完成：原子完成「MySQL → ANALYZED + 发 kg-commit」
     * 发 MQ 失败时状态保持 ANALYZED，由 Watcher 的 ANALYZED 超时兜底补发
     */
    @PostMapping("/analyzed")
    public ResponseEntity<Map<String, Object>> analyzed(
            @RequestHeader(value = "X-Internal-Key", required = false) String key,
            @RequestBody Map<String, Object> body) {
        checkKey(key);
        Long mediaId = asLong(body.get("media_id"));
        String stats = body.get("stats") == null ? null : String.valueOf(body.get("stats"));

        boolean ok = stateService.transition(mediaId, ANALYZED, List.of(ANALYZING), "解析完成，等待图谱写入");
        if (!ok) {
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("accepted", false, "reason", "state precondition not met"));
        }
        stateService.saveStats(mediaId, stats);  // analyze 产物统计归档（非关键路径）

        // 先发 attempt 计数（持久化），再发消息：保证 MySQL/消息/Redis 心跳三方 attempt 一致
        if (!stateService.markCommitDispatched(mediaId)) {
            log.warn("kg_analyzed_commit_count_failed mediaId={}（状态被并发改动，等 Watcher 兜底）", mediaId);
            return ResponseEntity.ok(Map.of("accepted", true));
        }
        KgBuildTask task = stateService.getTask(mediaId);
        try {
            rocketMQTemplate.convertAndSend(kgCommitTopic,
                    new KgCommitMsg(mediaId, task.getCommitAttempt(), task.getUserId()));
            log.info("kg_analyzed_commit_sent mediaId={} attempt={}", mediaId, task.getCommitAttempt());
        } catch (Exception e) {
            // 状态已 ANALYZED，Watcher 5min 超时兜底补发 kg-commit
            log.error("kg_analyzed_commit_send_failed mediaId={}: {}（等 Watcher 兜底）", mediaId, e.getMessage());
        }
        return ResponseEntity.ok(Map.of("accepted", true));
    }

    /** commit 完成：→ SUCCESS */
    @PostMapping("/finished")
    public ResponseEntity<Map<String, Object>> finished(
            @RequestHeader(value = "X-Internal-Key", required = false) String key,
            @RequestBody Map<String, Object> body) {
        checkKey(key);
        Long mediaId = asLong(body.get("media_id"));
        String stats = body.get("stats") == null ? null : String.valueOf(body.get("stats"));

        boolean ok = stateService.markSuccess(mediaId, stats);
        if (!ok) {
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("accepted", false, "reason", "state precondition not met"));
        }
        log.info("kg_finished mediaId={}", mediaId);
        return ResponseEntity.ok(Map.of("accepted", true));
    }

    /** 阶段失败：→ ANALYZE_FAILED / COMMIT_FAILED（retryable=false 永久错误，Watcher 不重投） */
    @PostMapping("/failed")
    public ResponseEntity<Map<String, Object>> failed(
            @RequestHeader(value = "X-Internal-Key", required = false) String key,
            @RequestBody Map<String, Object> body) {
        checkKey(key);
        Long mediaId = asLong(body.get("media_id"));
        String errorPhase = String.valueOf(body.get("error_phase"));
        String errorCode = body.get("error_code") == null ? null : String.valueOf(body.get("error_code"));
        String message = body.get("message") == null ? null : String.valueOf(body.get("message"));
        boolean retryable = !Boolean.FALSE.equals(body.get("retryable"));

        String to;
        List<String> from;
        if ("analyze".equals(errorPhase)) {
            to = ANALYZE_FAILED;
            from = List.of(QUEUED, ANALYZING);
        } else {
            to = COMMIT_FAILED;
            from = List.of(ANALYZED, COMMITTING);
        }
        boolean ok = stateService.markFailed(mediaId, to, errorPhase, errorCode, message, retryable, from);
        if (!ok) {
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("accepted", false, "reason", "state precondition not met"));
        }
        log.warn("kg_failed mediaId={} phase={} code={} retryable={} msg={}",
                mediaId, errorPhase, errorCode, retryable, message);
        return ResponseEntity.ok(Map.of("accepted", true));
    }

    private static Long asLong(Object o) {
        if (o instanceof Number n) return n.longValue();
        return Long.parseLong(String.valueOf(o));
    }
}
