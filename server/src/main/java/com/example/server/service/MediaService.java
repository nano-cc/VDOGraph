package com.example.server.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.example.server.entity.MediaFile;
import com.example.server.dto.VideoContext;
import com.example.server.mapper.MediaFileMapper;
import com.example.server.utils.MinioUtils;
import com.example.server.utils.QuickHashUtils;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.time.LocalDateTime;
import java.util.List;
import java.util.NoSuchElementException;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.TimeUnit;

@Service
public class MediaService {

    private static final Logger log = LoggerFactory.getLogger(MediaService.class);

    private final MediaFileMapper mediaFileMapper;
    private final StringRedisTemplate redisTemplate;
    private final MinioUtils minioUtils;
    private final ObjectMapper objectMapper;
    private final AgentCheckpointService checkpointService;
    private final AgentTelemetry telemetry;
    private final QdrantVectorStore vectorStore;
    private final VideoContextService videoContextService;

    private static final String MEDIA_MD5_KEY_PREFIX = "media:md5:";
    private static final Set<String> VIDEO_SUFFIXES = Set.of(
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v");

    public MediaService(MediaFileMapper mediaFileMapper,
                        StringRedisTemplate redisTemplate,
                        MinioUtils minioUtils,
                        ObjectMapper objectMapper,
                        AgentCheckpointService checkpointService,
                        AgentTelemetry telemetry,
                        QdrantVectorStore vectorStore,
                        VideoContextService videoContextService) {
        this.mediaFileMapper = mediaFileMapper;
        this.redisTemplate = redisTemplate;
        this.minioUtils = minioUtils;
        this.objectMapper = objectMapper;
        this.checkpointService = checkpointService;
        this.telemetry = telemetry;
        this.vectorStore = vectorStore;
        this.videoContextService = videoContextService;
    }

    /** quickHash（XXH3-128）统一计算：与前端 hash-wasm createXXHash128(0) 输出一致 */
    public String calculateQuickHash(MultipartFile file) throws IOException {
        try (InputStream inputStream = file.getInputStream()) {
            return QuickHashUtils.xxh3Hex(inputStream);
        }
    }

    public String calculateQuickHash(File file) throws IOException {
        return QuickHashUtils.xxh3Hex(file);
    }

    public void rememberContentHash(Long mediaId, String md5) {
        if (mediaId == null || md5 == null || md5.isBlank()) return;
        try {
            redisTemplate.opsForValue().set(MEDIA_MD5_KEY_PREFIX + mediaId, md5);
        } catch (RuntimeException e) {
            log.warn("media_hash_cache_write_failed mediaId={}", mediaId, e);
        }
    }

    public MediaFile saveUploadedMedia(String filename, String fileUrl, Long userId, String quickHash) {
        MediaFile mediaFile = new MediaFile();
        mediaFile.setFilename(normalizeVideoFilename(filename));
        mediaFile.setFilePath(fileUrl);
        mediaFile.setStatus("COMPLETED");
        mediaFile.setUploadTime(LocalDateTime.now());
        mediaFile.setUserId(userId);
        mediaFile.setQuickHash(quickHash);
        try {
            mediaFileMapper.insert(mediaFile);
            rememberContentHash(mediaFile.getId(), quickHash);
            invalidateUserList(userId);
            return mediaFile;
        } catch (RuntimeException e) {
            removeUploadedObject(fileUrl, e);
            throw e;
        }
    }

    public List<MediaFile> listByUser(Long userId) {
        String cacheKey = userListKey(userId);
        try {
            String cached = redisTemplate.opsForValue().get(cacheKey);
            if (cached != null) {
                return objectMapper.readValue(cached, new TypeReference<List<MediaFile>>() { });
            }
        } catch (Exception e) {
            log.warn("media_list_cache_read_failed userId={}", userId, e);
        }

        QueryWrapper<MediaFile> query = new QueryWrapper<>();
        List<MediaFile> mediaFiles = mediaFileMapper.selectList(
                query.select("id", "filename", "status", "cover_url", "duration_ms", "upload_time")
                        .eq("user_id", userId)
                        .orderByDesc("id"));
        try {
            redisTemplate.opsForValue().set(
                    cacheKey, objectMapper.writeValueAsString(mediaFiles), 30, TimeUnit.MINUTES);
        } catch (Exception e) {
            log.warn("media_list_cache_write_failed userId={}", userId, e);
        }
        return mediaFiles;
    }

    public String contentHash(Long mediaId) {
        try {
            String cached = redisTemplate.opsForValue().get(MEDIA_MD5_KEY_PREFIX + mediaId);
            if (cached != null && !cached.isBlank()) return cached;
        } catch (RuntimeException e) {
            log.warn("media_hash_cache_read_failed mediaId={}", mediaId, e);
        }

        MediaFile mediaFile = mediaFileMapper.selectById(mediaId);
        // 新数据读 quick_hash（XXH3-128），历史数据兜底 content_hash（旧 MD5）
        String persisted = mediaFile == null ? null
                : (mediaFile.getQuickHash() != null && !mediaFile.getQuickHash().isBlank()
                        ? mediaFile.getQuickHash() : mediaFile.getContentHash());
        rememberContentHash(mediaId, persisted);
        return persisted;
    }

    public void deleteOwnedMedia(Long mediaId, Long userId) {
        MediaFile mediaFile = requireOwnedMedia(mediaId, userId);
        mediaFileMapper.deleteById(mediaId);
        if (mediaFile.getFilePath() != null && mediaFile.getFilePath().startsWith("http")) {
            try {
                minioUtils.removeFile(mediaFile.getFilePath());
            } catch (RuntimeException e) {
                log.warn("media_object_cleanup_failed mediaId={} path={}",
                        mediaId, mediaFile.getFilePath(), e);
            }
        }
        purgeRuntimeArtifacts(mediaId);
        invalidateUserList(userId);
    }

    public boolean exists(Long mediaId) {
        return mediaId != null && mediaFileMapper.selectById(mediaId) != null;
    }

    public void purgeRuntimeArtifacts(Long mediaId) {
        VideoContext context = null;
        try {
            context = checkpointService.loadContext(mediaId);
        } catch (RuntimeException e) {
            log.warn("media_evidence_manifest_read_failed mediaId={}", mediaId, e);
        }
        videoContextService.deleteEvidenceFrames(context);
        try {
            redisTemplate.delete(List.of(
                    MEDIA_MD5_KEY_PREFIX + mediaId,
                    "transcription:active:" + mediaId,
                    "transcription:state:" + mediaId));
            checkpointService.deleteMedia(mediaId);
            telemetry.deleteTask(mediaId);
            vectorStore.deleteMedia(mediaId);
        } catch (RuntimeException e) {
            log.warn("media_runtime_cleanup_failed mediaId={}", mediaId, e);
        }
    }

    public void invalidateUserList(Long userId) {
        if (userId == null) return;
        try {
            redisTemplate.delete(userListKey(userId));
        } catch (RuntimeException e) {
            log.warn("media_list_cache_invalidation_failed userId={}", userId, e);
        }
    }

    /**
     * 按 quickHash（XXH3-128）查重（URL 导入下载完成后的"秒传"判定，排除占位记录自身）。
     * 与手动上传前端算的 quickHash 同算法同列，跨路径判重有效。
     */
    public MediaFile findByQuickHash(Long userId, String quickHash, Long excludeId) {
        if (quickHash == null || quickHash.isBlank()) return null;
        QueryWrapper<MediaFile> query = new QueryWrapper<>();
        query.eq("user_id", userId)
                .eq("quick_hash", quickHash)
                .eq("status", "COMPLETED")
                .ne(excludeId != null, "id", excludeId)
                .orderByAsc("id")
                .last("LIMIT 1");
        return mediaFileMapper.selectOne(query);
    }

    /**
     * URL 导入占位记录（PROCESSING，消费者下载完成后回填 filePath 等最终信息）
     */
    public MediaFile createImportPlaceholder(String filename, Long userId) {
        MediaFile mediaFile = new MediaFile();
        mediaFile.setFilename(normalizeVideoFilename(filename));
        mediaFile.setFilePath("");
        mediaFile.setStatus("PROCESSING");
        mediaFile.setUploadTime(LocalDateTime.now());
        mediaFile.setUserId(userId);
        mediaFileMapper.insert(mediaFile);
        invalidateUserList(userId);
        return mediaFile;
    }

    public MediaFile findById(Long mediaId) {
        return mediaId == null ? null : mediaFileMapper.selectById(mediaId);
    }

    /**
     * 删除占位记录（内容判重命中时：占位没产生任何外部资源，直接移除）
     */
    public void deletePlaceholder(MediaFile mediaFile) {
        mediaFileMapper.deleteById(mediaFile.getId());
        invalidateUserList(mediaFile.getUserId());
    }

    /**
     * URL 导入完成：占位记录落最终信息（MinIO 地址/quickHash/真实文件名/完成态）
     */
    public void completeUrlImport(MediaFile mediaFile, String fileUrl, String quickHash, String filename) {
        mediaFile.setFilePath(fileUrl);
        mediaFile.setQuickHash(quickHash);
        mediaFile.setFilename(normalizeVideoFilename(filename));
        mediaFile.setStatus("COMPLETED");
        mediaFileMapper.updateById(mediaFile);
        rememberContentHash(mediaFile.getId(), quickHash);
        invalidateUserList(mediaFile.getUserId());
    }

    /**
     * URL 导入失败：占位记录置失败态（展示给用户后可手动删除重试）
     */
    public void failUrlImport(MediaFile mediaFile) {
        mediaFile.setStatus("FAILED");
        mediaFileMapper.updateById(mediaFile);
        invalidateUserList(mediaFile.getUserId());
    }

    /**
     * 清理导入失败时已上传的 MinIO 对象（尽力而为，不阻断主流程）
     */
    public void removeObjectQuietly(String fileUrl) {
        if (fileUrl == null) return;
        try {
            minioUtils.removeFile(fileUrl);
        } catch (RuntimeException e) {
            log.warn("url_import_object_cleanup_failed path={}", fileUrl, e);
        }
    }

    public String readableSource(String source) {
        return minioUtils.readableSource(source);
    }

    public String normalizeVideoFilename(String filename) {
        if (filename == null || filename.isBlank()) {
            throw new IllegalArgumentException("视频文件名不能为空");
        }
        String normalized = filename.replace('\\', '/');
        normalized = normalized.substring(normalized.lastIndexOf('/') + 1).trim();
        if (normalized.isBlank() || normalized.length() > 255) {
            throw new IllegalArgumentException("视频文件名无效或过长");
        }
        String suffix = fileSuffix(normalized).toLowerCase(java.util.Locale.ROOT);
        if (!VIDEO_SUFFIXES.contains(suffix)) {
            throw new IllegalArgumentException("仅支持 MP4、MOV、MKV、AVI、WEBM 和 M4V 视频");
        }
        return normalized;
    }

    public MediaFile requireOwnedMedia(Long mediaId, Long userId) {
        MediaFile mediaFile = mediaFileMapper.selectById(mediaId);
        if (mediaFile == null) throw new NoSuchElementException("文件不存在");
        if (!Objects.equals(mediaFile.getUserId(), userId)) {
            throw new SecurityException("无权访问该文件");
        }
        return mediaFile;
    }

    private String userListKey(Long userId) {
        return "media:list:v2:user:" + userId;
    }

    private String fileSuffix(String filename) {
        int dot = filename.lastIndexOf('.');
        return dot >= 0 ? filename.substring(dot) : "";
    }

    private void removeUploadedObject(String fileUrl, RuntimeException originalError) {
        try {
            minioUtils.removeFile(fileUrl);
        } catch (RuntimeException cleanupError) {
            originalError.addSuppressed(cleanupError);
            log.warn("uploaded_object_rollback_failed path={}", fileUrl, cleanupError);
        }
    }
}
