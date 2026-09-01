package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 社区实体
 */
public record CommunityEntity(
        @JsonProperty("id") String id,
        @JsonProperty("name") String name,
        @JsonProperty("type") String type,
        @JsonProperty("description") String description,
        @JsonProperty("source_count") int sourceCount
) {}
