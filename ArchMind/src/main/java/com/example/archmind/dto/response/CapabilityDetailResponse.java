package com.example.archmind.dto.response;

import java.util.List;

/**
 * 能力详情：能力摘要 + 成员类（按 depth 排序）+ 跨领域调用（按目标领域 uid 排序）。
 */
public record CapabilityDetailResponse(CapabilityDto capability, List<MemberDto> members,
                                       List<AreaCallDto> areaCalls) {
}
