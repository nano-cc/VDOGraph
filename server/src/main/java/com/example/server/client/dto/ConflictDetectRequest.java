package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * 关系冲突检测请求
 */
public record ConflictDetectRequest(
        @JsonProperty("segments") List<Map<String, Object>> segments,
        @JsonProperty("canonical_entities") List<Map<String, Object>> canonicalEntities
) {}
