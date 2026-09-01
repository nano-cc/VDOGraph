package com.example.server.service;

import com.example.server.utils.MinioUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;

/**
 * ffprobe 视频内容校验（上传完成后的权威判定）
 * 通过预签名 GET URL 远程探测（Range 请求只读元数据，不下载全文件）
 */
@Component
public class FfprobeService {

    private static final Logger log = LoggerFactory.getLogger(FfprobeService.class);

    private final MinioUtils minioUtils;

    public FfprobeService(MinioUtils minioUtils) {
        this.minioUtils = minioUtils;
    }

    /**
     * 探测视频文件：返回 null 表示通过，否则返回错误信息
     */
    public String probe(String fileUrl) {
        try {
            String signedUrl = minioUtils.readableSource(fileUrl);
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
                    output.append(line).append('\n');
                }
            }

            boolean finished = process.waitFor(30, TimeUnit.SECONDS);
            if (!finished) {
                process.destroyForcibly();
                return "ffprobe 探测超时";
            }
            if (process.exitValue() != 0) {
                return "无法解析为有效视频: " + output.toString().strip().lines().findFirst().orElse("unknown");
            }
            String duration = output.toString().strip();
            if (duration.isBlank() || "N/A".equals(duration)) {
                return "无法读取视频时长，文件可能损坏";
            }
            log.info("ffprobe ok url={} duration={}s", fileUrl, duration);
            return null;
        } catch (Exception e) {
            log.error("ffprobe failed for {}: {}", fileUrl, e.getMessage());
            return "ffprobe 执行失败: " + e.getMessage();
        }
    }
}
