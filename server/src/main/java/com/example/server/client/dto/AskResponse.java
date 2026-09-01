package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * 知识图谱问答响应（Python AI 服务 /api/v1/query/ask）
 */
public record AskResponse(
        @JsonProperty("answer") String answer,
        @JsonProperty("citations") List<Map<String, Object>> citations,
        @JsonProperty("mode") String mode,
        @JsonProperty("tool_calls") List<String> toolCalls,
        @JsonProperty("duration_ms") Double durationMs
) {}
