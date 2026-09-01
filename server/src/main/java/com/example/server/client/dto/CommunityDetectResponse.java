package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

/**
 * 社区检测响应
 */
public record CommunityDetectResponse(
        @JsonProperty("communities") List<Community> communities,
        @JsonProperty("statistics") Map<String, Object> statistics
) {}
