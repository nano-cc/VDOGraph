package com.example.server.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.example.server.entity.KgBuildTask;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface KgBuildTaskMapper extends BaseMapper<KgBuildTask> {

    /** 对账：已上传完成但没有 KG 任务记录的视频（防 triggerBuild 建档失败导致任务无声丢失） */
    @Select("SELECT m.id AS media_id, m.user_id, m.file_path " +
            "FROM media_files m LEFT JOIN kg_build_tasks t ON m.id = t.media_id " +
            "WHERE m.status = 'COMPLETED' AND t.media_id IS NULL " +
            "AND m.upload_time < DATE_SUB(NOW(3), INTERVAL 5 MINUTE) " +
            "ORDER BY m.upload_time ASC LIMIT 50")
    List<OrphanMedia> selectMediaWithoutKgTask();

    /** 对账结果投影 */
    class OrphanMedia {
        public Long mediaId;
        public Long userId;
        public String filePath;
    }
}
