package com.example.server.service;

import com.example.server.dto.UrlImportMsg;
import com.example.server.entity.MediaFile;
import org.apache.rocketmq.spring.core.RocketMQTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.util.DigestUtils;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

@Service
public class MediaIngestService {

    private static final Logger log = LoggerFactory.getLogger(MediaIngestService.class);
    /** 同一用户同一 URL 的判重窗口（双击/重试秒回，对齐 quickHash 秒传的体验） */
    private static final String URL_DEDUP_PREFIX = "url:import:dedup:";
    private static final Duration URL_DEDUP_TTL = Duration.ofHours(24);

    private final MediaService mediaService;
    private final RocketMQTemplate rocketMQTemplate;
    private final StringRedisTemplate redisTemplate;
    private final UrlImportStatusService importStatusService;
    private final String urlImportTopic;

    public MediaIngestService(MediaService mediaService,
                              RocketMQTemplate rocketMQTemplate,
                              StringRedisTemplate redisTemplate,
                              UrlImportStatusService importStatusService,
                              @Value("${rocketmq.topic.url-import:url-import}") String urlImportTopic) {
        this.mediaService = mediaService;
        this.rocketMQTemplate = rocketMQTemplate;
        this.redisTemplate = redisTemplate;
        this.importStatusService = importStatusService;
        this.urlImportTopic = urlImportTopic;
    }

    /**
     * URL 视频导入（异步化：创建占位 + 发 MQ 即返回，下载/校验/判重在 UrlImportConsumer 执行）
     * 返回 PROCESSING 占位记录；前端按 /media/import-status 轮询推进
     *
     * #53：老 multipart 直传（ingestFile）已随老上传协议下线，手动上传统一走 S3 /media/v2/*
     */
    public MediaFile ingestUrl(String url, Long userId) {
        if (url == null || url.isBlank()) throw new IllegalArgumentException("视频链接不能为空");

        // L1：同一用户同一 URL 判重（24h 窗口内直接复用进行中的/已完成的记录）
        String urlDedupKey = URL_DEDUP_PREFIX + userId + ":"
                + DigestUtils.md5DigestAsHex(url.getBytes(StandardCharsets.UTF_8));
        String existingId = redisTemplate.opsForValue().get(urlDedupKey);
        if (existingId != null) {
            MediaFile existing = mediaService.findById(Long.valueOf(existingId));
            // 已完成或进行中才复用；失败的占位不复用（允许用户重试）
            if (existing != null && ("COMPLETED".equals(existing.getStatus())
                    || "PROCESSING".equals(existing.getStatus()))) {
                log.info("url_import_dedup_hit userId={} mediaId={}", userId, existing.getId());
                return existing;
            }
        }

        String host = URI.create(url).getHost();
        // 占位文件名带 .mp4 后缀（normalizeVideoFilename 有扩展名校验；最终文件名下载完成后回填）
        MediaFile placeholder = mediaService.createImportPlaceholder(
                "WEB_" + (host != null ? host : "url") + ".mp4", userId);
        importStatusService.mark(placeholder.getId(), "DOWNLOADING", "导入任务已排队");
        try {
            rocketMQTemplate.convertAndSend(urlImportTopic,
                    new UrlImportMsg(placeholder.getId(), url, userId, 1));
            redisTemplate.opsForValue().set(urlDedupKey, String.valueOf(placeholder.getId()), URL_DEDUP_TTL);
            log.info("url_import_dispatched mediaId={} host={}", placeholder.getId(), host);
        } catch (RuntimeException e) {
            mediaService.failUrlImport(placeholder);
            importStatusService.mark(placeholder.getId(), "FAILED", "导入任务投递失败，请重试");
            throw new IllegalStateException("导入任务投递失败", e);
        }
        return placeholder;
    }
}
