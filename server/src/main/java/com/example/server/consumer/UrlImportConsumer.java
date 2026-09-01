package com.example.server.consumer;

import com.example.server.dto.UrlImportMsg;
import com.example.server.entity.MediaFile;
import com.example.server.service.FfprobeService;
import com.example.server.service.KgBuildService;
import com.example.server.service.MediaService;
import com.example.server.service.UrlImportStatusService;
import com.example.server.utils.MinioUtils;
import com.example.server.utils.YtDlpUtils;
import org.apache.rocketmq.spring.annotation.RocketMQMessageListener;
import org.apache.rocketmq.spring.core.RocketMQListener;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.File;

/**
 * URL 视频导入消费者（URL 导入异步化）
 * 流程：yt-dlp 下载 → MD5 内容判重（秒传）→ MinIO 上传 → ffprobe 校验 → 完成 → 触发 KG 构建
 * 失败语义：下载失败/校验失败多为永久错误，标记 FAILED 直接 ACK 不重投；
 * MinIO 等基础设施异常上抛，走 broker 重投（maxReconsumeTimes=1）
 */
@Component
@RocketMQMessageListener(
        topic = "${rocketmq.topic.url-import:url-import}",
        consumerGroup = "url-import-consumer",
        maxReconsumeTimes = 1)
public class UrlImportConsumer implements RocketMQListener<UrlImportMsg> {

    private static final Logger log = LoggerFactory.getLogger(UrlImportConsumer.class);
    private static final String LOCK_KEY_PREFIX = "url:import:lock:";

    private final YtDlpUtils ytDlpUtils;
    private final MinioUtils minioUtils;
    private final MediaService mediaService;
    private final FfprobeService ffprobeService;
    private final KgBuildService kgBuildService;
    private final UrlImportStatusService statusService;
    private final org.redisson.api.RedissonClient redissonClient;
    private final com.example.server.service.MediaVisualsService mediaVisualsService;

    public UrlImportConsumer(YtDlpUtils ytDlpUtils,
                             MinioUtils minioUtils,
                             MediaService mediaService,
                             FfprobeService ffprobeService,
                             KgBuildService kgBuildService,
                             UrlImportStatusService statusService,
                             org.redisson.api.RedissonClient redissonClient,
                             com.example.server.service.MediaVisualsService mediaVisualsService) {
        this.ytDlpUtils = ytDlpUtils;
        this.minioUtils = minioUtils;
        this.mediaService = mediaService;
        this.ffprobeService = ffprobeService;
        this.kgBuildService = kgBuildService;
        this.statusService = statusService;
        this.redissonClient = redissonClient;
        this.mediaVisualsService = mediaVisualsService;
    }

    @Override
    public void onMessage(UrlImportMsg msg) {
        if (msg == null || msg.getMediaId() == null || msg.getUrl() == null || msg.getUrl().isBlank()) {
            log.error("url_import_poison_message payload={}", msg == null ? "null" : "mediaId=" + msg.getMediaId());
            return;
        }
        Long mediaId = msg.getMediaId();

        // 执行租约（Redisson，#64）：broker 重投/重复消息并发到达时只有一个执行体
        // watchdog 自动续期，覆盖 yt-dlp 长下载；持锁进程崩溃后 30s 自动释放
        org.redisson.api.RLock leaseLock = redissonClient.getLock(LOCK_KEY_PREFIX + mediaId);
        if (!leaseLock.tryLock()) {
            log.info("url_import_skipped_locked mediaId={}", mediaId);
            return;
        }

        File tempFile = null;
        try {
            MediaFile placeholder = mediaService.findById(mediaId);
            // 占位已被删除（用户取消/判重清理）或已终态：直接跳过
            if (placeholder == null || !"PROCESSING".equals(placeholder.getStatus())) {
                log.info("url_import_skipped_stale mediaId={}", mediaId);
                return;
            }

            // 1. 下载（yt-dlp 失败多为永久错误：不支持的平台/404/超时，标记失败不重投）
            statusService.mark(mediaId, "DOWNLOADING", "正在从源站下载视频");
            try {
                tempFile = ytDlpUtils.downloadVideo(msg.getUrl());
            } catch (Exception e) {
                fail(placeholder, "视频下载失败: " + e.getMessage());
                return;
            }

            // 2. quickHash（XXH3-128）内容判重（与手动上传同算法同列，跨路径秒传有效）
            String quickHash = mediaService.calculateQuickHash(tempFile);
            MediaFile duplicate = mediaService.findByQuickHash(msg.getUserId(), quickHash, mediaId);
            if (duplicate != null) {
                mediaService.deletePlaceholder(placeholder);
                statusService.mark(mediaId, "DEDUP", "相同内容已存在，已为你关联", duplicate.getId());
                log.info("url_import_content_dedup mediaId={} -> existing={}", mediaId, duplicate.getId());
                return;
            }

            // 3. 上传 MinIO（基础设施异常上抛走 MQ 重投）
            String fileUrl = minioUtils.uploadLocalFile(tempFile);

            // 4. ffprobe 内容校验（对齐手动上传 complete 链路，损坏文件拒收）
            statusService.mark(mediaId, "PROBING", "校验视频内容");
            String probeError = ffprobeService.probe(fileUrl);
            if (probeError != null) {
                mediaService.removeObjectQuietly(fileUrl);
                fail(placeholder, "视频内容校验失败（文件损坏或不是有效视频）");
                return;
            }

            // 5. 完成：回填占位记录，触发 KG 构建（与手动上传同一异步链）
            mediaService.completeUrlImport(placeholder, fileUrl, quickHash, "WEB_" + tempFile.getName());
            statusService.mark(mediaId, "READY", "导入完成，知识图谱构建中");
            kgBuildService.triggerBuild(mediaId, fileUrl, msg.getUserId());
            // 时长 + 封面抽帧（增强项，失败不影响导入结果）
            mediaVisualsService.enrich(mediaId, fileUrl);
            log.info("url_import_completed mediaId={}", mediaId);
        } catch (RuntimeException e) {
            // 基础设施类失败（MinIO/Redis 抖动）：上抛走 broker 重投
            log.warn("url_import_retryable_failed mediaId={}: {}", mediaId, e.getMessage());
            throw new IllegalStateException("URL 导入消费失败，交由 RocketMQ 重试", e);
        } catch (Exception e) {
            // 受检异常（MinIO uploadLocalFile 等）：视为可重试的基础设施失败
            log.warn("url_import_retryable_failed mediaId={}: {}", mediaId, e.getMessage());
            throw new IllegalStateException("URL 导入消费失败，交由 RocketMQ 重试", e);
        } finally {
            if (leaseLock.isHeldByCurrentThread()) {
                leaseLock.unlock();
            }
            if (tempFile != null && tempFile.exists() && !tempFile.delete()) {
                log.warn("temporary_video_cleanup_failed path={}", tempFile.getAbsolutePath());
            }
        }
    }

    private void fail(MediaFile placeholder, String message) {
        mediaService.failUrlImport(placeholder);
        statusService.mark(placeholder.getId(), "FAILED", message);
        log.warn("url_import_failed mediaId={}: {}", placeholder.getId(), message);
    }
}
