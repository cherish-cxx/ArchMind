package com.example.archmind.dto.response;

import java.util.List;

/**
 * 跨层调用边：fromLayer → toLayer 的调用次数合计，样本是被聚合的具体类对。
 * 同层调用不会出现在这里。
 */
public record ArchitectureLayerEdgeDto(String fromLayer, String toLayer, int count,
                                       List<ArchitectureEdgeSampleDto> samples) {
}
