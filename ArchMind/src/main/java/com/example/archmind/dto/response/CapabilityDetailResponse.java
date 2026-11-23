package com.example.archmind.dto.response;

import java.util.List;

/**
 * 能力详情：能力摘要 + 成员类（按 depth 排序）+ 成员之间的调用边（按类去重）
 * + 跨领域调用（按目标领域 uid 排序）。
 */
public record CapabilityDetailResponse(CapabilityDto capability, List<MemberDto> members,
                                       List<EdgeDto> edges, List<AreaCallDto> areaCalls) {
}
