-- V7: 重试预算与派发计数拆分（#79）
-- analyze_attempt/commit_attempt 保留为 fencing token（每次派发递增，消息/心跳/预检语义不变）
-- 新增 analyze_retry/commit_retry 作为重试预算：仅失败驱动的重投递增，背压/队列等待不烧预算
ALTER TABLE kg_build_tasks
    ADD COLUMN analyze_retry INT NOT NULL DEFAULT 0 COMMENT 'analyze 失败重投次数（预算上限依据）',
    ADD COLUMN commit_retry INT NOT NULL DEFAULT 0 COMMENT 'commit 失败重投次数（预算上限依据）';

-- 存量回填：首次派发不算重试，retry = max(attempt - 1, 0)
UPDATE kg_build_tasks
SET analyze_retry = GREATEST(analyze_attempt - 1, 0),
    commit_retry = GREATEST(commit_attempt - 1, 0);
