package com.example.server.controller;

import com.example.server.common.ErrorCode;
import com.example.server.common.Result;
import com.example.server.dto.TaskStatus;
import com.example.server.dto.TaskStage;
import com.example.server.entity.MediaFile;
import com.example.server.exception.BusinessException;
import com.example.server.service.AudioExportService;
import com.example.server.service.AuthService;
import com.example.server.service.MediaService;
import com.example.server.service.TaskEventService;
import com.example.server.service.TranscriptionTaskService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

@RestController
@RequestMapping("/analysis")
public class MediaProcessingController {

    private static final Logger log = LoggerFactory.getLogger(MediaProcessingController.class);

    private final AudioExportService audioExportService;
    private final MediaService mediaService;
    private final TranscriptionTaskService transcriptionTaskService;
    private final TaskEventService taskEventService;
    private final com.example.server.client.AiServiceClient aiServiceClient;

    public MediaProcessingController(AudioExportService audioExportService,
                                     MediaService mediaService,
                                     TranscriptionTaskService transcriptionTaskService,
                                     TaskEventService taskEventService,
                                     com.example.server.client.AiServiceClient aiServiceClient) {
        this.audioExportService = audioExportService;
        this.mediaService = mediaService;
        this.transcriptionTaskService = transcriptionTaskService;
        this.taskEventService = taskEventService;
        this.aiServiceClient = aiServiceClient;
    }

    @PostMapping("/transcribe")
    public ResponseEntity<Result<Void>> transcribe(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        mediaService.requireOwnedMedia(id, userId);
        if (!transcriptionTaskService.queue(id)) {
            throw new BusinessException(ErrorCode.CONFLICT, "文字提取任务正在处理中");
        }
        try {
            transcriptionTaskService.transcribe(id);
        } catch (RuntimeException e) {
            // 派发失败要回滚“已排队”标记，避免任务卡在处理中；这是真实的补偿逻辑，予以保留。
            transcriptionTaskService.rejectQueued(id);
            log.warn("transcription_dispatch_rejected mediaId={} userId={}", id, userId, e);
            throw new BusinessException(ErrorCode.SERVICE_UNAVAILABLE, "任务队列已满，请稍后重试");
        }
        // 异步受理：202 Accepted，结果由 transcription-status / SSE 获取。
        return ResponseEntity.accepted().body(Result.ok());
    }

    /**
     * 视频全量文字（优先复用 KG analyze 产物：Neo4j 片段转写拼接，零 ASR 成本）
     * 404 表示尚未建图，前端回退老 ASR 转写流程
     */
    @GetMapping("/kg-transcript")
    public Result<java.util.Map<String, Object>> kgTranscript(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        mediaService.requireOwnedMedia(id, userId);
        java.util.Map<String, Object> transcript = aiServiceClient.kgTranscript(id);
        if (transcript == null) {
            throw new BusinessException(ErrorCode.NOT_FOUND, "该视频尚未完成图谱解析");
        }
        return Result.ok(transcript);
    }

    @GetMapping("/transcription-status")
    public Result<TaskStatus> transcriptionStatus(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        MediaFile mediaFile = mediaService.requireOwnedMedia(id, userId);
        return Result.ok(transcriptionTaskService.status(mediaFile));
    }

    @GetMapping(value = "/transcription-events", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter transcriptionEvents(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        MediaFile mediaFile = mediaService.requireOwnedMedia(id, userId);
        return taskEventService.subscribe(
                id,
                TaskEventService.TRANSCRIPTION,
                "",
                transcriptionTaskService.status(mediaFile),
                TaskStage.TRANSCRIPTION);
    }

    @GetMapping("/download")
    public ResponseEntity<StreamingResponseBody> download(
            @RequestParam Long id,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        MediaFile mediaFile = mediaService.requireOwnedMedia(id, userId);
        Path outputPath = audioExportService.exportMp3(mediaFile);

        StreamingResponseBody body = output -> {
            try {
                Files.copy(outputPath, output);
            } finally {
                Files.deleteIfExists(outputPath);
            }
        };
        String filename = mediaFile.getFilename() == null
                ? "audio.mp3"
                : mediaFile.getFilename().replaceAll("\\.[^.]+$", "") + ".mp3";
        String encodedName = URLEncoder.encode(filename, StandardCharsets.UTF_8);
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType("audio/mpeg"))
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename*=UTF-8''" + encodedName)
                .body(body);
    }
}
