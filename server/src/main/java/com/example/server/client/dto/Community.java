package com.example.server.client.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * 社区
 */
public record Community(
        @JsonProperty("id") String id,
        @JsonProperty("level") int level,
        @JsonProperty("entity_count") int entityCount,
        @JsonProperty("relationship_count") int relationshipCount,
        @JsonProperty("entities") List<CommunityEntity> entities,
        @JsonProperty("relationships") List<CommunityRelationship> relationships,
        @JsonProperty("source_segments") List<String> sourceSegments,
        @JsonProperty("summary") String summary
) {}
