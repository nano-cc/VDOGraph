package com.example.server.experiment;

import com.example.server.dto.VideoContext;
import com.example.server.service.KnowledgeGraphService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;

/**
 * 知识图谱服务集成测试
 */
@SpringBootTest
public class KnowledgeGraphServiceTest {

    @Autowired
    private KnowledgeGraphService knowledgeGraphService;

    @Test
    public void testBuildKnowledgeGraph() {
        System.out.println("=== 测试知识图谱构建 ===\n");

        // 构建测试数据
        VideoContext.VideoSegment segment = new VideoContext.VideoSegment(
                0,
                60000,
                "谁夺走了中国人的牛肉，自由大家有没有发现牛肉价格最近涨得特别厉害？今年6月份，全国牛肉批发价已经干到了33块钱一斤。",
                List.of(),
                List.of()
        );

        VideoContext videoContext = new VideoContext(
                "test_video.mp4",
                "分析牛肉价格",
                List.of(segment)
        );

        // 构建知识图谱
        KnowledgeGraphService.KnowledgeGraphResult result = knowledgeGraphService.buildKnowledgeGraph(videoContext);

        // 验证结果
        System.out.println("\n=== 结果 ===");
        System.out.println("实体关系抽取: " + result.extractResponse().results().size() + " segments");
        System.out.println("实体消歧: " + result.disambiguationResponse().canonicalEntities().size() + " canonical entities");
        System.out.println("关系冲突检测: " + result.conflictResponse().canonicalRelationships().size() + " canonical relationships");
        System.out.println("社区检测: " + result.communityResponse().communities().size() + " communities");

        System.out.println("\n=== 所有测试通过 ===");
    }
}
