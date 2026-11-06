package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 会话索引项 —— 「最近会话」列表用。
 *
 * <p>一个项目可以有多个会话，每个会话是一份独立上下文；
 * 这里只给索引所需的元信息，消息体走 {@code /conversations/{id}/messages}。</p>
 */
@Data
public class ConversationSummaryDTO {

    private String id;
    private Long projectId;
    private String title;
    private Integer turnCount;
    private String status;
    private String createdAt;
    private String updatedAt;
}
