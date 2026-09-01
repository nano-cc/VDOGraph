package com.example.server.experiment;

import com.example.server.client.AiServiceClient;
import com.example.server.client.dto.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;
import java.util.Map;

/**
 * AI 服务客户端集成测试
 */
@SpringBootTest
public class AiServiceClientTest {

    @Autowired
    private AiServiceClient aiServiceClient;

    @Test
    public void testHealthCheck() {
        boolean healthy = aiServiceClient.healthCheck();
        System.out.println("AI Service Health: " + (healthy ? "✅ OK" : "❌ Failed"));
        assert healthy;
    }

    @Test
    public void testExtractEntities() {
        // 构建测试数据
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

        // 调用 AI 服务
        EntityExtractResponse response = aiServiceClient.extractEntities(List.of(segmentExtraction));

        System.out.println("✅ Extracted " + response.results().size() + " segments");
        System.out.println("First segment entities: " + response.results().get(0).entities().size());
        System.out.println("First segment relationships: " + response.results().get(0).relationships().size());
    }

    @Test
    public void testDetectCommunities() {
        // 构建测试数据
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

        // 调用 AI 服务
        CommunityDetectResponse response = aiServiceClient.detectCommunities(entities, relationships);

        System.out.println("✅ Detected " + response.communities().size() + " communities");
        System.out.println("Statistics: " + response.statistics());
    }
}
