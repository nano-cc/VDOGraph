package com.example.server.client;

import com.example.server.client.dto.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

import java.util.List;
import java.util.Map;

/**
 * AI 服务客户端（调用 Python AI 服务）
 */
@Component
public class AiServiceClient {

    private static final Logger log = LoggerFactory.getLogger(AiServiceClient.class);

    private final RestTemplate restTemplate;
    private final RestTemplate shortTimeoutRestTemplate;
    private final String aiServiceUrl;
    private final String apiKey;

    public AiServiceClient(@Value("${ai.service.url:http://localhost:8000}") String aiServiceUrl,
                           @Value("${ai.service.api-key:}") String apiKey) {
        // KG 构建是长任务（长视频 commit 串行 LLM 可达 1 小时以上），读超时放宽到 3 小时
        org.springframework.http.client.SimpleClientHttpRequestFactory factory =
                new org.springframework.http.client.SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(10_000);
        factory.setReadTimeout(10_800_000);
        this.restTemplate = new RestTemplate(factory);

        // 投递类调用（build-async 返回 202）：秒级超时
        org.springframework.http.client.SimpleClientHttpRequestFactory shortFactory =
                new org.springframework.http.client.SimpleClientHttpRequestFactory();
        shortFactory.setConnectTimeout(5_000);
        shortFactory.setReadTimeout(15_000);
        this.shortTimeoutRestTemplate = new RestTemplate(shortFactory);

        this.aiServiceUrl = aiServiceUrl;
        this.apiKey = apiKey;
    }

    private HttpHeaders jsonHeaders() {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        if (apiKey != null && !apiKey.isBlank()) {
            headers.set("X-API-Key", apiKey);
        }
        return headers;
    }

    /**
     * 异步构建（一期异步化，旧链路兼容）：投递即返回 202
     * 新代码请用 analyzeAsync / commitAsync（#66 两阶段拆分）
     */
    public Map<String, Object> buildAsync(Long mediaId, String videoUrl, int attempt, Long userId) {
        String url = aiServiceUrl + "/api/v1/pipeline/build-async";
        Map<String, Object> request = Map.of(
                "video_path", videoUrl,
                "media_id", mediaId,
                "user_goal", "",
                "force", false,
                "attempt", attempt,
                "user_id", userId
        );
        HttpEntity<Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Dispatching async KG build: mediaId={} attempt={}", mediaId, attempt);
        ResponseEntity<Map> response = shortTimeoutRestTemplate.postForEntity(url, httpEntity, Map.class);
        return response.getBody();
    }

    /**
     * analyze 阶段异步投递（#66）：202 即返回，Python 后台执行解析+抽取，
     * 完成后 Python 回调 Java /internal/kg/analyzed 推进状态机
     */
    public Map<String, Object> analyzeAsync(Long mediaId, String videoUrl, int attempt, Long userId) {
        return analyzeAsync(mediaId, videoUrl, attempt, userId, null);
    }

    /** #84 uploadTimeMs：视频真实上传时间（毫秒戳），写入 Neo4j Media.uploaded_at；可空向后兼容 */
    public Map<String, Object> analyzeAsync(Long mediaId, String videoUrl, int attempt, Long userId, Long uploadTimeMs) {
        String url = aiServiceUrl + "/api/v1/pipeline/analyze-async";
        java.util.Map<String, Object> request = new java.util.HashMap<>();
        request.put("video_path", videoUrl);
        request.put("media_id", mediaId);
        request.put("user_goal", "");
        request.put("force", false);
        request.put("attempt", attempt);
        request.put("user_id", userId);
        if (uploadTimeMs != null) request.put("upload_time_ms", uploadTimeMs);
        HttpEntity<java.util.Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Dispatching async KG analyze: mediaId={} attempt={}", mediaId, attempt);
        ResponseEntity<java.util.Map> response = shortTimeoutRestTemplate.postForEntity(url, httpEntity, java.util.Map.class);
        return response.getBody();
    }

    /**
     * commit 阶段异步投递（#66）：202 即返回，Python 后台自持 per-user 写锁执行图写入，
     * 完成后 Python 回调 Java /internal/kg/finished 推进状态机
     */
    public Map<String, Object> commitAsync(Long mediaId, int attempt, Long userId) {
        String url = aiServiceUrl + "/api/v1/pipeline/commit-async";
        Map<String, Object> request = Map.of(
                "media_id", mediaId,
                "attempt", attempt,
                "user_id", userId
        );
        HttpEntity<Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Dispatching async KG commit: mediaId={} attempt={}", mediaId, attempt);
        ResponseEntity<Map> response = shortTimeoutRestTemplate.postForEntity(url, httpEntity, Map.class);
        return response.getBody();
    }

    /**
     * 阶段一：解析 + 抽取（无锁并发）
     */
    public Map<String, Object> analyzeGraph(Long mediaId, String videoUrl) {
        String url = aiServiceUrl + "/api/v1/pipeline/analyze";
        Map<String, Object> request = Map.of(
                "video_path", videoUrl,
                "media_id", mediaId,
                "user_goal", "",
                "force", false
        );
        HttpEntity<Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Calling AI service analyze: mediaId={}", mediaId);
        ResponseEntity<Map> response = restTemplate.postForEntity(url, httpEntity, Map.class);
        return response.getBody();
    }

    /**
     * #85 社区列表（顶层，按实体数降序）
     */
    public Map<String, Object> getCommunities(Long userId) {
        String url = aiServiceUrl + "/api/v1/query/communities?user_id=" + userId;
        ResponseEntity<Map> response = restTemplate.exchange(
                url, org.springframework.http.HttpMethod.GET,
                new HttpEntity<>(jsonHeaders()), Map.class);
        return response.getBody();
    }

    /**
     * 视频总结列表（Neo4j Media.summary）
     */
    public Map<String, Object> getMediaSummaries(Long userId) {
        String url = aiServiceUrl + "/api/v1/query/media-summaries?user_id=" + userId;
        ResponseEntity<Map> response = restTemplate.exchange(
                url, org.springframework.http.HttpMethod.GET,
                new HttpEntity<>(jsonHeaders()), Map.class);
        return response.getBody();
    }

    /**
     * #84 图谱可视化数据（mediaId null = 全局图谱）
     */
    public Map<String, Object> getGraph(Long userId, Long mediaId) {
        String url = aiServiceUrl + "/api/v1/query/graph?user_id=" + userId
                + (mediaId != null ? "&media_id=" + mediaId : "");
        ResponseEntity<Map> response = restTemplate.exchange(
                url, org.springframework.http.HttpMethod.GET,
                new HttpEntity<>(jsonHeaders()), Map.class);
        return response.getBody();
    }

    /**
     * 阶段二：图写入（调用方需持 Redisson 全局写锁 kg:graph:write）
     */
    public Map<String, Object> commitGraph(Long mediaId) {
        String url = aiServiceUrl + "/api/v1/pipeline/commit";
        Map<String, Object> request = Map.of("media_id", mediaId);
        HttpEntity<Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Calling AI service commit: mediaId={}", mediaId);
        ResponseEntity<Map> response = restTemplate.postForEntity(url, httpEntity, Map.class);
        return response.getBody();
    }

    /**
     * #83 触发全量 Leiden 社区重建（异步投递；singleton 占比低于 minRatio 时 Python 侧跳过）
     */
    public Map<String, Object> rebuildCommunities(String groupId, double minRatio) {
        String url = aiServiceUrl + "/api/v1/community/rebuild";
        Map<String, Object> request = Map.of(
                "group_id", groupId,
                "min_singleton_ratio", minRatio
        );
        HttpEntity<Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());
        log.info("Calling AI service community rebuild: groupId={}", groupId);
        ResponseEntity<Map> response = restTemplate.postForEntity(url, httpEntity, Map.class);
        return response.getBody();
    }

    /**
     * 删除视频的图谱数据
     * phase=1 快速段（Media+Segment），phase=2 清理段（关系/孤儿/社区），phase=0 全量
     */
    public void deleteMediaGraph(Long mediaId, int phase, Long userId) {
        String url = aiServiceUrl + "/api/v1/pipeline/media/" + mediaId + "?phase=" + phase + "&user_id=" + userId;
        try {
            org.springframework.http.HttpEntity<Void> entity = new org.springframework.http.HttpEntity<>(jsonHeaders());
            restTemplate.exchange(url, org.springframework.http.HttpMethod.DELETE, entity, Map.class);
            log.info("Deleted graph data for mediaId={} phase={}", mediaId, phase);
        } catch (Exception e) {
            log.warn("Failed to delete graph data for mediaId={} phase={}: {}", mediaId, phase, e.getMessage());
        }
    }

    /**
     * 实体关系抽取
     */
    public EntityExtractResponse extractEntities(List<SegmentExtraction> segments) {
        String url = aiServiceUrl + "/api/v1/entity/extract";

        EntityExtractRequest request = new EntityExtractRequest(segments);

        HttpEntity<EntityExtractRequest> httpEntity = new HttpEntity<>(request, jsonHeaders());

        try {
            log.info("Calling AI service: {} with {} segments", url, segments.size());
            ResponseEntity<EntityExtractResponse> response = restTemplate.postForEntity(
                    url, httpEntity, EntityExtractResponse.class);
            log.info("AI service returned {} results", response.getBody().results().size());
            return response.getBody();
        } catch (Exception e) {
            log.error("Failed to call AI service: {}", url, e);
            throw new RuntimeException("Failed to extract entities", e);
        }
    }

    /**
     * 社区检测
     */
    public CommunityDetectResponse detectCommunities(List<Object> entities, List<Object> relationships) {
        String url = aiServiceUrl + "/api/v1/community/detect";

        CommunityDetectRequest request = new CommunityDetectRequest(
                (List) entities, (List) relationships);

        HttpEntity<CommunityDetectRequest> httpEntity = new HttpEntity<>(request, jsonHeaders());

        try {
            log.info("Calling AI service: {} with {} entities and {} relationships",
                    url, entities.size(), relationships.size());
            ResponseEntity<CommunityDetectResponse> response = restTemplate.postForEntity(
                    url, httpEntity, CommunityDetectResponse.class);
            log.info("AI service returned {} communities", response.getBody().communities().size());
            return response.getBody();
        } catch (Exception e) {
            log.error("Failed to call AI service: {}", url, e);
            throw new RuntimeException("Failed to detect communities", e);
        }
    }

    /**
     * 实体消歧
     */
    public DisambiguationResponse disambiguate(List<Map<String, Object>> segments) {
        String url = aiServiceUrl + "/api/v1/disambiguation/disambiguate";

        DisambiguationRequest request = new DisambiguationRequest(segments);

        HttpEntity<DisambiguationRequest> httpEntity = new HttpEntity<>(request, jsonHeaders());

        try {
            log.info("Calling AI service: {} with {} segments", url, segments.size());
            ResponseEntity<DisambiguationResponse> response = restTemplate.postForEntity(
                    url, httpEntity, DisambiguationResponse.class);
            log.info("AI service returned {} canonical entities",
                    response.getBody().canonicalEntities().size());
            return response.getBody();
        } catch (Exception e) {
            log.error("Failed to call AI service: {}", url, e);
            throw new RuntimeException("Failed to disambiguate entities", e);
        }
    }

    /**
     * 关系冲突检测
     */
    public ConflictDetectResponse detectConflicts(List<Map<String, Object>> segments,
                                                   List<Map<String, Object>> canonicalEntities) {
        String url = aiServiceUrl + "/api/v1/conflict/detect";

        ConflictDetectRequest request = new ConflictDetectRequest(segments, canonicalEntities);

        HttpEntity<ConflictDetectRequest> httpEntity = new HttpEntity<>(request, jsonHeaders());

        try {
            log.info("Calling AI service: {} with {} segments and {} canonical entities",
                    url, segments.size(), canonicalEntities.size());
            ResponseEntity<ConflictDetectResponse> response = restTemplate.postForEntity(
                    url, httpEntity, ConflictDetectResponse.class);
            log.info("AI service returned {} canonical relationships",
                    response.getBody().canonicalRelationships().size());
            return response.getBody();
        } catch (Exception e) {
            log.error("Failed to call AI service: {}", url, e);
            throw new RuntimeException("Failed to detect conflicts", e);
        }
    }

    /**
     * 知识图谱问答
     */
    public AskResponse ask(String question, String mode, Long userId) {
        return ask(question, mode, userId, null);
    }

    /**
     * 知识图谱问答（mediaId 可选：限定单个视频范围内问答）
     */
    public AskResponse ask(String question, String mode, Long userId, Long mediaId) {
        return ask(question, mode, userId, mediaId, null);
    }

    /**
     * 知识图谱问答（#72：history 可选，会话历史 [{role, content}] 透传给 Python agent）
     */
    public AskResponse ask(String question, String mode, Long userId, Long mediaId,
                           java.util.List<java.util.Map<String, String>> history) {
        String url = aiServiceUrl + "/api/v1/query/ask";

        java.util.Map<String, Object> request = new java.util.HashMap<>();
        request.put("question", question);
        request.put("mode", mode == null || mode.isBlank() ? "auto" : mode);
        request.put("user_id", userId);
        if (mediaId != null) {
            request.put("media_id", mediaId);
        }
        if (history != null && !history.isEmpty()) {
            request.put("history", history);
        }

        HttpEntity<java.util.Map<String, Object>> httpEntity = new HttpEntity<>(request, jsonHeaders());

        try {
            log.info("Calling AI service: {} question='{}' mode={} mediaId={} history={}",
                    url, question, mode, mediaId, history == null ? 0 : history.size());
            ResponseEntity<AskResponse> response = restTemplate.postForEntity(
                    url, httpEntity, AskResponse.class);
            log.info("AI service answered, citations: {}",
                    response.getBody().citations() == null ? 0 : response.getBody().citations().size());
            return response.getBody();
        } catch (Exception e) {
            log.error("Failed to call AI service: {}", url, e);
            throw new RuntimeException("Failed to ask knowledge graph", e);
        }
    }

    /**
     * 知识图谱问答（SSE 流式，透传 Python 的事件流到 output）
     */
    public void askStream(String question, Long userId, java.io.OutputStream out) throws java.io.IOException {
        askStream(question, userId, null, null, out, null);
    }

    /**
     * 知识图谱问答（SSE 流式，mediaId 可选：限定单个视频范围内问答）
     */
    public void askStream(String question, Long userId, Long mediaId, java.io.OutputStream out) throws java.io.IOException {
        askStream(question, userId, mediaId, null, out, null);
    }

    /**
     * 知识图谱问答（SSE 流式，#72 全参版）
     * history: 会话历史透传；onFinalAnswer: 从透传流中捕获 final 事件的 answer（Java 侧落库用）
     */
    public void askStream(String question, Long userId, Long mediaId,
                          java.util.List<java.util.Map<String, String>> history,
                          java.io.OutputStream out,
                          java.util.function.Consumer<String> onFinalAnswer) throws java.io.IOException {
        java.net.URL url = new java.net.URL(aiServiceUrl + "/api/v1/query/ask/stream");
        java.net.HttpURLConnection conn = (java.net.HttpURLConnection) url.openConnection();
        conn.setRequestMethod("POST");
        conn.setRequestProperty("Content-Type", "application/json");
        if (apiKey != null && !apiKey.isBlank()) {
            conn.setRequestProperty("X-API-Key", apiKey);
        }
        conn.setDoOutput(true);
        conn.setConnectTimeout(10_000);
        conn.setReadTimeout(300_000);  // agent 多步检索可能超过 1 分钟

        java.util.Map<String, Object> bodyMap = new java.util.HashMap<>();
        bodyMap.put("question", question);
        bodyMap.put("mode", "auto");
        bodyMap.put("user_id", userId);
        if (mediaId != null) {
            bodyMap.put("media_id", mediaId);
        }
        if (history != null && !history.isEmpty()) {
            bodyMap.put("history", history);
        }
        String body = com.alibaba.fastjson2.JSON.toJSONString(bodyMap);
        try (java.io.OutputStream os = conn.getOutputStream()) {
            os.write(body.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        }

        log.info("Streaming AI service ask: '{}' (history={})", question, history == null ? 0 : history.size());
        // 透传 + 尾部缓存（捕获 final 事件用于落库；SSE 事件在末尾，留 64KB 足够）
        java.io.ByteArrayOutputStream tail = new java.io.ByteArrayOutputStream();
        try (java.io.InputStream in = conn.getInputStream()) {
            // 边读边写边 flush（SSE 需要事件级实时性，transferTo 不 flush 会被缓冲区攒住）
            byte[] buffer = new byte[4096];
            int n;
            while ((n = in.read(buffer)) != -1) {
                out.write(buffer, 0, n);
                out.flush();
                if (onFinalAnswer != null) {
                    tail.write(buffer, 0, n);
                    if (tail.size() > 64 * 1024) {
                        byte[] all = tail.toByteArray();
                        tail.reset();
                        tail.write(all, all.length - 48 * 1024, 48 * 1024);
                    }
                }
            }
        } finally {
            conn.disconnect();
        }

        if (onFinalAnswer != null) {
            String answer = extractFinalAnswer(tail.toString(java.nio.charset.StandardCharsets.UTF_8));
            if (answer != null && !answer.isBlank()) {
                onFinalAnswer.accept(answer);
            }
        }
    }

    /** 从 SSE 尾部缓存里找最后一个 "type":"final" 事件并取 answer 字段 */
    private static String extractFinalAnswer(String sseTail) {
        int idx = sseTail.lastIndexOf("\"type\":\"final\"");
        if (idx < 0) idx = sseTail.lastIndexOf("\"type\": \"final\"");
        if (idx < 0) return null;
        int dataStart = sseTail.lastIndexOf("data:", idx);
        if (dataStart < 0) return null;
        String json = sseTail.substring(dataStart + 5).trim();
        // 去掉行尾 SSE 分隔
        int nl = json.indexOf("\n");
        if (nl > 0) json = json.substring(0, nl);
        try {
            com.alibaba.fastjson2.JSONObject obj = com.alibaba.fastjson2.JSON.parseObject(json);
            return obj.getString("answer");
        } catch (Exception e) {
            log.warn("Failed to parse final SSE event for persistence: {}", e.getMessage());
            return null;
        }
    }

    /**
     * 视频全量文字（复用 KG analyze 产物，Python 侧读 Neo4j 片段拼接）
     * 返回 null 表示该视频尚未建图（调用方回退老 ASR 流程）
     */
    public Map<String, Object> kgTranscript(Long mediaId) {
        String url = aiServiceUrl + "/api/v1/query/media/" + mediaId + "/transcript";
        try {
            org.springframework.http.HttpEntity<Void> entity = new org.springframework.http.HttpEntity<>(jsonHeaders());
            ResponseEntity<Map> response = shortTimeoutRestTemplate.exchange(
                    url, org.springframework.http.HttpMethod.GET, entity, Map.class);
            return response.getBody();
        } catch (org.springframework.web.client.HttpClientErrorException.NotFound e) {
            return null;
        } catch (Exception e) {
            log.warn("Failed to get KG transcript for mediaId={}: {}", mediaId, e.getMessage());
            return null;
        }
    }

    /**
     * 健康检查
     */
    public boolean healthCheck() {
        String url = aiServiceUrl + "/health";
        try {
            ResponseEntity<String> response = restTemplate.getForEntity(url, String.class);
            return response.getStatusCode().is2xxSuccessful();
        } catch (Exception e) {
            log.error("AI service health check failed", e);
            return false;
        }
    }
}
