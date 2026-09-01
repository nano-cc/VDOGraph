package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * 实体消歧响应
 */
public record DisambiguationResponse(
        @JsonProperty("canonical_entities") List<Map<String, Object>> canonicalEntities,
        @JsonProperty("merge_history") List<Map<String, Object>> mergeHistory,
        @JsonProperty("statistics") Map<String, Object> statistics
) {}
