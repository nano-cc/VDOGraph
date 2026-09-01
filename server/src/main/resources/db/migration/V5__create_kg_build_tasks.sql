-- V5: KG 构建任务表（#66 治理专项）
-- MySQL 唯一事实源：状态/失败信息/重试计数/审计；Redis kg:task:{mediaId} 只做热缓存
CREATE TABLE IF NOT EXISTS kg_build_tasks (
    media_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL,
    video_url VARCHAR(1024) NULL,
    group_id VARCHAR(64) NULL,
    attempt INT NOT NULL DEFAULT 1 COMMENT '总投递尝试次数（兼容旧语义）',
    analyze_attempt INT NOT NULL DEFAULT 0 COMMENT 'analyze 阶段投递次数',
    commit_attempt INT NOT NULL DEFAULT 0 COMMENT 'commit 阶段投递次数',
    error_phase VARCHAR(32) NULL COMMENT 'analyze / commit / dispatch',
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(1000) NULL,
    retryable TINYINT(1) NOT NULL DEFAULT 1 COMMENT '0=永久错误不重试',
    stats TEXT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    completed_at TIMESTAMP(3) NULL,
    PRIMARY KEY (media_id),
    KEY idx_kg_status_time (status, updated_at),
    KEY idx_kg_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
