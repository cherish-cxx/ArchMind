package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.response.ArchitectureLayersResponse;
import com.example.archmind.service.domain.ArchitectureQueryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 认知层：系统架构分层视图。按类角色投影成六层，只读聚合，不改图数据。
 */
@RestController
@RequestMapping("/api/project")
@RequiredArgsConstructor
public class ArchitectureController {

    private final ArchitectureQueryService architectureQueryService;
    private final CheckProjectUtil checkProjectUtil;

    /** 项目分层视图：六层固定齐全，边只含跨层调用。 */
    @GetMapping("/{projectId}/architecture-layers")
    public Result<ArchitectureLayersResponse> layers(@PathVariable Long projectId) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(architectureQueryService.architecture(projectId));
    }
}
