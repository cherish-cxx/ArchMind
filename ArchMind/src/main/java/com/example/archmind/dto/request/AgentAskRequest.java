package com.example.archmind.dto.request;

import com.example.archmind.dto.AgentNodeRef;
import lombok.Data;

/**
 * {@code POST /api/agent/ask} 的请求体。
 *
 * <p>形状对齐 Python 侧 {@code app/models/protocol.py} 的 {@code AskRequest}，
 * 网关会**原样透传**过去（不做字段改名）。</p>
 */
@Data
public class AgentAskRequest {

    /**
     * 要问的项目。
     *
     * <p><b>这个字段必须过租户校验</b> —— 它是 P4 网关存在的理由：
     * Python 拿到它时不做任何归属检查（内部工具接口用的是
     * {@code checkProjectExists}，只问「存不存在」）。</p>
     */
    private Long projectId;

    private String message;

    /** 不传 = 新会话（Python 那边会生成一个并回带）。 */
    private String conversationId;

    /** 用户当前在图上的锚点，可为 null（直接问「这个项目是干嘛的」）。 */
    private AgentNodeRef focus;
}
