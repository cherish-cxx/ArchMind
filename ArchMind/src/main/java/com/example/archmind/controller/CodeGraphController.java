package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;
import com.example.archmind.service.ast.CodeGraphQueryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;

/**
 * 代码探索：类清单 + 点类看关联。读 Neo4j 图，仅对已分析（已落图）的项目有意义。
 */
@RestController
@RequestMapping("/api/project")
@RequiredArgsConstructor
public class CodeGraphController {

    private final CodeGraphQueryService codeGraphQueryService;
    private final CheckProjectUtil checkProjectUtil;

    /** 项目全部类清单，前端「代码探索」的入口 */
    @GetMapping("/{projectId}/classes")
    public Result<List<ClassNodeDTO>> listClasses(@PathVariable Long projectId) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(codeGraphQueryService.listClasses(projectId));
    }

    /** 点某个类 → 查它的上下游关联（类级聚合）。uid = 类全限定名，放 query param 避免路径里的点号问题 */
    @GetMapping("/{projectId}/class/relations")
    public Result<ClassRelationsResponse> classRelations(@PathVariable Long projectId,
                                                        @RequestParam String uid) {
        checkProjectUtil.checkProject(projectId);
        return Result.success(codeGraphQueryService.classRelations(projectId, uid));
    }
}
