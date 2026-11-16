package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 项目说明（AGENTS.md / CLAUDE.md 草稿）导出结果。
 *
 * <p>只含 projectId，不含 userId 或任何 token —— brief 会被粘贴进用户仓库，
 * 不能携带身份密钥。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class BriefResponse {
    private String filename;
    private String content;
    private long graphRev;
}
