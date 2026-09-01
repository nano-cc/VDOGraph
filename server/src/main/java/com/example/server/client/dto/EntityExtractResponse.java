package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * 实体抽取响应
 */
public record EntityExtractResponse(
        @JsonProperty("results") List<SegmentExtraction> results
) {}
