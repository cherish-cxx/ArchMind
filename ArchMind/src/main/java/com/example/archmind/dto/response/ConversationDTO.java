package com.example.archmind.dto.response;

import com.example.archmind.dto.AgentNodeRef;
import com.example.archmind.dto.RelationRefDTO;
import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * {@code GET /internal/agent/conversations/{id}} 的响应 —— **刷新页面时恢复屏幕用**。
 *
 * <p>只给恢复屏幕必需的两样：{@code viewport}（照它把图重画）与 {@code focus}（当前锚点）。
 * <b>不给 {@code history}</b> —— 那是 Agent 的内部记忆，前端恢复屏幕用不上。</p>
 *
 * <p>会话不存在时只有 {@code exists:false} + {@code reason}，其余字段为 null，
 * 前端据此把那个会话窗口置灰。</p>
 */
@Data
public class ConversationDTO {

    private Boolean exists;
    private String reason;
    private ViewportDTO viewport;
    private AgentNodeRef focus;

    /** Python 侧 {@code Viewport} 的镜像。 */
    @Data
    public static class ViewportDTO {
        private Integer rev;
        private List<String> visibleNodes = new ArrayList<>();
        private List<RelationRefDTO> visibleRelations = new ArrayList<>();
        private AgentNodeRef focusedNode;
        /** 超上限被截断时置 true —— 截断可以，假装没截断不行。 */
        private Boolean truncated;
        /** cumulative | focus */
        private String mode;
        private String centerUid;
        private Integer hops;
        private Integer foldedCount;
    }
}
