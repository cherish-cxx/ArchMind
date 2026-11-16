package com.example.archmind.service;

import com.example.archmind.dto.request.McpPatCreateRequest;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPatCreatedResponse;
import com.example.archmind.dto.response.McpPatView;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.dto.response.McpProjectItem;

import java.util.List;

/**
 * MCP 个人访问令牌：创建、列表、吊销、认证与项目归属上下文校验。
 */
public interface McpPatService {

    McpPatCreatedResponse create(Long userId, McpPatCreateRequest req);

    List<McpPatView> list(Long userId);

    void revoke(Long userId, Long patId);

    McpPrincipal authenticate(String rawToken);

    McpContext context(McpPrincipal principal, Long projectId);

    List<McpProjectItem> projects(Long userId);
}
