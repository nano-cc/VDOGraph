package com.example.server.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.example.server.entity.KgBuildTask;
import com.example.server.mapper.KgBuildTaskMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * KG 任务状态中枢（#66）
 * - MySQL kg_build_tasks：唯一事实源（状态转换全部条件更新，禁止裸写）
 * - Redis kg:task:{mediaId}：热缓存（心跳/进度/前端轮询，Python 直写 updated_at/progress/message）
 * 单一写方原则：MySQL status 只有本服务能写；Redis status 快照由本服务在转换后刷新
 */
@Service
public class KgTaskStateService {

    private static final Logger log = LoggerFactory.getLogger(KgTaskStateService.class);

    public static final String TASK_KEY_PREFIX = "kg:task:";
    private static final Duration TASK_TTL = Duration.ofDays(7);

    // ---- 状态常量 ----
    public static final String QUEUED = "QUEUED";
    public static final String ANALYZING = "ANALYZING";
    public static final String ANALYZED = "ANALYZED";
    public static final String COMMITTING = "COMMITTING";
    public static final String SUCCESS = "SUCCESS";
    public static final String ANALYZE_FAILED = "ANALYZE_FAILED";
    public static final String COMMIT_FAILED = "COMMIT_FAILED";
    public static final String CANCELLED = "CANCELLED";

    /** 进行中状态（有/无执行体混合） */
    public static final List<String> ACTIVE_STATES = List.of(QUEUED, ANALYZING, ANALYZED, COMMITTING);
    /** 失败但可能重试的状态 */
    public static final List<String> FAILED_STATES = List.of(ANALYZE_FAILED, COMMIT_FAILED);

    private final KgBuildTaskMapper taskMapper;
    private final StringRedisTemplate redisTemplate;

    public KgTaskStateService(KgBuildTaskMapper taskMapper, StringRedisTemplate redisTemplate) {
        this.taskMapper = taskMapper;
        this.redisTemplate = redisTemplate;
    }

    // ==================== 建档 ====================

    /**
     * 建档（主键唯一约束防重）：已存在返回 false
     */
    public boolean createTask(Long mediaId, Long userId, String videoUrl) {
        KgBuildTask task = new KgBuildTask();
        task.setMediaId(mediaId);
        task.setUserId(userId);
        task.setVideoUrl(videoUrl);
        task.setGroupId("user_" + userId);
        task.setStatus(QUEUED);
        task.setAttempt(1);
        task.setAnalyzeAttempt(0);
        task.setCommitAttempt(0);
        task.setRetryable(true);
        try {
            taskMapper.insert(task);
            writeRedisSnapshot(mediaId, QUEUED, "排队中");
            return true;
        } catch (DuplicateKeyException e) {
            return false;
        }
    }

    // ==================== 状态转换（全部条件更新） ====================

    /**
     * 条件转换：仅当当前状态在 fromStates 内才更新。返回是否转换成功。
     */
    public boolean transition(Long mediaId, String to, Collection<String> fromStates, String message) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .in("status", fromStates)
                .set("status", to)
                .set("updated_at", LocalDateTime.now());
        if (message != null) {
            // 进行中消息不写 error 字段，Redis 快照里体现
        }
        int rows = taskMapper.update(null, uw);
        if (rows > 0) {
            writeRedisSnapshot(mediaId, to, message);
            return true;
        }
        log.info("kg_state_transition_rejected mediaId={} to={} from={}", mediaId, to, fromStates);
        return false;
    }

    /** 标记阶段失败（条件更新 + 错误信息） */
    public boolean markFailed(Long mediaId, String failedStatus, String errorPhase,
                              String errorCode, String errorMessage, boolean retryable,
                              Collection<String> fromStates) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .in("status", fromStates)
                .set("status", failedStatus)
                .set("error_phase", errorPhase)
                .set("error_code", errorCode)
                .set("retryable", retryable)
                .set("updated_at", LocalDateTime.now());
        if (errorMessage != null) {
            uw.set("error_message", errorMessage.length() > 1000 ? errorMessage.substring(0, 1000) : errorMessage);
        }
        int rows = taskMapper.update(null, uw);
        if (rows > 0) {
            writeRedisSnapshot(mediaId, failedStatus, errorMessage);
            return true;
        }
        return false;
    }

    /** 标记成功 */
    public boolean markSuccess(Long mediaId, String stats) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .eq("status", COMMITTING)
                .set("status", SUCCESS)
                .set("stats", stats)
                .set("completed_at", LocalDateTime.now())
                .set("updated_at", LocalDateTime.now());
        int rows = taskMapper.update(null, uw);
        if (rows > 0) {
            writeRedisSnapshot(mediaId, SUCCESS, "知识图谱已就绪");
            // 清掉残留进度（如 32/32），就绪状态不该带进度条
            redisTemplate.opsForHash().delete(TASK_KEY_PREFIX + mediaId, "progress");
            if (stats != null) {
                redisTemplate.opsForHash().put(TASK_KEY_PREFIX + mediaId, "stats", stats);
            }
            return true;
        }
        return false;
    }

    /** 取消（视频删除时调用）：进行中/失败态 → CANCELLED，已 SUCCESS 的不动 */
    public void cancel(Long mediaId) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .notIn("status", SUCCESS, CANCELLED)
                .set("status", CANCELLED)
                .set("updated_at", LocalDateTime.now());
        taskMapper.update(null, uw);
        redisTemplate.delete(TASK_KEY_PREFIX + mediaId);
    }

    // ==================== attempt 计数（条件自增，防多实例并发重投超发） ====================

    /** analyze 重投：QUEUED/ANALYZE_FAILED 时 analyze_attempt+1 并把状态归位 QUEUED */
    public boolean prepareAnalyzeRedispatch(Long mediaId, int maxAttempts) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .in("status", QUEUED, ANALYZE_FAILED)
                .apply("analyze_attempt < {0}", maxAttempts)
                .set("status", QUEUED)
                .set("retryable", true)
                .setSql("analyze_attempt = analyze_attempt + 1")
                .set("updated_at", LocalDateTime.now());
        return taskMapper.update(null, uw) > 0;
    }

    /** commit 重投：ANALYZED/COMMIT_FAILED 时 commit_attempt+1 并把状态归位 ANALYZED */
    public boolean prepareCommitRedispatch(Long mediaId, int maxAttempts) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .in("status", ANALYZED, COMMIT_FAILED)
                .apply("commit_attempt < {0}", maxAttempts)
                .set("status", ANALYZED)
                .set("retryable", true)
                .setSql("commit_attempt = commit_attempt + 1")
                .set("updated_at", LocalDateTime.now());
        return taskMapper.update(null, uw) > 0;
    }

    /** 首次派发 analyze 计数（triggerBuild/consumer 前） */
    public boolean markAnalyzeDispatched(Long mediaId) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .eq("status", QUEUED)
                .setSql("analyze_attempt = analyze_attempt + 1")
                .set("updated_at", LocalDateTime.now());
        return taskMapper.update(null, uw) > 0;
    }

    /** 派发 commit 计数（analyzed 回调发 kg-commit 前）：必须持久化，
     *  否则 MySQL commit_attempt 与消息/心跳 attempt 不一致，归属校验永远 mismatch */
    public boolean markCommitDispatched(Long mediaId) {
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId)
                .in("status", ANALYZED, COMMIT_FAILED)
                .setSql("commit_attempt = commit_attempt + 1")
                .set("updated_at", LocalDateTime.now());
        return taskMapper.update(null, uw) > 0;
    }

    // ==================== 查询 ====================

    /** 归档阶段产物统计（非状态转换，幂等覆盖写） */
    public void saveStats(Long mediaId, String stats) {
        if (stats == null) return;
        UpdateWrapper<KgBuildTask> uw = new UpdateWrapper<>();
        uw.eq("media_id", mediaId).set("stats", stats).set("updated_at", LocalDateTime.now());
        taskMapper.update(null, uw);
        redisTemplate.opsForHash().put(TASK_KEY_PREFIX + mediaId, "stats", stats);
    }

    public KgBuildTask getTask(Long mediaId) {
        return taskMapper.selectById(mediaId);
    }

    /** Watcher 名单：所有未完结任务（进行中 + 失败态） */
    public List<KgBuildTask> listUnfinished() {
        QueryWrapper<KgBuildTask> qw = new QueryWrapper<>();
        qw.in("status", QUEUED, ANALYZING, ANALYZED, COMMITTING, ANALYZE_FAILED, COMMIT_FAILED);
        return taskMapper.selectList(qw);
    }

    /** Redis 心跳/进度（Watcher 死活判断、consumer 预检用） */
    public Map<Object, Object> readRedisState(Long mediaId) {
        return redisTemplate.opsForHash().entries(TASK_KEY_PREFIX + mediaId);
    }

    /** Redis 快照（状态转换后刷新，前端立即见新状态；attempt 字段保留 Python 写入的值） */
    public void writeRedisSnapshot(Long mediaId, String status, String message) {
        Map<String, String> fields = new HashMap<>();
        fields.put("status", status);
        if (message != null) {
            fields.put("message", message);
        }
        fields.put("updated_at", String.valueOf(System.currentTimeMillis() / 1000));
        redisTemplate.opsForHash().putAll(TASK_KEY_PREFIX + mediaId, fields);
        redisTemplate.expire(TASK_KEY_PREFIX + mediaId, TASK_TTL);
    }

    // ==================== 前端状态查询（Redis 优先，MySQL 兜底回填，旧 key 再兜底） ====================

    private static final String LEGACY_STATUS_KEY_PREFIX = "kg:build:";

    public Map<String, String> getStatusForApi(Long mediaId) {
        Map<String, String> result = new HashMap<>();
        result.put("mediaId", String.valueOf(mediaId));

        Map<Object, Object> hash = readRedisState(mediaId);
        if (!hash.isEmpty()) {
            String status = str(hash.get("status"));
            // 硬化：Redis 显示进行中但心跳早已停滞（>30min）→ 不信缓存，和 MySQL 事实源对账
            if (ACTIVE_STATES.contains(status) && !redisHeartbeatFresh(hash)) {
                KgBuildTask task = getTask(mediaId);
                if (task != null && !task.getStatus().equals(status)) {
                    log.info("kg_status_reconcile mediaId={} redis={} mysql={}（缓存停滞，以 MySQL 为准）",
                            mediaId, status, task.getStatus());
                    writeRedisSnapshot(mediaId, task.getStatus(),
                            task.getErrorMessage() != null ? task.getErrorMessage() : "");
                    hash = readRedisState(mediaId);
                }
            }
            String statusNow = str(hash.get("status"));
            result.put("status", toApiStatus(statusNow));
            result.put("phase", statusNow);
            result.put("message", str(hash.get("message")));
            result.put("progress", str(hash.get("progress")));
            result.put("stats", str(hash.get("stats")));
            return result;
        }

        // Redis miss → MySQL 兜底 + 回填
        KgBuildTask task = getTask(mediaId);
        if (task != null) {
            writeRedisSnapshot(mediaId, task.getStatus(),
                    task.getErrorMessage() != null ? task.getErrorMessage() : "");
            hash = readRedisState(mediaId);
        } else {
            // 旧链路状态（一期前的历史视频）
            String legacy = redisTemplate.opsForValue().get(LEGACY_STATUS_KEY_PREFIX + mediaId + ":status");
            if (legacy != null) {
                result.put("status", legacy);
                result.put("message", redisTemplate.opsForValue().get(LEGACY_STATUS_KEY_PREFIX + mediaId + ":message"));
                result.put("stats", redisTemplate.opsForValue().get(LEGACY_STATUS_KEY_PREFIX + mediaId + ":stats"));
            }
            return result;
        }

        String status = str(hash.get("status"));
        result.put("status", toApiStatus(status));
        result.put("phase", status);
        result.put("message", str(hash.get("message")));
        result.put("progress", str(hash.get("progress")));
        result.put("stats", str(hash.get("stats")));
        return result;
    }

    /** Redis 心跳新鲜度（30min 内更新过） */
    private static boolean redisHeartbeatFresh(Map<Object, Object> hash) {
        try {
            long ts = Long.parseLong(String.valueOf(hash.get("updated_at"))) * 1000;
            return System.currentTimeMillis() - ts < 30 * 60 * 1000;
        } catch (Exception e) {
            return false;
        }
    }

    /** 进行中态统一映射 RUNNING；两阶段 FAILED 对前端统一 FAILED（兼容现有前端判断） */
    private static String toApiStatus(String status) {
        if (status == null) return null;
        return switch (status) {
            case QUEUED, ANALYZING, ANALYZED, COMMITTING -> "RUNNING";
            case ANALYZE_FAILED, COMMIT_FAILED -> "FAILED";
            default -> status;
        };
    }

    private static String str(Object o) {
        return o == null ? null : o.toString();
    }
}
