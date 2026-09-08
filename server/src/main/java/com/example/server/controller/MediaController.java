package com.example.server.controller;

import com.example.server.common.Result;
import com.example.server.dto.MediaSummary;
import com.example.server.entity.MediaFile;
import com.example.server.service.AuthService;
import com.example.server.service.KgBuildService;
import com.example.server.service.MediaIngestService;
import com.example.server.service.MediaService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/media")
public class MediaController {

    private final MediaIngestService mediaIngestService;
    private final MediaService mediaService;
    private final KgBuildService kgBuildService;
    private final com.example.server.service.UrlImportStatusService urlImportStatusService;
    private final AuthService authService;

    public MediaController(MediaIngestService mediaIngestService,
                           MediaService mediaService,
                           KgBuildService kgBuildService,
                           com.example.server.service.UrlImportStatusService urlImportStatusService,
                           AuthService authService) {
        this.mediaIngestService = mediaIngestService;
        this.mediaService = mediaService;
        this.kgBuildService = kgBuildService;
        this.urlImportStatusService = urlImportStatusService;
        this.authService = authService;
    }

    // #53：老上传协议（init-upload/upload-status/upload-chunk/complete-upload/直传 upload）已下线，
    // 手动上传统一走 S3UploadController 的 /media/v2/*（quickHash 秒传 + 预签名 multipart 断点续传）

    @PostMapping("/upload-url")
    public Result<MediaSummary> uploadUrl(@RequestParam("url") String url,
                                          @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        // 异步化：返回 PROCESSING 占位，下载/校验/判重在 MQ 消费者执行，前端轮询 import-status
        return Result.ok(MediaSummary.from(mediaIngestService.ingestUrl(url, userId)));
    }

    /**
     * URL 导入进度（前端轮询）：DOWNLOADING/PROBING/READY/DEDUP/FAILED
     */
    @GetMapping("/import-status")
    public Result<java.util.Map<String, String>> importStatus(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(urlImportStatusService.get(id));
    }

    @GetMapping("/list")
    public Result<List<MediaSummary>> getList(
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(mediaService.listByUser(userId).stream()
                .map(MediaSummary::from)
                .toList());
    }

    @GetMapping("/playback")
    public Result<String> playback(@RequestParam Long id,
                                   @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        MediaFile mediaFile = mediaService.requireOwnedMedia(id, userId);
        return Result.ok(mediaService.readableSource(mediaFile.getFilePath()));
    }

    /**
     * 视频封面（302 重定向到 MinIO 预签名 URL）
     * 注意：<img> 标签发不了 Authorization 头，此端点在 WebConfig 里从拦截器排除，
     * 改用 query 参数 token 手动鉴权
     */
    @GetMapping("/cover")
    public org.springframework.http.ResponseEntity<Void> cover(@RequestParam Long id,
                                                               @RequestParam(required = false) String token) {
        Long userId = authService.resolveUser(token == null ? null : "Bearer " + token);
        MediaFile mediaFile = mediaService.requireOwnedMedia(id, userId);
        if (mediaFile.getCoverUrl() == null || mediaFile.getCoverUrl().isBlank()) {
            return org.springframework.http.ResponseEntity.notFound().build();
        }
        String signedUrl = mediaService.readableSource(mediaFile.getCoverUrl());
        return org.springframework.http.ResponseEntity.status(org.springframework.http.HttpStatus.FOUND)
                .location(java.net.URI.create(signedUrl))
                .build();
    }

    @DeleteMapping("/delete")
    public Result<Void> delete(@RequestParam("id") Long id,
                               @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        mediaService.deleteOwnedMedia(id, userId);
        // 同步删除图谱数据（两阶段：快速段同步，清理段异步）
        kgBuildService.deleteGraph(id, userId);
        return Result.ok();
    }
}
