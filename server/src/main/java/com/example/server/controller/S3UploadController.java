package com.example.server.controller;

import com.example.server.common.Result;
import com.example.server.entity.MediaFile;
import com.example.server.service.AuthService;
import com.example.server.service.FfprobeService;
import com.example.server.service.KgBuildService;
import com.example.server.service.S3UploadService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * S3 multipart 直传上传接口（v2）
 * 流程：init（秒传/续传/新建）→ presign（签发 PUT URL）→ 前端直传 → complete（ffprobe+入库）
 */
@RestController
@RequestMapping("/media/v2")
public class S3UploadController {

    private static final Logger log = LoggerFactory.getLogger(S3UploadController.class);

    private final S3UploadService s3UploadService;
    private final FfprobeService ffprobeService;
    private final KgBuildService kgBuildService;
    private final com.example.server.service.MediaVisualsService mediaVisualsService;

    public S3UploadController(S3UploadService s3UploadService,
                              FfprobeService ffprobeService,
                              KgBuildService kgBuildService,
                              com.example.server.service.MediaVisualsService mediaVisualsService) {
        this.s3UploadService = s3UploadService;
        this.ffprobeService = ffprobeService;
        this.kgBuildService = kgBuildService;
        this.mediaVisualsService = mediaVisualsService;
    }

    @PostMapping("/init-upload")
    public Result<Map<String, Object>> initUpload(
            @RequestParam String filename,
            @RequestParam long size,
            @RequestParam int totalChunks,
            @RequestParam(required = false) String quickHash,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        return Result.ok(s3UploadService.initUpload(filename, size, totalChunks, quickHash, userId));
    }

    @PostMapping("/presign")
    public Result<Map<Integer, String>> presign(
            @RequestParam String uploadId,
            @RequestParam List<Integer> partNumbers,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        return Result.ok(s3UploadService.presignParts(uploadId, partNumbers, userId));
    }

    @PostMapping("/complete-upload")
    public Result<Map<String, Object>> completeUpload(
            @RequestParam String uploadId,
            @RequestParam(required = false) String quickHash,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        Map<String, Object> result = new java.util.HashMap<>(s3UploadService.completeUpload(uploadId, quickHash, userId, ffprobeService::probe));
        Object mediaFile = result.remove("mediaFile");
        // 幂等命中时不重复触发构建
        if (mediaFile instanceof MediaFile mf) {
            kgBuildService.triggerBuild(mf.getId(), mf.getFilePath(), mf.getUserId());
            // 时长 + 封面抽帧（增强项，失败不影响上传结果）
            mediaVisualsService.enrich(mf.getId(), mf.getFilePath());
        }
        return Result.ok(result);
    }

    @PostMapping("/abort")
    public Result<Void> abort(
            @RequestParam String uploadId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        s3UploadService.abortUpload(uploadId, userId);
        return Result.ok();
    }

    @GetMapping("/upload-status")
    public Result<Map<String, Object>> uploadStatus(
            @RequestParam String quickHash,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        Map<String, Object> status = s3UploadService.uploadStatus(quickHash, userId);
        return status == null ? Result.ok(Map.of("status", "NOT_FOUND")) : Result.ok(status);
    }
}
