package com.example.server.dto;

import java.io.Serializable;

/**
 * KG analyze 阶段消息（kg-analyze topic，#66 拆分）
 * attempt = analyze_attempt（第几次派发 analyze）
 */
public class KgAnalyzeMsg implements Serializable {

    private Long mediaId;
    private String videoUrl;
    private Long userId;
    private Integer attempt;

    public KgAnalyzeMsg() {}

    public KgAnalyzeMsg(Long mediaId, String videoUrl, Integer attempt, Long userId) {
        this.mediaId = mediaId;
        this.videoUrl = videoUrl;
        this.attempt = attempt;
        this.userId = userId;
    }

    public Long getMediaId() { return mediaId; }
    public void setMediaId(Long mediaId) { this.mediaId = mediaId; }
    public String getVideoUrl() { return videoUrl; }
    public void setVideoUrl(String videoUrl) { this.videoUrl = videoUrl; }
    public Integer getAttempt() { return attempt == null ? 1 : attempt; }
    public void setAttempt(Integer attempt) { this.attempt = attempt; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
}
