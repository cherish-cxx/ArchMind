package com.example.archmind.dto.response;

/**
 * 跨层边上的一个类对样本：fromUid 调用 toUid 共 count 次。
 */
public record ArchitectureEdgeSampleDto(String fromUid, String toUid, int count) {
}
