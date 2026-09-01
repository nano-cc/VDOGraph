package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * 片段抽取结果
 */
public record SegmentExtraction(
        @JsonProperty("segment_id") String segmentId,
        @JsonProperty("segment_index") int segmentIndex,
        @JsonProperty("start_ms") long startMs,
        @JsonProperty("end_ms") long endMs,
        @JsonProperty("transcript") String transcript,
        @JsonProperty("ocr_texts") List<String> ocrTexts,
        @JsonProperty("entities") List<Entity> entities,
        @JsonProperty("relationships") List<Relationship> relationships
) {}
