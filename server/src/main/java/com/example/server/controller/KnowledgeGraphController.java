package com.example.server.controller;

import com.example.server.client.AiServiceClient;
import com.example.server.client.dto.AskResponse;
import com.example.server.common.Result;
import com.example.server.service.AuthService;
import com.example.server.service.KgBuildService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;
import org.springframework.http.MediaType;

/**
 * 知识图谱查询接口（代理 Python AI 服务）
 */
@RestController
@RequestMapping("/kg")
public class KnowledgeGraphController {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeGraphController.class);

    private final AiServiceClient aiServiceClient;
    private final KgBuildService kgBuildService;

    public KnowledgeGraphController(AiServiceClient aiServiceClient, KgBuildService kgBuildService) {
        this.aiServiceClient = aiServiceClient;
        this.kgBuildService = kgBuildService;
    }

    /**
     * 查询知识图谱构建状态（前端轮询）
     */
    @org.springframework.web.bind.annotation.GetMapping("/status")
    public Result<java.util.Map<String, String>> buildStatus(
            @RequestParam Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(kgBuildService.getStatus(mediaId));
    }

    /**
     * 知识图谱问答
     * mode: auto（agent 自主决策）/ local（事实类）/ global（概括类）
     */
    @PostMapping("/ask")
    public Result<AskResponse> ask(
            @RequestParam String question,
            @RequestParam(defaultValue = "auto") String mode,
            @RequestParam(required = false) Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        log.info("User {} asks knowledge graph: '{}' (mode={}, mediaId={})", userId, question, mode, mediaId);
        AskResponse response = aiServiceClient.ask(question, mode, userId, mediaId);
        return Result.ok(response);
    }

    /**
     * 知识图谱问答（SSE 流式）：实时返回 agent 思考过程（工具调用及结果），最后返回答案
     */
    @PostMapping(value = "/ask/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public StreamingResponseBody askStream(
            @RequestParam String question,
            @RequestParam(required = false) Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        log.info("User {} asks knowledge graph (stream): '{}' (mediaId={})", userId, question, mediaId);
        return outputStream -> aiServiceClient.askStream(question, userId, mediaId, outputStream);
    }
}
