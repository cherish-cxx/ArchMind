package com.example.archmind.controller;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.exception.McpAuthException;
import com.example.archmind.common.result.Result;
import com.example.archmind.dto.request.McpContextRequest;
import com.example.archmind.dto.response.BriefResponse;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.dto.response.McpProjectItem;
import com.example.archmind.dto.response.ToolEnvelope;
import com.example.archmind.service.McpPatService;
import com.example.archmind.service.ProjectBriefService;
import com.example.archmind.service.ast.GraphRevService;
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
    private final ProjectBriefService projectBriefService;
    private final GraphRevService graphRevService;

    private static final String AUTH_FAILED_MESSAGE = "认证失败";

    /**
     * 纯 token 校验（不涉及 projectId），供 Python PAT 中间件换取调用者身份。
     *
     * <p>成功时返回裸 {@link McpPrincipal}（userId / patId / boundProjectId / expiresAt），
     * 失败一律 401 + 通用 {@link Result}，不回显 token。</p>
     */
    @PostMapping("/authenticate")
    public ResponseEntity<?> authenticate(@RequestBody(required = false) McpContextRequest request) {
        String token = request == null ? null : request.getToken();
        if (token == null || token.isBlank()) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                    .body(Result.fail(HttpStatus.UNAUTHORIZED.value(), "缺少 token"));
        }
        try {
            McpPrincipal principal = mcpPatService.authenticate(token);
            return ResponseEntity.ok(principal);
        } catch (McpAuthException e) {
            return ResponseEntity.status(e.getStatus())
                    .body(Result.fail(e.getStatus(), AUTH_FAILED_MESSAGE));
        }
    }

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

    /**
     * MCP 的 {@code archmind_project_brief} 工具数据源。返回裸 {@link ToolEnvelope}（HTTP 200），
     * 不包 {@link Result}，供 Python JavaClient 直接解析。
     */
    @GetMapping("/brief")
    public ToolEnvelope<BriefResponse> brief(@RequestParam Long projectId,
                                             @RequestParam(defaultValue = "claude") String target) {
        try {
            BriefResponse res = projectBriefService.render(projectId, target);
            return ToolEnvelope.success(res).withGraphRev(res.getGraphRev());
        } catch (BusinessException e) {
            // 全局异常处理器返回 Result 包装，Python 的 ToolEnvelope 解析器读不了；
            // 这里显式转成 200 + ok=false 的信封。
            return ToolEnvelope.<BriefResponse>failure(e.getMessage())
                    .withGraphRev(graphRevService.currentRev(projectId));
        }
    }
}
