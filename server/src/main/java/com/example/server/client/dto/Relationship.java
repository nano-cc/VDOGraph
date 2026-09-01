package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 关系
 */
public record Relationship(
        @JsonProperty("source") String source,
        @JsonProperty("target") String target,
        @JsonProperty("description") String description,
        @JsonProperty("strength") int strength,
        @JsonProperty("confidence") double confidence,
        @JsonProperty("source_type") String sourceType
) {}
