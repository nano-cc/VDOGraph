package com.example.server.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@TableName("media_files")
public class MediaFile {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private String filename;
    private String status;
    private String filePath;
    private String contentHash;

    /** 秒传指纹（xxHash3-128 全量，客户端上传前计算） */
    private String quickHash;

    private String aiSummary;
    private String transcriptText;
    private String coverUrl;

    /** 视频时长（毫秒，complete 时 ffprobe 落库） */
    private Long durationMs;

    private LocalDateTime uploadTime;
}
