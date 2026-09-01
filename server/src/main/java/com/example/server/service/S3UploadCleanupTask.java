package com.example.server.service;

import io.minio.messages.ListMultipartUploadsResult;
import io.minio.messages.Upload;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.lang.reflect.Field;
import java.time.Duration;
import java.time.ZonedDateTime;
import java.util.List;

/**
 * 孤儿 multipart 上传清扫（G4）
 * 用户放弃/浏览器崩溃导致的未完成 multipart 会话会永久占用 MinIO 存储，
 * 每日清扫发起时间超过 3 天的未完成上传
 */
@Component
public class S3UploadCleanupTask {

    private static final Logger log = LoggerFactory.getLogger(S3UploadCleanupTask.class);
    private static final Duration ORPHAN_AGE = Duration.ofDays(3);

    private final MultipartMinioClient multipartClient;
    private final String bucketName;

    public S3UploadCleanupTask(MultipartMinioClient multipartClient,
                               @Value("${minio.bucketName}") String bucketName) {
        this.multipartClient = multipartClient;
        this.bucketName = bucketName;
    }

    @Scheduled(cron = "0 0 4 * * ?")  // 每天凌晨 4 点
    public void cleanupOrphanUploads() {
        log.info("orphan multipart cleanup started");
        int cleaned = 0;
        try {
            ListMultipartUploadsResult result = multipartClient.listIncompleteUploads(bucketName, "uploads/");
            List<Upload> uploads = extractUploads(result);
            ZonedDateTime cutoff = ZonedDateTime.now().minus(ORPHAN_AGE);

            for (Upload upload : uploads) {
                if (upload.initiated() != null && upload.initiated().isBefore(cutoff)) {
                    try {
                        multipartClient.abortUpload(bucketName, upload.objectName(), upload.uploadId());
                        cleaned++;
                        log.info("orphan multipart aborted: object={} initiated={}", upload.objectName(), upload.initiated());
                    } catch (Exception e) {
                        log.warn("failed to abort orphan multipart {}: {}", upload.objectName(), e.getMessage());
                    }
                }
            }
        } catch (Exception e) {
            log.error("orphan multipart cleanup failed: {}", e.getMessage(), e);
        }
        log.info("orphan multipart cleanup finished, cleaned={}", cleaned);
    }

    /** ListMultipartUploadsResult.uploads 是包级字段无 getter，反射读取 */
    @SuppressWarnings("unchecked")
    private List<Upload> extractUploads(ListMultipartUploadsResult result) {
        try {
            Field field = ListMultipartUploadsResult.class.getDeclaredField("uploads");
            field.setAccessible(true);
            List<Upload> uploads = (List<Upload>) field.get(result);
            return uploads == null ? List.of() : uploads;
        } catch (Exception e) {
            log.warn("failed to read uploads field: {}", e.getMessage());
            return List.of();
        }
    }
}
