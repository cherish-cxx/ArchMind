package com.example.archmind.dto.response;

import java.util.List;

/**
 * 层内的一个分组：入口/业务/领域模型按业务领域分组（key=areaUid），
 * 基础设施/外部/未归类按包名尾段分组（key=尾段，detail=完整包名）。
 * classCount 是组内全部类数；classes 最多 maxClassesPerGroup 个，被截断时 truncated=true。
 */
public record ArchitectureGroupDto(String key, String name, String detail, int classCount,
                                   List<ArchitectureClassDto> classes, boolean truncated) {
}
