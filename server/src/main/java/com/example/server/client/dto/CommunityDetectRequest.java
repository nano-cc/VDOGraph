package com.example.server.client.dto;

import java.util.List;
import java.util.Map;

/**
 * 社区检测请求
 */
public record CommunityDetectRequest(
        List<Map<String, Object>> entities,
        List<Map<String, Object>> relationships
) {}
