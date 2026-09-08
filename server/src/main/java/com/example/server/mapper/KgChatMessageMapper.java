package com.example.server.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.example.server.entity.KgChatMessage;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface KgChatMessageMapper extends BaseMapper<KgChatMessage> {
}
