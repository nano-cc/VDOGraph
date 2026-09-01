package com.example.server.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.example.server.entity.MediaFile;
import com.example.server.mapper.MediaFileMapper;
import com.example.server.utils.MinioUtils;
import io.minio.GetPresignedObjectUrlArgs;
import io.minio.MinioClient;
import io.minio.http.Method;
import io.minio.messages.ListPartsResult;
import io.minio.messages.Part;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * S3 multipart 直传上传服务
 * 职责：秒传判定、预签名签发、完成合并、ffprobe 校验
 * 任务状态：Redis 存 quickHash → 上传任务映射（TTL 3 天）
 * 半成品清理：MinIO multipart 生命周期 + 定时清扫（S3UploadCleanupTask）
 */
@Service
public class S3UploadService {

    private static final Logger log = LoggerFactory.getLogger(S3UploadService.class);

    private static final String TASK_KEY_PREFIX = "upload:s3:task:";
    private static final String QH_KEY_PREFIX = "upload:s3:qh:";
    private static final String DONE_KEY_PREFIX = "upload:s3:done:";
    private static final String INIT_LOCK_PREFIX = "upload:s3:initlock:";
    private static final String ACTIVE_KEY_PREFIX = "upload:s3:active:";
    private static final Duration TASK_TTL = Duration.ofDays(3);
    private static final Duration DONE_TTL = Duration.ofDays(7);
    private static final int MAX_ACTIVE_TASKS_PER_USER = 5;
    private static final long MAX_UPLOAD_BYTES = 2048L * 1024 * 1024;  // 2GB
    private static final long CHUNK_SIZE = 5L * 1024 * 1024;
    private static final int PRESIGN_EXPIRY_SECONDS = 30 * 60;

    private final MinioClient minioClient;
    private final MultipartMinioClient multipartClient;
    private final MinioUtils minioUtils;
    private final MediaFileMapper mediaFileMapper;
    private final StringRedisTemplate redisTemplate;
    private final org.redisson.api.RedissonClient redissonClient;
    private final com.fasterxml.jackson.databind.ObjectMapper objectMapper;
    private final String bucketName;

    public S3UploadService(MinioClient minioClient,
                           MultipartMinioClient multipartClient,
                           MinioUtils minioUtils,
                           MediaFileMapper mediaFileMapper,
                           StringRedisTemplate redisTemplate,
                           org.redisson.api.RedissonClient redissonClient,
                           com.fasterxml.jackson.databind.ObjectMapper objectMapper,
                           @Value("${minio.bucketName}") String bucketName) {
        this.minioClient = minioClient;
        this.multipartClient = multipartClient;
        this.minioUtils = minioUtils;
        this.mediaFileMapper = mediaFileMapper;
        this.redisTemplate = redisTemplate;
        this.redissonClient = redissonClient;
        this.objectMapper = objectMapper;
        this.bucketName = bucketName;
    }

    // ==================== init ====================

    /**
     * 初始化上传：大小校验 → 秒传判定 → 续传找回 → 互斥 → 新建 multipart
     * 返回 {status: INSTANT|RESUME|NEW, ...}
     */
    public Map<String, Object> initUpload(String filename, long size, int totalChunks,
                                          String quickHash, Long userId) throws Exception {
        // 0. 大小/分片数校验（H1）
        if (size <= 0) {
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.INVALID_ARGUMENT, "文件大小为 0，可能已损坏");
        }
        if (size > MAX_UPLOAD_BYTES) {
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.INVALID_ARGUMENT,
                    "文件超过 2GB 上限（当前 " + (size / 1024 / 1024) + "MB）");
        }
        long expectedChunks = (size + CHUNK_SIZE - 1) / CHUNK_SIZE;
        if (totalChunks != (int) expectedChunks) {
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.INVALID_ARGUMENT,
                    "分片数与文件大小不符（声明 " + totalChunks + "，应为 " + expectedChunks + "）");
        }

        // 1. 秒传判定
        if (quickHash != null && !quickHash.isBlank()) {
            MediaFile existing = mediaFileMapper.selectOne(new QueryWrapper<MediaFile>()
                    .eq("user_id", userId)
                    .eq("quick_hash", quickHash)
                    .last("LIMIT 1"));
            if (existing != null && "COMPLETED".equals(existing.getStatus())) {
                log.info("upload_instant_hit userId={} quickHash={} mediaId={}", userId, quickHash, existing.getId());
                return Map.of("status", "INSTANT",
                        "mediaId", existing.getId(),
                        "filename", existing.getFilename());
            }

            // 2. 续传找回
            String taskJson = redisTemplate.opsForValue().get(QH_KEY_PREFIX + userId + ":" + quickHash);
            if (taskJson != null) {
                @SuppressWarnings("unchecked")
                Map<String, Object> task = objectMapper.readValue(taskJson, Map.class);
                String uploadId = (String) task.get("uploadId");
                if ((int) task.get("totalChunks") == totalChunks) {
                    List<Map<String, Object>> uploaded = listUploadedParts(uploadId);
                    log.info("upload_resume userId={} uploadId={} uploaded={}/{}", userId, uploadId, uploaded.size(), totalChunks);
                    Map<String, Object> result = new HashMap<>();
                    result.put("status", "RESUME");
                    result.put("uploadId", uploadId);
                    result.put("uploadedParts", uploaded);
                    return result;
                }
            }
        }

        // 3. 互斥（G1：同用户同文件并发上传归并到同一任务；Redisson RLock，#64）
        String lockKey = INIT_LOCK_PREFIX + userId + ":" + (quickHash == null ? "nqh" : quickHash);
        org.redisson.api.RLock initLock = redissonClient.getLock(lockKey);
        boolean lockAcquired;
        try {
            // tryLock 自带 2s 等待：等先到的 init 写完任务映射
            lockAcquired = initLock.tryLock(2, java.util.concurrent.TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.CONFLICT, "上传初始化被中断，请重试");
        }
        if (!lockAcquired) {
            // 锁被占：先到的 init 已写完任务映射，按续传归并
            if (quickHash != null && !quickHash.isBlank()) {
                String taskJson = redisTemplate.opsForValue().get(QH_KEY_PREFIX + userId + ":" + quickHash);
                if (taskJson != null) {
                    @SuppressWarnings("unchecked")
                    Map<String, Object> task = objectMapper.readValue(taskJson, Map.class);
                    if ((int) task.get("totalChunks") == totalChunks) {
                        List<Map<String, Object>> uploaded = listUploadedParts((String) task.get("uploadId"));
                        Map<String, Object> result = new HashMap<>();
                        result.put("status", "RESUME");
                        result.put("uploadId", task.get("uploadId"));
                        result.put("uploadedParts", uploaded);
                        log.info("upload_init_merged userId={} uploadId={}（并发 init 归并）", userId, task.get("uploadId"));
                        return result;
                    }
                }
            }
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.CONFLICT, "相同文件正在上传中，请稍后重试");
        }

        try {
            // 4. 活跃任务数上限（H2：防 init 被刷）
            if (quickHash != null && !quickHash.isBlank() && activeTaskCount(userId) >= MAX_ACTIVE_TASKS_PER_USER) {
                throw new com.example.server.exception.BusinessException(
                        com.example.server.common.ErrorCode.CONFLICT,
                        "同时进行的上传任务过多（上限 " + MAX_ACTIVE_TASKS_PER_USER + "），请先完成或取消部分任务");
            }

            // 5. 新建 multipart
            String objectName = "uploads/" + UUID.randomUUID() + fileSuffix(filename);
            String uploadId = multipartClient.createUpload(bucketName, objectName);

            Map<String, Object> task = new HashMap<>();
            task.put("uploadId", uploadId);
            task.put("objectName", objectName);
            task.put("filename", filename);
            task.put("size", size);
            task.put("totalChunks", totalChunks);
            task.put("userId", userId);
            task.put("quickHash", quickHash == null ? "" : quickHash);
            saveTask(uploadId, quickHash, userId, task);
            if (quickHash != null && !quickHash.isBlank()) {
                addActiveTask(userId, uploadId);
            }

            log.info("upload_init userId={} uploadId={} totalChunks={}", userId, uploadId, totalChunks);
            return Map.of("status", "NEW", "uploadId", uploadId);
        } finally {
            if (initLock.isHeldByCurrentThread()) {
                initLock.unlock();
            }
        }
    }

    // ==================== 预签名 ====================

    /**
     * 为分片生成预签名 PUT URL（数据直达 MinIO，不过服务器）
     */
    public Map<Integer, String> presignParts(String uploadId, List<Integer> partNumbers, Long userId) throws Exception {
        Map<String, Object> task = requireTask(uploadId);
        if (!task.get("userId").toString().equals(String.valueOf(userId))) {
            throw new SecurityException("upload task belongs to another user");
        }
        String objectName = (String) task.get("objectName");
        Map<Integer, String> urls = new HashMap<>();
        for (int partNumber : partNumbers) {
            urls.put(partNumber, minioClient.getPresignedObjectUrl(GetPresignedObjectUrlArgs.builder()
                    .method(Method.PUT)
                    .bucket(bucketName)
                    .object(objectName)
                    .expiry(PRESIGN_EXPIRY_SECONDS)
                    .extraQueryParams(Map.of("partNumber", String.valueOf(partNumber), "uploadId", uploadId))
                    .build()));
        }
        return urls;
    }

    // ==================== 完成 ====================

    private static final String COMPLETE_LOCK_PREFIX = "upload:s3:complete:";
    /** complete 锁 lease：覆盖 ListParts + MinIO 合并 + ffprobe（30s 超时），留足余量 */
    private static final long COMPLETE_LOCK_LEASE_SECONDS = 600;

    /**
     * 完成上传（幂等 + Redisson 锁串行化，#65）：
     * done 标记检查 → MySQL quickHash 检查 → 锁内复检 → ListParts 校验 → 组装 → ffprobe → 写库
     */
    public Map<String, Object> completeUpload(String uploadId, String quickHash, Long userId, Prober prober) throws Exception {
        // 幂等检查 ①：Redis done 标记（G2 快路径）
        String doneMediaId = redisTemplate.opsForValue().get(DONE_KEY_PREFIX + uploadId);
        if (doneMediaId != null) {
            log.info("upload_complete_idempotent uploadId={} mediaId={}（done 标记命中）", uploadId, doneMediaId);
            return Map.of("mediaId", Long.parseLong(doneMediaId), "idempotent", true);
        }

        // 幂等检查 ②：MySQL quickHash（G2 权威路径，Redis 丢了也兜得住）
        if (quickHash != null && !quickHash.isBlank()) {
            MediaFile existing = mediaFileMapper.selectOne(new QueryWrapper<MediaFile>()
                    .eq("user_id", userId).eq("quick_hash", quickHash).last("LIMIT 1"));
            if (existing != null && "COMPLETED".equals(existing.getStatus())) {
                log.info("upload_complete_idempotent uploadId={} mediaId={}（MySQL quickHash 命中）",
                        uploadId, existing.getId());
                return Map.of("mediaId", existing.getId(), "idempotent", true);
            }
        }

        // 分布式锁串行化（#65）：同一 uploadId 的 complete 只有一个在跑，消除并发双写窗口
        org.redisson.api.RLock completeLock = redissonClient.getLock(COMPLETE_LOCK_PREFIX + uploadId);
        boolean locked;
        try {
            locked = completeLock.tryLock(10, COMPLETE_LOCK_LEASE_SECONDS, java.util.concurrent.TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.CONFLICT, "完成上传被中断，请重试");
        }
        if (!locked) {
            // 等锁超时：对方可能已完成，轮询 done 标记拿到幂等结果
            for (int i = 0; i < 5; i++) {
                Thread.sleep(1000);
                doneMediaId = redisTemplate.opsForValue().get(DONE_KEY_PREFIX + uploadId);
                if (doneMediaId != null) {
                    return Map.of("mediaId", Long.parseLong(doneMediaId), "idempotent", true);
                }
            }
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.CONFLICT, "该上传正在完成中，请勿重复提交");
        }

        try {
            // 锁内复检（等锁期间别人可能已完成）
            doneMediaId = redisTemplate.opsForValue().get(DONE_KEY_PREFIX + uploadId);
            if (doneMediaId != null) {
                return Map.of("mediaId", Long.parseLong(doneMediaId), "idempotent", true);
            }
            if (quickHash != null && !quickHash.isBlank()) {
                MediaFile existing = mediaFileMapper.selectOne(new QueryWrapper<MediaFile>()
                        .eq("user_id", userId).eq("quick_hash", quickHash).last("LIMIT 1"));
                if (existing != null && "COMPLETED".equals(existing.getStatus())) {
                    return Map.of("mediaId", existing.getId(), "idempotent", true);
                }
            }

            return doCompleteUpload(uploadId, quickHash, userId, prober);
        } finally {
            if (completeLock.isHeldByCurrentThread()) {
                completeLock.unlock();
            }
        }
    }

    /** complete 实际执行体（调用方须已持 COMPLETE_LOCK） */
    private Map<String, Object> doCompleteUpload(String uploadId, String quickHash, Long userId, Prober prober) throws Exception {
        Map<String, Object> task = requireTask(uploadId);
        if (!task.get("userId").toString().equals(String.valueOf(userId))) {
            throw new SecurityException("upload task belongs to another user");
        }
        String objectName = (String) task.get("objectName");
        int totalChunks = (int) task.get("totalChunks");

        // ListParts 是权威状态（前端无需传 ETag）
        ListPartsResult listResult = multipartClient.listParts(bucketName, objectName, uploadId);
        List<Part> parts = listResult.partList();
        if (parts.size() != totalChunks) {
            throw new IllegalStateException("分片未全部上传完成（已传 " + parts.size() + "/" + totalChunks + "）");
        }

        // 实际字节数校验（H6）
        long declaredSize = ((Number) task.get("size")).longValue();
        long actualSize = parts.stream().mapToLong(Part::partSize).sum();
        if (actualSize != declaredSize) {
            throw new com.example.server.exception.BusinessException(
                    com.example.server.common.ErrorCode.INVALID_ARGUMENT,
                    "实际上传数据量与声明不符（声明 " + declaredSize + "，实际 " + actualSize + "）");
        }

        // 服务端组装（零数据搬运）
        multipartClient.completeUpload(bucketName, objectName, uploadId, parts.toArray(new Part[0]));

        // ffprobe 内容校验（伪装/损坏文件在这被挡）
        String fileUrl = minioUtils.objectUrl(objectName);
        if (prober != null) {
            String probeError = prober.probe(fileUrl);
            if (probeError != null) {
                minioUtils.removeObject(objectName);
                clearTask(uploadId, task);
                log.warn("upload_probe_failed userId={} object={} error={}", userId, objectName, probeError);
                throw new com.example.server.exception.BusinessException(
                        com.example.server.common.ErrorCode.INVALID_ARGUMENT, "视频内容校验失败: " + probeError);
            }
        }

        // 写库
        MediaFile mediaFile = new MediaFile();
        mediaFile.setFilename((String) task.get("filename"));
        mediaFile.setFilePath(fileUrl);
        mediaFile.setStatus("COMPLETED");
        mediaFile.setUserId(userId);
        String qh = (String) task.get("quickHash");
        mediaFile.setQuickHash(qh.isBlank() ? null : qh);
        // content_hash（旧 MD5 列）不再写入：判重统一走 quick_hash（XXH3-128）
        mediaFile.setUploadTime(java.time.LocalDateTime.now());
        mediaFileMapper.insert(mediaFile);

        // done 标记（幂等依据，先于任务清理写入）
        redisTemplate.opsForValue().set(DONE_KEY_PREFIX + uploadId, String.valueOf(mediaFile.getId()), DONE_TTL);
        clearTask(uploadId, task);
        log.info("upload_completed userId={} mediaId={} object={}", userId, mediaFile.getId(), objectName);

        Map<String, Object> result = new HashMap<>();
        result.put("mediaId", mediaFile.getId());
        result.put("fileUrl", fileUrl);
        result.put("mediaFile", mediaFile);
        return result;
    }

    // ==================== 取消 ====================

    public void abortUpload(String uploadId, Long userId) throws Exception {
        Map<String, Object> task = requireTask(uploadId);
        if (!task.get("userId").toString().equals(String.valueOf(userId))) {
            throw new SecurityException("upload task belongs to another user");
        }
        multipartClient.abortUpload(bucketName, (String) task.get("objectName"), uploadId);
        clearTask(uploadId, task);
        log.info("upload_aborted userId={} uploadId={}", userId, uploadId);
    }

    // ==================== 续传状态 ====================

    /**
     * 按 quickHash 找回任务并返回已传分片（换设备/丢 localStorage 也能续传）
     */
    public Map<String, Object> uploadStatus(String quickHash, Long userId) throws Exception {
        String taskJson = redisTemplate.opsForValue().get(QH_KEY_PREFIX + userId + ":" + quickHash);
        if (taskJson == null) return null;
        @SuppressWarnings("unchecked")
        Map<String, Object> task = objectMapper.readValue(taskJson, Map.class);
        String uploadId = (String) task.get("uploadId");
        List<Map<String, Object>> uploaded = listUploadedParts(uploadId);
        Map<String, Object> result = new HashMap<>();
        result.put("status", "RESUME");
        result.put("uploadId", uploadId);
        result.put("uploadedParts", uploaded);
        return result;
    }

    // ==================== 内部 ====================

    @FunctionalInterface
    public interface Prober {
        /** 返回 null 表示通过，否则返回错误信息 */
        String probe(String fileUrl);
    }

    private List<Map<String, Object>> listUploadedParts(String uploadId) throws Exception {
        Map<String, Object> task = requireTask(uploadId);
        ListPartsResult listResult = multipartClient.listParts(bucketName, (String) task.get("objectName"), uploadId);
        List<Map<String, Object>> uploaded = new ArrayList<>();
        for (Part part : listResult.partList()) {
            uploaded.add(Map.of("partNumber", part.partNumber(), "size", part.partSize()));
        }
        return uploaded;
    }

    private void saveTask(String uploadId, String quickHash, Long userId, Map<String, Object> task) throws Exception {
        String json = objectMapper.writeValueAsString(task);
        redisTemplate.opsForValue().set(TASK_KEY_PREFIX + uploadId, json, TASK_TTL);
        if (quickHash != null && !quickHash.isBlank()) {
            redisTemplate.opsForValue().set(QH_KEY_PREFIX + userId + ":" + quickHash, json, TASK_TTL);
        }
    }

    // ---- 活跃任务计数（H2） ----

    private void addActiveTask(Long userId, String uploadId) {
        String key = ACTIVE_KEY_PREFIX + userId;
        redisTemplate.opsForSet().add(key, uploadId);
        redisTemplate.expire(key, TASK_TTL);
    }

    private void removeActiveTask(Long userId, String uploadId) {
        redisTemplate.opsForSet().remove(ACTIVE_KEY_PREFIX + userId, uploadId);
    }

    private int activeTaskCount(Long userId) {
        String key = ACTIVE_KEY_PREFIX + userId;
        var members = redisTemplate.opsForSet().members(key);
        if (members == null || members.isEmpty()) return 0;
        // 过滤掉已过期/已完成的任务（任务 key 还在的才算活跃）
        int count = 0;
        for (String uploadId : members) {
            if (Boolean.TRUE.equals(redisTemplate.hasKey(TASK_KEY_PREFIX + uploadId))) {
                count++;
            } else {
                redisTemplate.opsForSet().remove(key, uploadId);
            }
        }
        return count;
    }

    private Map<String, Object> requireTask(String uploadId) throws Exception {
        String json = redisTemplate.opsForValue().get(TASK_KEY_PREFIX + uploadId);
        if (json == null) throw new IllegalStateException("上传任务不存在或已过期");
        @SuppressWarnings("unchecked")
        Map<String, Object> task = objectMapper.readValue(json, Map.class);
        return task;
    }

    private void clearTask(String uploadId, Map<String, Object> task) {
        redisTemplate.delete(TASK_KEY_PREFIX + uploadId);
        String quickHash = (String) task.get("quickHash");
        if (quickHash != null && !quickHash.isBlank()) {
            redisTemplate.delete(QH_KEY_PREFIX + task.get("userId") + ":" + quickHash);
        }
        Object userId = task.get("userId");
        if (userId != null) {
            removeActiveTask(Long.parseLong(userId.toString()), uploadId);
        }
    }

    private String fileSuffix(String filename) {
        if (filename == null) return "";
        int dot = filename.lastIndexOf('.');
        if (dot < 0 || filename.length() - dot > 11) return "";
        String suffix = filename.substring(dot).toLowerCase();
        return suffix.matches("\\.[a-z0-9]+") ? suffix : "";
    }
}
