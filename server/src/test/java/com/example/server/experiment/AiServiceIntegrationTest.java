package com.example.server.experiment;

import com.example.server.client.AiServiceClient;
import com.example.server.client.dto.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;
import java.util.Map;

/**
 * AI 服务完整流程集成测试
 */
@SpringBootTest
public class AiServiceIntegrationTest {

    @Autowired
    private AiServiceClient aiServiceClient;

    @Test
    public void testFullPipeline() {
        System.out.println("=== 测试完整流程 ===\n");

        // 1. 健康检查
        System.out.println("1. 健康检查");
        boolean healthy = aiServiceClient.healthCheck();
        System.out.println("   AI Service Health: " + (healthy ? "✅ OK" : "❌ Failed"));
        assert healthy;

        // 2. 实体关系抽取
        System.out.println("\n2. 实体关系抽取");
        VideoSegment segment = new VideoSegment(
                0,
                60000,
                "谁夺走了中国人的牛肉，自由大家有没有发现牛肉价格最近涨得特别厉害？",
                List.of(),
                List.of()
        );

        SegmentExtraction segmentExtraction = new SegmentExtraction(
                "media_1_segment_0",
                0,
                0,
                60000,
                segment.transcript(),
                segment.ocrTexts(),
                List.of(),
                List.of()
        );

        EntityExtractResponse extractResponse = aiServiceClient.extractEntities(List.of(segmentExtraction));
        System.out.println("   ✅ Extracted " + extractResponse.results().size() + " segments");
        System.out.println("   First segment entities: " + extractResponse.results().get(0).entities().size());
        System.out.println("   First segment relationships: " + extractResponse.results().get(0).relationships().size());

        // 3. 实体消歧
        System.out.println("\n3. 实体消歧");
        List<Map<String, Object>> segments = List.of(
                Map.of(
                        "segment_id", "media_1_segment_0",
                        "segment_index", 0,
                        "entities", List.of(
                                Map.of("name", "进口牛肉", "type", "Product", "description", "从国外进口的牛肉"),
                                Map.of("name", "牛肉价格", "type", "Concept", "description", "牛肉的价格")
                        ),
                        "relationships", List.of(
                                Map.of("source", "进口牛肉", "target", "牛肉价格", "description", "影响", "strength", 8)
                        )
                )
        );

        DisambiguationResponse disambiguationResponse = aiServiceClient.disambiguate(segments);
        System.out.println("   ✅ Disambiguated to " + disambiguationResponse.canonicalEntities().size() + " canonical entities");
        System.out.println("   Merge rate: " + disambiguationResponse.statistics().get("merge_rate"));

        // 4. 关系冲突检测
        System.out.println("\n4. 关系冲突检测");
        ConflictDetectResponse conflictResponse = aiServiceClient.detectConflicts(
                segments,
                disambiguationResponse.canonicalEntities()
        );
        System.out.println("   ✅ Detected " + conflictResponse.canonicalRelationships().size() + " canonical relationships");
        System.out.println("   Duplicates: " + conflictResponse.statistics().get("duplicates"));

        // 5. 社区检测
        System.out.println("\n5. 社区检测");
        List<Object> entities = List.of(
                Map.of("id", "entity_1", "name", "进口牛肉", "type", "Product"),
                Map.of("id", "entity_2", "name", "牛肉价格", "type", "Concept")
        );

        List<Object> relationships = List.of(
                Map.of(
                        "source_entity_id", "entity_1",
                        "target_entity_id", "entity_2",
                        "source_entity_name", "进口牛肉",
                        "target_entity_name", "牛肉价格",
                        "description", "影响",
                        "strength", 8
                )
        );

        CommunityDetectResponse communityResponse = aiServiceClient.detectCommunities(entities, relationships);
        System.out.println("   ✅ Detected " + communityResponse.communities().size() + " communities");
        System.out.println("   Statistics: " + communityResponse.statistics());

        System.out.println("\n=== 所有测试通过 ===");
    }
}
