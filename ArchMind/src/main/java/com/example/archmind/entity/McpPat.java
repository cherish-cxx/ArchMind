package com.example.archmind.entity;

import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * MCP 个人访问令牌。只存 SHA-256 摘要，明文仅在创建响应中出现一次。
 */
@Data
@TableName("mcp_pat")
public class McpPat {

    @TableId
    private Long id;

    private Long userId;

    /** 预留：项目域 token；为空表示用户级 token */
    private Long projectId;

    private String name;

    private String tokenHash;

    /** token 明文前 15 位，用于列表识别 */
    private String prefix;

    private LocalDateTime createTime;

    private LocalDateTime expiresAt;

    private LocalDateTime lastUsedAt;

    private LocalDateTime revokedAt;
}
