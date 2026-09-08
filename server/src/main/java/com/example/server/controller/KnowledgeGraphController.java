package com.example.server.controller;

import com.example.server.client.AiServiceClient;
import com.example.server.client.dto.AskResponse;
import com.example.server.common.Result;
import com.example.server.entity.KgChatSession;
import com.example.server.service.AuthService;
import com.example.server.service.KgBuildService;
import com.example.server.service.KgChatService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;
import org.springframework.http.MediaType;

import java.util.List;
import java.util.Map;

/**
 * 知识图谱查询接口（代理 Python AI 服务）
 * #72：会话历史（sessionId 关联 kg_chat_sessions/messages，Java 持有事实源，Python 无状态）
 */
@RestController
@RequestMapping("/kg")
public class KnowledgeGraphController {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeGraphController.class);

    private final AiServiceClient aiServiceClient;
    private final KgBuildService kgBuildService;
    private final KgChatService kgChatService;

    public KnowledgeGraphController(AiServiceClient aiServiceClient, KgBuildService kgBuildService,
                                    KgChatService kgChatService) {
        this.aiServiceClient = aiServiceClient;
        this.kgBuildService = kgBuildService;
        this.kgChatService = kgChatService;
    }

    /**
     * 查询知识图谱构建状态（前端轮询）
     */
    @GetMapping("/status")
    public Result<java.util.Map<String, String>> buildStatus(
            @RequestParam Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(kgBuildService.getStatus(mediaId));
    }

    /**
     * #77 手动重试失败任务（重试预算到上限后用户自助恢复）
     * 重置预算+错误信息，回到待派发位并立即重新投递
     */
    @PostMapping("/retry")
    public Result<String> retryBuild(
            @RequestParam Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return kgBuildService.manualRetry(mediaId, userId);
    }

    // ==================== 会话管理（#72） ====================

    /** 新建会话（mediaId 可选：单视频问答会话） */
    @PostMapping("/session/create")
    public Result<KgChatSession> createSession(
            @RequestParam(required = false) Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(kgChatService.createSession(userId, mediaId));
    }

    /** 我的会话列表（新的在前） */
    @GetMapping("/session/list")
    public Result<List<KgChatSession>> listSessions(
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(kgChatService.listSessions(userId));
    }

    /** 会话消息（按时间序） */
    @GetMapping("/session/messages")
    public Result<List<com.example.server.entity.KgChatMessage>> listMessages(
            @RequestParam Long sessionId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(kgChatService.listMessages(sessionId, userId));
    }

    /** 删除会话（连带消息） */
    @DeleteMapping("/session/delete")
    public Result<Void> deleteSession(
            @RequestParam Long sessionId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        kgChatService.deleteSession(sessionId, userId);
        return Result.ok();
    }

    // ==================== 问答 ====================

    /**
     * #84 图谱可视化数据（代理 Python）：mediaId 不传=全局图谱，传=单视频子图
     */
    @GetMapping("/graph")
    public Result<Map<String, Object>> graph(
            @RequestParam(required = false) Long mediaId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(aiServiceClient.getGraph(userId, mediaId));
    }

    /**
     * #85 社区列表（代理 Python）：全局图谱的社区划分面板
     */
    @GetMapping("/communities")
    public Result<Map<String, Object>> communities(
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(aiServiceClient.getCommunities(userId));
    }

    /**
     * 视频总结列表（代理 Python，Neo4j Media.summary）：前端卡片展示
     */
    @GetMapping("/media-summaries")
    public Result<Map<String, Object>> mediaSummaries(
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        return Result.ok(aiServiceClient.getMediaSummaries(userId));
    }

    /**
     * 知识图谱问答
     * mode: auto（agent 自主决策）/ local（事实类）/ global（概括类）
     * sessionId 可选：带则加载历史并落库本轮问答（多轮对话）
     */
    @PostMapping("/ask")
    public Result<AskResponse> ask(
            @RequestParam String question,
            @RequestParam(defaultValue = "auto") String mode,
            @RequestParam(required = false) Long mediaId,
            @RequestParam(required = false) Long sessionId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        log.info("User {} asks knowledge graph: '{}' (mode={}, mediaId={}, sessionId={})",
                userId, question, mode, mediaId, sessionId);

        List<Map<String, String>> history = null;
        if (sessionId != null) {
            history = kgChatService.loadHistory(sessionId, userId);
            kgChatService.appendMessage(sessionId, "user", question);
        }
        AskResponse response = aiServiceClient.ask(question, mode, userId, mediaId, history);
        if (sessionId != null && response.answer() != null && !response.answer().isBlank()) {
            kgChatService.appendMessage(sessionId, "assistant", response.answer());
        }
        return Result.ok(response);
    }

    /**
     * 知识图谱问答（SSE 流式）：实时返回 agent 思考过程（工具调用/子 Agent 派发），最后返回答案
     * sessionId 可选：流结束后从透传流捕获 final 答案落库
     */
    @PostMapping(value = "/ask/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public StreamingResponseBody askStream(
            @RequestParam String question,
            @RequestParam(required = false) Long mediaId,
            @RequestParam(required = false) Long sessionId,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        log.info("User {} asks knowledge graph (stream): '{}' (mediaId={}, sessionId={})",
                userId, question, mediaId, sessionId);

        List<Map<String, String>> history = null;
        if (sessionId != null) {
            history = kgChatService.loadHistory(sessionId, userId);
            kgChatService.appendMessage(sessionId, "user", question);
        }
        final Long sid = sessionId;
        final List<Map<String, String>> hist = history;
        return outputStream -> aiServiceClient.askStream(question, userId, mediaId, hist, outputStream,
                answer -> {
                    if (sid != null) {
                        kgChatService.appendMessage(sid, "assistant", answer);
                    }
                });
    }
}
