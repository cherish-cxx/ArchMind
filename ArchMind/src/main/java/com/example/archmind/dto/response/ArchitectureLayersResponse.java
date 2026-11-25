package com.example.archmind.dto.response;

import java.util.List;

/**
 * 系统架构分层视图：六层固定齐全（空层 classCount=0、groups=[]），
 * classCount 是全部类数（含未归类）。
 */
public record ArchitectureLayersResponse(List<ArchitectureLayerDto> layers,
                                         List<ArchitectureLayerEdgeDto> edges,
                                         int classCount) {
}
