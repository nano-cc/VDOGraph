package com.example.server.service;

import com.example.server.entity.MediaFile;
import com.example.server.mapper.KgBuildTaskMapper;
import com.example.server.mapper.MediaFileMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * KG 任务对账（#66 建档兜底）
 * 堵住唯一真洞：triggerBuild 时 MySQL 建档失败 → 没有记录、没有消息，任务无声丢失。
 * 定期扫描 media_files(COMPLETED) × kg_build_tasks 左连接，缺任务的自动补建并触发构建。
 */
@Component
public class KgReconcileTask {

    private static final Logger log = LoggerFactory.getLogger(KgReconcileTask.class);

    private final KgBuildTaskMapper kgBuildTaskMapper;
    private final MediaFileMapper mediaFileMapper;
    private final KgBuildService kgBuildService;

    public KgReconcileTask(KgBuildTaskMapper kgBuildTaskMapper,
                           MediaFileMapper mediaFileMapper,
                           KgBuildService kgBuildService) {
        this.kgBuildTaskMapper = kgBuildTaskMapper;
        this.mediaFileMapper = mediaFileMapper;
        this.kgBuildService = kgBuildService;
    }

    /** 每 10 分钟对账一次（对账是兜底，不是实时路径） */
    @Scheduled(fixedDelay = 600_000, initialDelay = 120_000)
    public void reconcile() {
        List<KgBuildTaskMapper.OrphanMedia> orphans;
        try {
            orphans = kgBuildTaskMapper.selectMediaWithoutKgTask();
        } catch (Exception e) {
            log.warn("KG reconcile query failed: {}", e.getMessage());
            return;
        }
        if (orphans.isEmpty()) return;

        for (KgBuildTaskMapper.OrphanMedia orphan : orphans) {
            try {
                MediaFile media = mediaFileMapper.selectById(orphan.mediaId);
                if (media == null || !"COMPLETED".equals(media.getStatus())) continue;
                log.warn("KG reconcile: mediaId={} 有视频记录但无构建任务，补建并触发", orphan.mediaId);
                kgBuildService.triggerBuild(media.getId(), media.getFilePath(), media.getUserId());
            } catch (Exception e) {
                log.warn("KG reconcile error on mediaId={}: {}", orphan.mediaId, e.getMessage());
            }
        }
    }
}
