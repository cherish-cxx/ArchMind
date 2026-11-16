package com.example.archmind.common.exception;

import lombok.Getter;

/**
 * MCP 鉴权/归属校验失败，携带 HTTP 状态码（401/403/404）。
 */
@Getter
public class McpAuthException extends RuntimeException {

    private final int status;

    public McpAuthException(int status, String message) {
        super(message);
        this.status = status;
    }
}
