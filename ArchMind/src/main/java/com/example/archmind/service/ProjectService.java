package com.example.archmind.service;

import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.ProjectResponse;

import java.util.List;

public interface ProjectService {

    ProjectResponse createProject(Long userId, ProjectCreateRequest request);

    List<ProjectResponse> listMyProjects(Long userId);

    void deleteProject(Long projectId);

    /**
     * 清理项目全部数据（库 + 磁盘 + 缓存 + Agent Context），不做归属校验。
     * 供删除项目与「创建+上传失败」的补偿回滚复用。
     */
    void deleteProjectData(Long projectId);
}
