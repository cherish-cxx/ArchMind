package com.example.archmind.dto.response;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 会话历史 —— 打开一个已存在的会话时恢复消息列表。
 *
 * <p>{@code messages} 的形状由 Python 侧定义（user / agent 两种角色），
 * Java 只做透传：这里不重写 sections / citations / steps 这些结构化字段。</p>
 */
@Data
public class ConversationHistoryDTO {

    private String conversationId;
    private Boolean exists;
    private String reason;
    private List<Map<String, Object>> messages = new ArrayList<>();
}
