package com.example.archmind.dto.response;

import java.util.List;

/**
 * 跨领域调用：本领域能力停在目标领域的次数。
 * fromClassUids 是发出该调用的来源类（按领域对聚合，可能有多个）；
 * 前端取它与当前能力成员的交集来连出口线。
 */
public record AreaCallDto(String toAreaUid, String toAreaName, int count, List<String> fromClassUids) {
}
