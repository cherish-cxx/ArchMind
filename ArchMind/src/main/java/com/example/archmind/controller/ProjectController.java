package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.CreateProjectWithSourceResponse;
import com.example.archmind.dto.response.ProjectResponse;
import com.example.archmind.service.ProjectCreationService;
import com.example.archmind.service.ProjectService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

@RestController
@RequestMapping("/api/project")
@RequiredArgsConstructor
public class ProjectController {

    private final ProjectService projectService;
    private final ProjectCreationService projectCreationService;
    private final CheckProjectUtil checkProjectUtil;

    @PostMapping
    public Result<ProjectResponse> create(@Valid @RequestBody ProjectCreateRequest request) {
        return Result.success(projectService.createProject(checkProjectUtil.getCurrentUserId(), request));
    }

    /**
     * 创建并上传源码：一个请求完成建项目 + 落盘 + 解压 + 扫描，
     * 失败整体回滚；Idempotency-Key 让失败重试不会产生重复项目。
     */
    @PostMapping(value = "/with-source", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Result<CreateProjectWithSourceResponse> createWithSource(
            @RequestParam String name,
            @RequestParam(required = false) String description,
            @RequestParam(required = false) String gitUrl,
            @RequestParam("file") MultipartFile file,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {
        ProjectCreateRequest request = new ProjectCreateRequest();
        request.setName(name);
        request.setDescription(description);
        request.setGitUrl(gitUrl);
        Long userId = checkProjectUtil.getCurrentUserId();
        return Result.success(projectCreationService.createWithSource(userId, request, file, idempotencyKey));
    }

    @GetMapping
    public Result<List<ProjectResponse>> myProjects() {
        return Result.success(projectService.listMyProjects(checkProjectUtil.getCurrentUserId()));
    }
    @DeleteMapping("/{projectId}")
    public Result<Void> deleteProject(@PathVariable Long projectId) {
        projectService.deleteProject(projectId);
        return Result.success();
    }
}
