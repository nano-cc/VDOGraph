package com.example.server.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * KG 构建任务（MySQL 唯一事实源，#66）
 * Redis kg:task:{mediaId} 只是本表的热缓存（心跳/进度/前端轮询）
 */
@Data
@TableName("kg_build_tasks")
public class KgBuildTask {

    /** 主键即 mediaId（非自增），天然 UNIQUE(media_id) 防重 */
    @TableId(type = IdType.INPUT)
    private Long mediaId;

    private Long userId;

    /** QUEUED/ANALYZING/ANALYZED/COMMITTING/SUCCESS/ANALYZE_FAILED/COMMIT_FAILED/CANCELLED */
    private String status;

    private String videoUrl;

    private String groupId;

    private Integer attempt;

    private Integer analyzeAttempt;

    private Integer commitAttempt;

    private String errorPhase;

    private String errorCode;

    private String errorMessage;

    /** 1=可重试 0=永久错误不重试 */
    private Boolean retryable;

    private String stats;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;

    private LocalDateTime completedAt;
}
