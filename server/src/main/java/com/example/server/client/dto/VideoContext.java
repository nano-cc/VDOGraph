package com.example.server.client.dto;

import java.util.List;

/**
 * 视频上下文
 */
public record VideoContext(
        String source,
        String userGoal,
        List<VideoSegment> segments
) {}
