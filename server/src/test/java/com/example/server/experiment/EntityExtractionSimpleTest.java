package com.example.server.experiment;

import com.example.server.dto.VideoContext;
import com.example.server.service.AgentCheckpointService;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;

import java.time.Duration;

/**
 * 实体关系抽取实验 - 简单版本
 * 直接构建 ChatModel，不依赖 DeepSeekUtils
 */
@SpringBootTest
public class EntityExtractionSimpleTest {

    @Autowired
    private AgentCheckpointService checkpointService;

    @Autowired
    private ObjectMapper objectMapper;

    @Value("${ai.deepseek.api-key}")
    private String apiKey;

    @Value("${ai.deepseek.base-url}")
    private String baseUrl;

    @Value("${ai.deepseek.model:deepseek-ai/DeepSeek-V3.2}")
    private String modelName;

    @Test
    public void testExtractFirstSegment() throws Exception {
        // 加载视频上下文
        Long mediaId = 1L;
        VideoContext context = checkpointService.loadContext(mediaId);

        if (context == null) {
            System.err.println("❌ VideoContext not found for mediaId=" + mediaId);
            return;
        }

        System.out.println("✅ 加载 VideoContext 成功");
        System.out.println("总片段数: " + context.segments().size());
        System.out.println();

        // 构建 ChatModel
        OpenAiChatModel chatModel = OpenAiChatModel.builder()
                .baseUrl(baseUrl)
                .apiKey(apiKey)
                .modelName(modelName)
                .timeout(Duration.ofSeconds(300))
                .maxRetries(0)
                .build();

        // 只测试第一个片段
        VideoContext.VideoSegment segment = context.segments().get(0);

        System.out.println("=== Segment 0 ===");
        System.out.println("时间: " + formatTime(segment.startMs()) + "-" + formatTime(segment.endMs()));
        System.out.println("ASR 文本长度: " + segment.transcript().length());
        System.out.println("OCR 文本数量: " + segment.ocrTexts().size());
        System.out.println();

        // 构建输入文本
        String inputText = buildInputText(segment);
        System.out.println("--- 输入文本 ---");
        System.out.println(inputText);
        System.out.println();

        // 构建 Prompt
        String prompt = buildExtractionPrompt(inputText);
        System.out.println("--- Prompt 长度: " + prompt.length() + " 字符 ---");
        System.out.println();

        // 调用 LLM
        System.out.println("--- 调用 LLM... ---");
        String response = chatModel.chat(
                SystemMessage.from("You are a helpful assistant."),
                UserMessage.from(prompt)
        ).aiMessage().text();

        System.out.println("--- LLM 返回结果 ---");
        System.out.println(response);
        System.out.println();
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
                // 过滤掉 OCR 错误信息
                if (!ocrText.contains("Error") && !ocrText.contains("Estimating")) {
                    sb.append(ocrText).append("\n");
                }
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
     * 构建抽取 Prompt（GraphRAG 风格 - 最简版本）
     */
    private String buildExtractionPrompt(String inputText) {
        return """
                -Goal-
                Given a text document and a list of entity types, identify all entities of those types from the text and all relationships among the identified entities.

                -Steps-
                1. Identify all entities. For each identified entity, extract:
                - entity_name: Name of the entity, capitalized
                - entity_type: One of the following types: [Person, Organization, Location, Product, Concept, Event, Other]
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

                -Example-
                Entity_types: ORGANIZATION,PERSON
                Text:
                The Central Institution is scheduled to meet on Monday, with Chair Martin Smith taking questions.
                ######################
                Output:
                ("entity"<|>CENTRAL INSTITUTION<|>ORGANIZATION<|>The Central Institution is setting interest rates)
                ##
                ("entity"<|>MARTIN SMITH<|>PERSON<|>Martin Smith is the chair of the Central Institution)
                ##
                ("relationship"<|>MARTIN SMITH<|>CENTRAL INSTITUTION<|>Martin Smith is the Chair<|>9)
                <|COMPLETE|>

                -Real Data-
                Entity_types: Person, Organization, Location, Product, Concept, Event, Other
                Text: %s
                Output:
                """.formatted(inputText);
    }
}
