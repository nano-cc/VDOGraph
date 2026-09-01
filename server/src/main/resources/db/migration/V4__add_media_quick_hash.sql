SET @quick_hash_column_exists = (
    SELECT COUNT(1) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'media_files' AND column_name = 'quick_hash'
);
SET @quick_hash_column_sql = IF(
    @quick_hash_column_exists = 0,
    'ALTER TABLE media_files ADD COLUMN quick_hash VARCHAR(32) NULL',
    'SELECT 1'
);
PREPARE quick_hash_column_statement FROM @quick_hash_column_sql;
EXECUTE quick_hash_column_statement;
DEALLOCATE PREPARE quick_hash_column_statement;

SET @quick_hash_index_exists = (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'media_files' AND index_name = 'idx_media_user_quick_hash'
);
SET @quick_hash_index_sql = IF(
    @quick_hash_index_exists = 0,
    'ALTER TABLE media_files ADD INDEX idx_media_user_quick_hash(user_id, quick_hash)',
    'SELECT 1'
);
PREPARE quick_hash_index_statement FROM @quick_hash_index_sql;
EXECUTE quick_hash_index_statement;
DEALLOCATE PREPARE quick_hash_index_statement;
