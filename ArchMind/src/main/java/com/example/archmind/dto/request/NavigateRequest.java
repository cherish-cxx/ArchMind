package com.example.archmind.dto.request;

import com.example.archmind.dto.AgentNodeRef;
import lombok.Data;

/**
 * {@code POST /api/agent/navigate} 的请求体。
 *
 * <p>和 {@link AgentAskRequest} 的区别是<b>谁判的意图</b>：{@code /ask} 靠 LLM 从自然语言里
 * 判出「这是导航」，这里前端<b>直接声明</b>，因此一轮下来零 token。</p>
 */
@Data
public class NavigateRequest {

    /** 同 {@link AgentAskRequest#getProjectId()}，必须过租户校验。 */
    private Long projectId;

    /** 导航作用于一个**已经存在**的会话（焦点栈在它的 Context 里），所以必填。 */
    private String conversationId;

    /**
     * {@code BACK}（退回去）或 {@code EXPAND}（展开）。
     *
     * <p>用 String 不用枚举：**校验的真相源在 Python**（{@code NavigateOp}），
     * Java 再定义一份枚举就有两处会漂。且目前只有 {@code BACK} 真正实现。</p>
     */
    private String op;

    /** 目前 {@code BACK} 用不到，留给 {@code EXPAND}。 */
    private AgentNodeRef target;
}
