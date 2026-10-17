package com.example.archmind.dto.response;

import lombok.Data;

/**
 * {@code POST /internal/agent/viewport} 的响应 —— 前端上报屏幕状态后的回执。
 *
 * <p>⚠️ **拒绝也是 HTTP 200 + 显式字段**，不用 4xx：和 {@code ToolEnvelope} 同一条原则,
 * 业务状态不做成 HTTP 错误。</p>
 */
@Data
public class ViewportAck {

    private Boolean accepted;

    /** {@code STALE_REV}（旧快照）或 {@code SESSION_GONE}（会话已过期）；接受时为 null。 */
    private String reason;

    /**
     * **永远回带当前 rev**（被拒时也带）。
     *
     * <p>没有它，前端被拒之后只会一直重发自己那个旧 rev，永远同步不上。</p>
     */
    private Integer rev;
}
