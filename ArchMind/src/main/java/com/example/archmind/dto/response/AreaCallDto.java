package com.example.archmind.dto.response;

/**
 * 跨领域调用：本领域能力停在目标领域的次数。
 */
public record AreaCallDto(String toAreaUid, String toAreaName, int count) {
}
