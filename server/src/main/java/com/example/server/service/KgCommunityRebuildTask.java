package com.example.server.service;

import com.example.server.client.AiServiceClient;
import com.example.server.mapper.KgBuildTaskMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * #83 定期全量 Leiden 社区重建调度
 * 背景：增量社区归属三层链在稀疏图上大量掉 singleton（实测 95%+），
 * global search 依赖多实体社区摘要，需要定期全量重跑纠偏。
 * 策略：每天凌晨 3:17 对每个有 KG 任务的 group 触发一次重建；
 * Python 侧按 singleton 占比 >=50% 才真正执行（图健康时自动跳过）。
 * 与增量写入的并发安全由 Python 端 per-group 社区锁保证。
 */
@Service
public class KgCommunityRebuildTask {

    private static final Logger log = LoggerFactory.getLogger(KgCommunityRebuildTask.class);

    private final KgBuildTaskMapper taskMapper;
    private final AiServiceClient aiServiceClient;

    public KgCommunityRebuildTask(KgBuildTaskMapper taskMapper, AiServiceClient aiServiceClient) {
        this.taskMapper = taskMapper;
        this.aiServiceClient = aiServiceClient;
    }

    @Scheduled(cron = "0 17 3 * * *")
    public void rebuildAll() {
        List<String> groups = taskMapper.selectDistinctGroupIds();
        if (groups.isEmpty()) return;
        log.info("[COMMUNITY-REBUILD] nightly trigger for {} groups", groups.size());
        for (String groupId : groups) {
            try {
                aiServiceClient.rebuildCommunities(groupId, 0.5);
            } catch (Exception e) {
                log.warn("[COMMUNITY-REBUILD] trigger failed group={}: {}", groupId, e.getMessage());
            }
        }
    }
}
