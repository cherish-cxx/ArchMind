package com.example.archmind.dto.response;

import java.util.List;

/**
 * 一个架构层。key 为 entry/business/domain-model/infrastructure/external/other；
 * classCount 是该层全部类数，groups 已按类数降序。
 */
public record ArchitectureLayerDto(String key, String name, int classCount,
                                   List<ArchitectureGroupDto> groups) {
}
