package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * 关系冲突检测响应
 */
public record ConflictDetectResponse(
        @JsonProperty("canonical_relationships") List<Map<String, Object>> canonicalRelationships,
        @JsonProperty("duplicate_groups") List<Map<String, Object>> duplicateGroups,
        @JsonProperty("statistics") Map<String, Object> statistics
) {}
