package com.example.archmind.dto.response;

/**
 * 能力摘要：uid 与入口 uid 相同，memberCount 是成员类数量，description 由领域描述阶段生成。
 */
public record CapabilityDto(String uid, String name, String description, String entryUid, int memberCount) {
}
