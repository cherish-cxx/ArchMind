package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;

/**
 * 列表视图：不含明文 token，只有前缀。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpPatView {

    private Long id;

    private String name;

    private String prefix;

    private LocalDateTime createTime;

    private LocalDateTime expiresAt;

    private LocalDateTime lastUsedAt;

    private LocalDateTime revokedAt;
}
