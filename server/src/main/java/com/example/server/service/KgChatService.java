package com.example.server.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.example.server.entity.KgChatMessage;
import com.example.server.entity.KgChatSession;
import com.example.server.mapper.KgChatMessageMapper;
import com.example.server.mapper.KgChatSessionMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * KG 问答会话历史（#72）：MySQL 事实源，Python 无状态。
 * 每次问答由 Java 加载最近 HISTORY_LIMIT 条透传给 Python，跨轮记忆；
 * 更长历史的压缩由 deepagents 内建 SummarizationMiddleware 在运行内处理。
 */
@Service
public class KgChatService {

    private static final Logger log = LoggerFactory.getLogger(KgChatService.class);

    /** 透传给 Python 的历史条数上限（上下文预算控制；超长会话的最老部分自然截断） */
    private static final int HISTORY_LIMIT = 20;
    private static final int TITLE_MAX_LEN = 30;

    private final KgChatSessionMapper sessionMapper;
    private final KgChatMessageMapper messageMapper;

    public KgChatService(KgChatSessionMapper sessionMapper, KgChatMessageMapper messageMapper) {
        this.sessionMapper = sessionMapper;
        this.messageMapper = messageMapper;
    }

    /** 创建会话（标题先占位，首条问题写入时更新） */
    public KgChatSession createSession(Long userId, Long mediaId) {
        KgChatSession session = new KgChatSession();
        session.setUserId(userId);
        session.setMediaId(mediaId);
        session.setTitle("新会话");
        sessionMapper.insert(session);
        return session;
    }

    /** 我的会话列表（新的在前） */
    public List<KgChatSession> listSessions(Long userId) {
        QueryWrapper<KgChatSession> qw = new QueryWrapper<>();
        qw.eq("user_id", userId).orderByDesc("updated_at");
        return sessionMapper.selectList(qw);
    }

    /** 校验归属并返回会话 */
    public KgChatSession requireOwned(Long sessionId, Long userId) {
        KgChatSession session = sessionMapper.selectById(sessionId);
        if (session == null || !session.getUserId().equals(userId)) {
            throw new IllegalArgumentException("会话不存在");
        }
        return session;
    }

    /** 会话消息（按时间序，用于前端渲染） */
    public List<KgChatMessage> listMessages(Long sessionId, Long userId) {
        requireOwned(sessionId, userId);
        QueryWrapper<KgChatMessage> qw = new QueryWrapper<>();
        qw.eq("session_id", sessionId).orderByAsc("id");
        return messageMapper.selectList(qw);
    }

    /** 加载最近 N 条历史，转成 Python 问答的 history 格式 [{role, content}] */
    public List<Map<String, String>> loadHistory(Long sessionId, Long userId) {
        requireOwned(sessionId, userId);
        QueryWrapper<KgChatMessage> qw = new QueryWrapper<>();
        qw.eq("session_id", sessionId).orderByDesc("id").last("LIMIT " + HISTORY_LIMIT);
        List<KgChatMessage> latest = messageMapper.selectList(qw);
        // 反转为时间正序
        List<Map<String, String>> history = new ArrayList<>();
        for (int i = latest.size() - 1; i >= 0; i--) {
            KgChatMessage m = latest.get(i);
            if (!"user".equals(m.getRole()) && !"assistant".equals(m.getRole())) continue;
            Map<String, String> item = new HashMap<>();
            item.put("role", m.getRole());
            item.put("content", m.getContent());
            history.add(item);
        }
        return history;
    }

    /** 追加一条消息；首条 user 消息时顺手生成会话标题 */
    public void appendMessage(Long sessionId, String role, String content) {
        KgChatMessage message = new KgChatMessage();
        message.setSessionId(sessionId);
        message.setRole(role);
        message.setContent(content);
        messageMapper.insert(message);

        KgChatSession session = sessionMapper.selectById(sessionId);
        if (session != null) {
            if ("user".equals(role) && "新会话".equals(session.getTitle())) {
                String title = content.length() > TITLE_MAX_LEN
                        ? content.substring(0, TITLE_MAX_LEN) + "…" : content;
                session.setTitle(title);
            }
            // updated_at 靠 ON UPDATE 刷新，触发列表排序
            sessionMapper.updateById(session);
        }
    }

    /** 删除会话（连带消息） */
    public void deleteSession(Long sessionId, Long userId) {
        requireOwned(sessionId, userId);
        QueryWrapper<KgChatMessage> qw = new QueryWrapper<>();
        qw.eq("session_id", sessionId);
        messageMapper.delete(qw);
        sessionMapper.deleteById(sessionId);
        log.info("kg_chat_session_deleted sessionId={} userId={}", sessionId, userId);
    }
}
