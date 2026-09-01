package com.example.server.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.HashMap;
import java.util.Map;

/**
 * URL 导入任务状态机（Redis url:import:status:{mediaId} hash）
 * 状态：DOWNLOADING → PROBING → READY / DEDUP（内容判重命中）/ FAILED
 * 前端轮询 /media/import-status 获取进度
 */
@Service
public class UrlImportStatusService {

    private static final Logger log = LoggerFactory.getLogger(UrlImportStatusService.class);
    private static final String STATUS_KEY_PREFIX = "url:import:status:";
    private static final Duration STATUS_TTL = Duration.ofHours(24);

    private final StringRedisTemplate redisTemplate;

    public UrlImportStatusService(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    public void mark(Long mediaId, String status, String message) {
        mark(mediaId, status, message, null);
    }

    public void mark(Long mediaId, String status, String message, Long dedupMediaId) {
        try {
            String key = STATUS_KEY_PREFIX + mediaId;
            Map<String, String> fields = new HashMap<>();
            fields.put("status", status);
            fields.put("message", message == null ? "" : message);
            fields.put("updated_at", String.valueOf(System.currentTimeMillis() / 1000));
            if (dedupMediaId != null) {
                fields.put("dedup_media_id", String.valueOf(dedupMediaId));
            }
            redisTemplate.opsForHash().putAll(key, fields);
            redisTemplate.expire(key, STATUS_TTL);
        } catch (RuntimeException e) {
            log.warn("url_import_status_write_failed mediaId={} status={}", mediaId, status, e);
        }
    }

    public Map<String, String> get(Long mediaId) {
        Map<Object, Object> hash = redisTemplate.opsForHash().entries(STATUS_KEY_PREFIX + mediaId);
        Map<String, String> result = new HashMap<>();
        hash.forEach((k, v) -> result.put(String.valueOf(k), String.valueOf(v)));
        return result;
    }
}
