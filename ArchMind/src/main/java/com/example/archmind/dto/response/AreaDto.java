package com.example.archmind.dto.response;

import java.util.List;

/**
 * 领域地图中的一个业务领域。classCount 是 BELONGS_TO 该领域的类数，
 * 不能用各能力的 memberCount 相加 —— 一个类可同时属于多个能力。
 */
public record AreaDto(String uid, String name, String kind, String description, int classCount,
                      List<CapabilityDto> capabilities) {
}
