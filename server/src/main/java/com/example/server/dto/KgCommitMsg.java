package com.example.server.dto;

import java.io.Serializable;

/**
 * KG commit 阶段消息（kg-commit topic，#66 拆分）
 * attempt = commit_attempt（第几次派发 commit）
 */
public class KgCommitMsg implements Serializable {

    private Long mediaId;
    private Long userId;
    private Integer attempt;

    public KgCommitMsg() {}

    public KgCommitMsg(Long mediaId, Integer attempt, Long userId) {
        this.mediaId = mediaId;
        this.attempt = attempt;
        this.userId = userId;
    }

    public Long getMediaId() { return mediaId; }
    public void setMediaId(Long mediaId) { this.mediaId = mediaId; }
    public Integer getAttempt() { return attempt == null ? 1 : attempt; }
    public void setAttempt(Integer attempt) { this.attempt = attempt; }
    public Long getUserId() { return userId; }
    public void setUserId(Long userId) { this.userId = userId; }
}
