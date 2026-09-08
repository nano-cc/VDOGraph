package com.example.server.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

@Data
@TableName("kg_chat_messages")
public class KgChatMessage {
    @TableId(type = IdType.AUTO)
    private Long id;
    private Long sessionId;
    private String role;
    private String content;
    private LocalDateTime createdAt;
}
