package com.example.server.service;

import com.example.server.utils.MinioUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;

/**
 * 视频视觉信息增强（#69）：时长落库 + 封面抽帧
 * complete-upload / URL 导入完成后调用；任何失败只记日志，绝不影响上传主流程
 */
@Component
public class MediaVisualsService {

    private static final Logger log = LoggerFactory.getLogger(MediaVisualsService.class);
    private static final long PROBE_TIMEOUT_S = 30;
    private static final long COVER_TIMEOUT_S = 60;

    private final MinioUtils minioUtils;
    private final com.example.server.mapper.MediaFileMapper mediaFileMapper;

    public MediaVisualsService(MinioUtils minioUtils,
                               com.example.server.mapper.MediaFileMapper mediaFileMapper) {
        this.minioUtils = minioUtils;
        this.mediaFileMapper = mediaFileMapper;
    }

    /** 入口：读取时长 + 抽帧封面并落库。失败静默（增强项不阻塞主链路） */
    public void enrich(Long mediaId, String fileUrl) {
        try {
            String signedUrl = minioUtils.readableSource(fileUrl);

            Long durationMs = probeDurationMs(signedUrl);
            String coverObject = captureCover(signedUrl, mediaId);

            if (durationMs != null || coverObject != null) {
                com.example.server.entity.MediaFile update = new com.example.server.entity.MediaFile();
                update.setId(mediaId);
                update.setDurationMs(durationMs);
                update.setCoverUrl(coverObject);
                mediaFileMapper.updateById(update);
                log.info("media_visuals_enriched mediaId={} durationMs={} cover={}", mediaId, durationMs, coverObject);
            }
        } catch (Exception e) {
            log.warn("media_visuals_enrich_failed mediaId={}: {}", mediaId, e.getMessage());
        }
    }

    /** ffprobe 读时长（毫秒），失败返回 null */
    private Long probeDurationMs(String signedUrl) {
        try {
            Process process = new ProcessBuilder(
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    signedUrl
            ).redirectErrorStream(true).start();

            StringBuilder output = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    output.append(line);
                }
            }
            if (!process.waitFor(PROBE_TIMEOUT_S, TimeUnit.SECONDS)) {
                process.destroyForcibly();
                return null;
            }
            String text = output.toString().strip();
            if (text.isBlank() || "N/A".equals(text)) return null;
            return (long) (Double.parseDouble(text) * 1000);
        } catch (Exception e) {
            log.warn("probe duration failed: {}", e.getMessage());
            return null;
        }
    }

    /** ffmpeg 抽帧（1s 处，失败回退首帧）上传 MinIO covers/，返回对象名 */
    private String captureCover(String signedUrl, Long mediaId) {
        Path cover = null;
        try {
            cover = Files.createTempFile("dovideo-cover-", ".jpg");
            if (!grabFrame(signedUrl, cover, "1")) {
                if (!grabFrame(signedUrl, cover, "0")) {
                    return null;
                }
            }
            String objectName = minioUtils.uploadLocalFile(cover.toFile(), mediaId + ".jpg", "covers/");
            return objectName;
        } catch (Exception e) {
            log.warn("capture cover failed mediaId={}: {}", mediaId, e.getMessage());
            return null;
        } finally {
            if (cover != null) {
                try {
                    Files.deleteIfExists(cover);
                } catch (Exception ignored) { }
            }
        }
    }

    private boolean grabFrame(String signedUrl, Path out, String seekSeconds) {
        try {
            Process process = new ProcessBuilder(
                    "ffmpeg", "-y", "-ss", seekSeconds, "-i", signedUrl,
                    "-frames:v", "1", "-q:v", "3", out.toString()
            ).redirectErrorStream(true)
                    .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                    .start();
            if (!process.waitFor(COVER_TIMEOUT_S, TimeUnit.SECONDS)) {
                process.destroyForcibly();
                return false;
            }
            return process.exitValue() == 0 && Files.exists(out) && Files.size(out) > 0;
        } catch (Exception e) {
            return false;
        }
    }
}
