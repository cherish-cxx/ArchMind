package com.example.archmind.dto.request;

import com.example.archmind.dto.AgentNodeRef;
import com.example.archmind.dto.RelationRefDTO;
import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 前端上报屏幕状态。对外路由是
 * {@code POST /api/agent/conversations/{id}/viewport} —— **会话 id 在路径里**，
 * 网关注入进 {@link #conversationId} 后再转发给 Python 的扁平接口
 * {@code POST /internal/agent/viewport}（那边 id 在 body 里）。
 *
 * <p>⚠️ 这是**整屏快照**不是增量：前端每次必须报当前屏幕上的全部，
 * 少报一个字段，那个字段在服务端就被当成「没有」。</p>
 */
@Data
public class ViewportReportRequest {

    /** 同 {@link AgentAskRequest#getProjectId()}，必须过租户校验。 */
    private Long projectId;

    /**
     * **由网关注入**（取自路径变量），忽略请求体里可能带的同名值 ——
     * 以路径为准，免得前端用一个 id 走路径、用另一个 id 进 body。
     */
    private String conversationId;

    /** 单调递增；后端只接受比当前更大的（旧快照 = 乱序到达，丢弃）。 */
    private Integer rev;

    /**
     * ⚠️ **必须初始化成空列表,不能留 null**。Python 侧这两个字段是
     * {@code list[str] = []} / {@code list[RelationRef] = []} —— Pydantic 的默认值
     * 只在字段**缺失**时生效,显式传 {@code null} 会直接 422（"Input should be a valid list"）。
     * Jackson 默认会把 null 字段也序列化出去,所以 Java 这边不给初值就是发 null。
     */
    private List<String> visibleNodes = new ArrayList<>();

    private List<RelationRefDTO> visibleRelations = new ArrayList<>();

    private AgentNodeRef focusedNode;
}
