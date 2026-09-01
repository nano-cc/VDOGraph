package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 实体
 */
public record Entity(
        @JsonProperty("name") String name,
        @JsonProperty("type") String type,
        @JsonProperty("description") String description,
        @JsonProperty("confidence") double confidence,
        @JsonProperty("source_type") String sourceType
) {}
