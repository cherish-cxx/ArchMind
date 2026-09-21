package com.example.archmind.dto.request;

import lombok.Data;

import java.util.HashMap;
import java.util.Map;

/**
 * {@code /internal/tools/*} 的统一入参：{@code {projectId, args:{...}}}。
 *
 * <p>{@code projectId} 由 Java 侧注入（Agent 转发时带下来），**不是用户身份信息** ——
 * 内部接口不接收任何 JWT / userId，租户边界靠「projectId 只由 Java 决定」来守。</p>
 */
@Data
public class InternalToolRequest {

    private Long projectId;

    /** 工具专属参数，如 limit / direction / uid */
    private Map<String, Object> args = new HashMap<>();
}
