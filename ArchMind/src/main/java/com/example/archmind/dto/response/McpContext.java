package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 一次 MCP 调用的项目上下文：鉴权与归属校验通过后返回。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpContext {

    private Long userId;

    private Long patId;

    private Long projectId;

    private String projectName;

    private String analysisStatus;

    private Long graphRev;
}
