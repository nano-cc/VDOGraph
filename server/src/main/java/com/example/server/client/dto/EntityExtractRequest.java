package com.example.server.client.dto;

import java.util.List;

/**
 * 实体抽取请求
 */
public record EntityExtractRequest(
        List<SegmentExtraction> segments
) {}
