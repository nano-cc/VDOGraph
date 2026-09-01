package com.example.server.client.dto;

import java.util.List;

/**
 * 视频片段
 */
public record VideoSegment(
        long startMs,
        long endMs,
        String transcript,
        List<String> ocrTexts,
        List<String> evidenceFrames
) {}
