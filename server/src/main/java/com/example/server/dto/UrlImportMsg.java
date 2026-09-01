package com.example.server.dto;

import java.io.Serializable;

/**
 * URL 视频导入任务消息（url-import topic）
 * 一条导入任务一条消息：yt-dlp 下载是长任务，MQ 化后请求线程不再被占住
 */
public class UrlImportMsg implements Serializable {

    private Long mediaId;
    private String url;
    private Long userId;
    private Integer attempt;

    public UrlImportMsg() {}

    public UrlImportMsg(Long mediaId, String url, Long userId, Integer attempt) {
        this.mediaId = mediaId;
        this.url = url;
        this.userId = userId;
        this.attempt = attempt;
    }

    public Long getMediaId() { return mediaId; }
    public void setMediaId(Long mediaId) { this.mediaId = mediaId; }
    public String getUrl() { return url; }
    public void setUrl(String url) { this.url = url; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
    public Integer getAttempt() { return attempt == null ? 1 : attempt; }
    public void setAttempt(Integer attempt) { this.attempt = attempt; }
}
