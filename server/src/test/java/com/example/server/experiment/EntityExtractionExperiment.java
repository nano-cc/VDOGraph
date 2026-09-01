package com.example.server.experiment;

import com.example.server.dto.VideoContext;
import com.example.server.service.AgentCheckpointService;
import com.example.server.utils.DeepSeekUtils;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;

/**
 * 实体关系抽取实验
 * 先跑几个片段测试效果
 */
@SpringBootTest
public class EntityExtractionExperiment {

    @Autowired
    private AgentCheckpointService checkpointService;

    @Autowired
    private DeepSeekUtils deepSeekUtils;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    public void testExtractFirstFewSegments() throws Exception {
        // 加载视频上下文（mediaId=1，即"中国人牛肉自由"）
        Long mediaId = 1L;
        VideoContext context = checkpointService.loadContext(mediaId);

        if (context == null) {
            System.err.println("VideoContext not found for mediaId=" + mediaId);
            return;
        }

        System.out.println("=== 加载 VideoContext 成功 ===");
        System.out.println("总片段数: " + context.segments().size());
        System.out.println();

        // 只测试前 3 个片段
        int testSegmentCount = Math.min(3, context.segments().size());

        for (int i = 0; i < testSegmentCount; i++) {
            VideoContext.VideoSegment segment = context.segments().get(i);

            System.out.println("=== Segment " + i + " ===");
            System.out.println("时间: " + formatTime(segment.startMs()) + "-" + formatTime(segment.endMs()));
            System.out.println("ASR 文本长度: " + segment.transcript().length());
            System.out.println("OCR 文本数量: " + segment.ocrTexts().size());
            System.out.println();

            // 构建输入文本
            String inputText = buildInputText(segment);
            System.out.println("--- 输入文本 ---");
            System.out.println(inputText);
            System.out.println();

            // 调用 LLM 抽取
            String result = extractEntitiesAndRelationships(inputText);

            System.out.println("--- LLM 返回结果 ---");
            System.out.println(result);
            System.out.println();
            System.out.println("=".repeat(80));
            System.out.println();
        }
    }

    /**
     * 构建输入文本
     */
    private String buildInputText(VideoContext.VideoSegment segment) {
        StringBuilder sb = new StringBuilder();

        sb.append("[时间: ").append(formatTime(segment.startMs()))
          .append("-").append(formatTime(segment.endMs())).append("]\n");

        sb.append("[语音内容]\n");
        sb.append(segment.transcript()).append("\n");

        if (!segment.ocrTexts().isEmpty()) {
            sb.append("\n[画面文字]\n");
            for (String ocrText : segment.ocrTexts()) {
                sb.append(ocrText).append("\n");
            }
        }

        return sb.toString();
    }

    /**
     * 格式化时间（毫秒 → MM:SS）
     */
    private String formatTime(long ms) {
        long seconds = ms / 1000;
        long minutes = seconds / 60;
        long secs = seconds % 60;
        return String.format("%02d:%02d", minutes, secs);
    }

    /**
     * 调用 LLM 抽取实体和关系
     */
    private String extractEntitiesAndRelationships(String inputText) {
        String prompt = buildExtractionPrompt(inputText);

        // 使用 DeepSeekUtils 的 chat 方法
        // 注意：这里需要查看 DeepSeekUtils 有哪些可用的方法
        // 暂时用一个简单的方式调用
        return callLLM(prompt);
    }

    /**
     * 构建抽取 Prompt（GraphRAG 风格）
     */
    private String buildExtractionPrompt(String inputText) {
        String entityTypes = "Person, Organization, Location, Product, Concept, Event, Other";
        String relationshipTypes = "CAUSES, RELATED_TO, PART_OF, LOCATED_IN, OCCURS_AT, CONTRADICTS, SUPPORTS, MENTIONS";

        return """
                -Goal-
                Given a text document and a list of entity types, identify all entities of those types from the text and all relationships among the identified entities.

                -Steps-
                1. Identify all entities. For each identified entity, extract:
                - entity_name: Name of the entity, capitalized
                - entity_type: One of the following types: [%s]
                - entity_description: Comprehensive description of the entity
                Format: ("entity"<|><entity_name><|><entity_type><|><entity_description>)

                2. Identify all pairs of (source_entity, target_entity) that are clearly related.
                For each pair, extract:
                - source_entity: name of the source entity
                - target_entity: name of the target entity
                - relationship_description: explanation of the relationship
                - relationship_strength: numeric score 1-10
                Format: ("relationship"<|><source_entity<|><target_entity><|><relationship_description><|><relationship_strength>)

                3. Return output as a single list. Use **##** as the list delimiter.

                4. When finished, output <|COMPLETE|>

                -Examples-
                Example 1:
                Entity_types: ORGANIZATION,PERSON
                Text:
                The Verdantis's Central Institution is scheduled to meet on Monday and Thursday, with the institution planning to release its latest policy decision on Thursday at 1:30 p.m. PDT, followed by a press conference where Central Institution Chair Martin Smith will take questions.
                ######################
                Output:
                ("entity"<|>CENTRAL INSTITUTION<|>ORGANIZATION<|>The Central Institution is the Federal Reserve of Verdantis, which is setting interest rates on Monday and Thursday)
                ##
                ("entity"<|>MARTIN SMITH<|>PERSON<|>Martin Smith is the chair of the Central Institution)
                ##
                ("relationship"<|>MARTIN SMITH<|>CENTRAL INSTITUTION<|>Martin Smith is the Chair of the Central Institution<|>9)
                <|COMPLETE|>

                -Real Data-
                Entity_types: %s
                Text: %s
                Output:
                """.formatted(entityTypes, entityTypes, inputText);
    }

    /**
     * 调用 LLM（简单实现）
     */
    private String callLLM(String prompt) {
        // TODO: 需要查看 DeepSeekUtils 有哪些可用的公开方法
        // 暂时返回一个占位符
        return "TODO: 实现 LLM 调用";
    }
}
