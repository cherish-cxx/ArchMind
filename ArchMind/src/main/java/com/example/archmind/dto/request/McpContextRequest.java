package com.example.archmind.dto.request;

import lombok.Data;

/**
 * 内部 MCP 上下文请求。
 *
 * <p>{@code token} 用于 PAT 认证；{@code projectId} 是不可信输入，归属校验在服务层完成。</p>
 */
@Data
public class McpContextRequest {

    private String token;

    private Long projectId;
}
