package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;

/**
 * 已认证的 MCP 调用者身份。token 只绑定 userId。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpPrincipal {

    private Long patId;

    private Long userId;

    /** 预留：项目域 token 绑定的项目 */
    private Long boundProjectId;

    private LocalDateTime expiresAt;
}
