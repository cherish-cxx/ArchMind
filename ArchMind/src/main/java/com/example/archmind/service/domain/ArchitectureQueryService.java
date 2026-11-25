package com.example.archmind.service.domain;

import com.example.archmind.dto.response.ArchitectureLayersResponse;

/**
 * 系统架构分层查询：只读聚合类角色、业务领域与跨层调用，不改图数据。
 */
public interface ArchitectureQueryService {

    /** 项目分层视图：六层固定齐全，边只含跨层调用。 */
    ArchitectureLayersResponse architecture(Long projectId);
}
