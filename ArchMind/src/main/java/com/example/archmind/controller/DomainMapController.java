package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.response.CapabilityDetailResponse;
import com.example.archmind.dto.response.DomainMapResponse;
import com.example.archmind.service.domain.DomainQueryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

/**
 * 认知层：领域地图与能力详情。读 DOMAIN 阶段落图的业务领域与能力。
 */
@RestController
@RequestMapping("/api/project")
@RequiredArgsConstructor
public class DomainMapController {

    private final DomainQueryService domainQueryService;
    private final CheckProjectUtil checkProjectUtil;

    /** 项目领域地图：领域 → 能力；无能力的领域（如基础设施）也会返回 */
    @GetMapping("/{projectId}/domain-map")
    public Result<DomainMapResponse> domainMap(@PathVariable Long projectId) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(domainQueryService.domainMap(projectId));
    }

    /** 点某个能力 → 成员类与跨领域调用。uid 带空格与斜杠，放 query param */
    @GetMapping("/{projectId}/capability")
    public Result<CapabilityDetailResponse> capability(@PathVariable Long projectId,
                                                       @RequestParam String uid) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(domainQueryService.capability(projectId, uid));
    }
}
