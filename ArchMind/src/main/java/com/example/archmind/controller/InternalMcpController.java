package com.example.archmind.controller;

import com.example.archmind.common.exception.McpAuthException;
import com.example.archmind.common.result.Result;
import com.example.archmind.dto.request.McpContextRequest;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.dto.response.McpProjectItem;
import com.example.archmind.service.McpPatService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 内部 MCP 鉴权与项目列表接口（{@code /internal/mcp/*}，由 {@code InternalTokenFilter} 保护）。
 *
 * <p>{@code context} 用 PAT 认证并把 {@code projectId} 当作不可信输入：认证/归属校验失败
 * 直接映射为对应的 HTTP 状态码（401/403/404），成功时原样返回 {@link McpContext}。</p>
 */
@RestController
@RequestMapping("/internal/mcp")
@RequiredArgsConstructor
public class InternalMcpController {

    private final McpPatService mcpPatService;

    @PostMapping("/context")
    public ResponseEntity<?> context(@RequestBody McpContextRequest request) {
        String token = request.getToken();
        if (token == null || token.isBlank()) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Result.fail(HttpStatus.UNAUTHORIZED.value(), "缺少 token"));
        }
        try {
            McpPrincipal principal = mcpPatService.authenticate(token);
            return ResponseEntity.ok(mcpPatService.context(principal, request.getProjectId()));
        } catch (McpAuthException e) {
            return ResponseEntity.status(e.getStatus())
                    .body(Result.fail(e.getStatus(), e.getMessage()));
        }
    }

    @GetMapping("/projects")
    public Result<List<McpProjectItem>> projects(@RequestParam Long userId) {
        return Result.success(mcpPatService.projects(userId));
    }
}
