ALTER TABLE media_files ADD COLUMN duration_ms BIGINT NULL COMMENT '视频时长（毫秒，complete 时 ffprobe 落库）';
