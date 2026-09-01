package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 社区关系
 */
public record CommunityRelationship(
        @JsonProperty("source") String source,
        @JsonProperty("target") String target,
        @JsonProperty("description") String description,
        @JsonProperty("strength") int strength
) {}
