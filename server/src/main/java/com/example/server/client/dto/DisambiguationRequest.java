package com.example.server.client.dto;

import java.util.List;
import java.util.Map;

/**
 * 实体消歧请求
 */
public record DisambiguationRequest(
        List<Map<String, Object>> segments
) {}
