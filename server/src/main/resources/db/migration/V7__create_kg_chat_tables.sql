-- #72 Agent 会话历史：KG 问答多轮对话持久化
-- Java 侧持有事实源；Python 无状态，每次问答由 Java 加载最近 N 条透传

CREATE TABLE IF NOT EXISTS kg_chat_sessions (
    id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '会话 ID',
    user_id     BIGINT       NOT NULL COMMENT '归属用户',
    media_id    BIGINT       NULL COMMENT '限定单视频问答时的视频 ID（全局问答为 NULL）',
    title       VARCHAR(128) NOT NULL DEFAULT '新会话' COMMENT '标题（首条问题截断生成）',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_user_updated (user_id, updated_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = 'KG 问答会话';

CREATE TABLE IF NOT EXISTS kg_chat_messages (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    session_id  BIGINT       NOT NULL COMMENT '归属会话',
    role        VARCHAR(16)  NOT NULL COMMENT 'user / assistant',
    content     TEXT         NOT NULL COMMENT '消息内容',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_session (session_id, id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = 'KG 问答消息';
