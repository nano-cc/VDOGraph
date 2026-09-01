package com.example.server.service;

import com.example.server.client.AiServiceClient;
import com.example.server.client.dto.*;
import com.example.server.dto.AnalysisMode;
import com.example.server.dto.AgentState;
import com.example.server.dto.VideoContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 知识图谱服务
 * 负责调用 Python AI 服务进行实体关系抽取、实体消歧、关系冲突检测、社区检测
 */
@Service
public class KnowledgeGraphService {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeGraphService.class);

    private final AiServiceClient aiServiceClient;

    public KnowledgeGraphService(AiServiceClient aiServiceClient) {
        this.aiServiceClient = aiServiceClient;
    }

    /**
     * 构建知识图谱
     */
    public KnowledgeGraphResult buildKnowledgeGraph(VideoContext videoContext) {
        log.info("Building knowledge graph from {} segments", videoContext.segments().size());

        // 1. 实体关系抽取
        EntityExtractResponse extractResponse = extractEntities(videoContext);
        log.info("Extracted {} segments with entities and relationships", extractResponse.results().size());

        // 2. 实体消歧
        DisambiguationResponse disambiguationResponse = disambiguateEntities(extractResponse);
        log.info("Disambiguated to {} canonical entities", disambiguationResponse.canonicalEntities().size());

        // 3. 关系冲突检测
        ConflictDetectResponse conflictResponse = detectConflicts(extractResponse, disambiguationResponse);
        log.info("Detected {} canonical relationships", conflictResponse.canonicalRelationships().size());

        // 4. 社区检测
        CommunityDetectResponse communityResponse = detectCommunities(disambiguationResponse, conflictResponse);
        log.info("Detected {} communities", communityResponse.communities().size());

        return new KnowledgeGraphResult(
                extractResponse,
                disambiguationResponse,
                conflictResponse,
                communityResponse
        );
    }

    /**
     * 实体关系抽取
     */
    private EntityExtractResponse extractEntities(VideoContext videoContext) {
        List<SegmentExtraction> segments = videoContext.segments().stream()
                .map(segment -> new SegmentExtraction(
                        generateSegmentId(videoContext.source(), segment.startMs()),
                        (int) (segment.startMs() / 60000),
                        segment.startMs(),
                        segment.endMs(),
                        segment.transcript(),
                        segment.ocrTexts(),
                        List.of(),
                        List.of()
                ))
                .collect(Collectors.toList());

        return aiServiceClient.extractEntities(segments);
    }

    /**
     * 实体消歧
     */
    private DisambiguationResponse disambiguateEntities(EntityExtractResponse extractResponse) {
        List<Map<String, Object>> segments = extractResponse.results().stream()
                .map(this::convertSegmentToMap)
                .collect(Collectors.toList());

        return aiServiceClient.disambiguate(segments);
    }

    /**
     * 关系冲突检测
     */
    private ConflictDetectResponse detectConflicts(EntityExtractResponse extractResponse,
                                                    DisambiguationResponse disambiguationResponse) {
        List<Map<String, Object>> segments = extractResponse.results().stream()
                .map(this::convertSegmentToMap)
                .collect(Collectors.toList());

        return aiServiceClient.detectConflicts(segments, disambiguationResponse.canonicalEntities());
    }

    /**
     * 社区检测
     */
    private CommunityDetectResponse detectCommunities(DisambiguationResponse disambiguationResponse,
                                                       ConflictDetectResponse conflictResponse) {
        List<Object> entities = disambiguationResponse.canonicalEntities().stream()
                .map(e -> (Object) e)
                .collect(Collectors.toList());

        List<Object> relationships = conflictResponse.canonicalRelationships().stream()
                .map(r -> (Object) r)
                .collect(Collectors.toList());

        return aiServiceClient.detectCommunities(entities, relationships);
    }

    /**
     * 生成片段 ID
     */
    private String generateSegmentId(String source, long startMs) {
        return source + "_segment_" + (startMs / 60000);
    }

    /**
     * 转换 SegmentExtraction 为 Map
     */
    private Map<String, Object> convertSegmentToMap(SegmentExtraction segment) {
        Map<String, Object> map = new java.util.HashMap<>();
        map.put("segment_id", segment.segmentId());
        map.put("segment_index", segment.segmentIndex());
        map.put("start_ms", segment.startMs());
        map.put("end_ms", segment.endMs());
        map.put("transcript", segment.transcript() != null ? segment.transcript() : "");
        map.put("ocr_texts", segment.ocrTexts() != null ? segment.ocrTexts() : List.of());
        map.put("entities", segment.entities() != null ? segment.entities().stream()
                .map(this::convertEntityToMap)
                .collect(Collectors.toList()) : List.of());
        map.put("relationships", segment.relationships() != null ? segment.relationships().stream()
                .map(this::convertRelationshipToMap)
                .collect(Collectors.toList()) : List.of());
        return map;
    }

    /**
     * 转换 Entity 为 Map
     */
    private Map<String, Object> convertEntityToMap(Entity entity) {
        Map<String, Object> map = new java.util.HashMap<>();
        map.put("name", entity.name());
        map.put("type", entity.type());
        map.put("description", entity.description() != null ? entity.description() : "");
        map.put("confidence", entity.confidence());
        map.put("source_type", entity.sourceType() != null ? entity.sourceType() : "asr");
        return map;
    }

    /**
     * 转换 Relationship 为 Map
     */
    private Map<String, Object> convertRelationshipToMap(Relationship relationship) {
        Map<String, Object> map = new java.util.HashMap<>();
        map.put("source", relationship.source());
        map.put("target", relationship.target());
        map.put("description", relationship.description() != null ? relationship.description() : "");
        map.put("strength", relationship.strength());
        map.put("confidence", relationship.confidence());
        map.put("source_type", relationship.sourceType() != null ? relationship.sourceType() : "asr");
        return map;
    }

    /**
     * 知识图谱结果
     */
    public record KnowledgeGraphResult(
            EntityExtractResponse extractResponse,
            DisambiguationResponse disambiguationResponse,
            ConflictDetectResponse conflictResponse,
            CommunityDetectResponse communityResponse
    ) {}
}
