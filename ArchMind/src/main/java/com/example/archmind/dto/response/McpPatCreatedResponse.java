package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;

/**
 * 创建响应：{@code token} 明文只在这里出现一次。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpPatCreatedResponse {

    private Long id;

    private String name;

    private String token;

    private String prefix;

    private LocalDateTime createTime;

    private LocalDateTime expiresAt;
}
