package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.request.McpPatCreateRequest;
import com.example.archmind.dto.response.McpPatCreatedResponse;
import com.example.archmind.dto.response.McpPatView;
import com.example.archmind.service.McpPatService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * MCP 个人访问令牌管理接口（JWT 保护）。
 *
 * <p>用户身份一律从 {@link CheckProjectUtil#getCurrentUserId()} 取，不接受请求体里的 userId。
 * 明文 token 只在创建响应里出现一次，列表只返回前缀。</p>
 */
@RestController
@RequestMapping("/api/mcp/tokens")
@RequiredArgsConstructor
public class McpPatController {

    private final McpPatService mcpPatService;
    private final CheckProjectUtil checkProjectUtil;

    @PostMapping
    public Result<McpPatCreatedResponse> create(@Valid @RequestBody McpPatCreateRequest request) {
        return Result.success(mcpPatService.create(checkProjectUtil.getCurrentUserId(), request));
    }

    @GetMapping
    public Result<List<McpPatView>> list() {
        return Result.success(mcpPatService.list(checkProjectUtil.getCurrentUserId()));
    }

    @DeleteMapping("/{patId}")
    public Result<Void> revoke(@PathVariable Long patId) {
        mcpPatService.revoke(checkProjectUtil.getCurrentUserId(), patId);
        return Result.success();
    }
}
