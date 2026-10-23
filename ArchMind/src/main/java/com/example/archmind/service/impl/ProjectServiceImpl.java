package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.archmind.common.constant.ProjectAnalysisStatus;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.config.UploadProperties;
import com.example.archmind.dao.FileEntityMapper;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dao.ProjectOverviewMapper;
import com.example.archmind.dao.ProjectSourceMapper;
import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.ProjectResponse;
import com.example.archmind.entity.FileEntity;
import com.example.archmind.entity.Project;
import com.example.archmind.entity.ProjectOverview;
import com.example.archmind.entity.ProjectSource;
import com.example.archmind.service.ProjectService;
import com.example.archmind.service.agent.AgentClient;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.LocalDateTime;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

@Slf4j
@Service
@RequiredArgsConstructor
public class ProjectServiceImpl implements ProjectService {

    private final ProjectMapper projectMapper;
    private final ProjectSourceMapper projectSourceMapper;
    private final FileEntityMapper fileEntityMapper;
    private final ProjectOverviewMapper projectOverviewMapper;
    private final UploadProperties uploadProperties;
    private final CheckProjectUtil checkProjectUtil;
    private final RedisUtil redisUtil;
    private final AgentClient agentClient;

    @Override
    public ProjectResponse createProject(Long userId, ProjectCreateRequest request) {
        if (userId == null) {
            throw new BusinessException("未获取到登录用户");
        }
        Project project = new Project();
        project.setUserId(userId);
        project.setName(request.getName().trim());
        project.setDescription(request.getDescription());
        project.setGitUrl(request.getGitUrl());
        project.setStatus("CREATED");
        project.setAnalysisStatus(ProjectAnalysisStatus.NO_SOURCE);
        project.setCreateTime(LocalDateTime.now());
        projectMapper.insert(project);
        return toResponse(project);
    }

    @Override
    public List<ProjectResponse> listMyProjects(Long userId) {
        if (userId == null) {
            throw new BusinessException("未获取到登录用户");
        }
        LambdaQueryWrapper<Project> wrapper = new LambdaQueryWrapper<>();
        wrapper.eq(Project::getUserId, userId)
                .orderByDesc(Project::getCreateTime);
        return projectMapper.selectList(wrapper).stream()
                .map(this::toResponse)
                .collect(Collectors.toList());
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public void deleteProject(Long projectId) {
        checkProjectUtil.checkProject(projectId);
        deleteProjectData(projectId);
    }

    @Override
    public void deleteProjectData(Long projectId) {
        if (projectId == null) {
            throw new BusinessException("项目ID为空");
        }
        // 先删子表再删 project，避免服务器建表若带外键时的约束冲突
        projectSourceMapper.delete(new LambdaQueryWrapper<ProjectSource>()
                .eq(ProjectSource::getProjectId, projectId));
        fileEntityMapper.delete(new LambdaQueryWrapper<FileEntity>()
                .eq(FileEntity::getProjectId, projectId));
        projectOverviewMapper.delete(new LambdaQueryWrapper<ProjectOverview>()
                .eq(ProjectOverview::getProjectId, projectId));
        projectMapper.deleteById(projectId);

        // 磁盘删除 {baseDir}/{projectId}（含 zip 与解压源码）；失败仅告警，不影响库表删除结果，最多留孤儿目录
        deleteDirectory(Paths.get(uploadProperties.getBaseDir())
                .resolve(String.valueOf(projectId))
                .toAbsolutePath()
                .normalize());

        // 清理该项目 overview 的 Redis 读缓存（key 不存在时无害）
        redisUtil.delete(ProjectOverviewServiceImpl.OVERVIEW_CACHE_KEY_PREFIX + projectId);

        // P4.3：清掉 Agent 侧该项目的全部会话 Context（在 Python 的 Redis db1 里）。
        // 与上面的删磁盘是**同一类**副作用：尽力而为，失败只告警，不影响删除结果 ——
        // 留一点孤儿 Context，远比「因为 Python 挂了就删不掉项目」可接受。
        // 走 AgentClient 的**短超时**实例（3s），不会把删除操作拖住。
        try {
            agentClient.evictProject(projectId);
        } catch (Exception e) {
            log.warn("清理 Agent 会话 Context 失败 projectId={}: {}", projectId, e.getMessage());
        }
    }

    private void deleteDirectory(Path dir) {
        if (dir == null || !Files.exists(dir)) {
            return;
        }
        try (Stream<Path> paths = Files.walk(dir)) {
            paths.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException e) {
                    log.warn("删除文件失败: {}", p);
                }
            });
        } catch (IOException e) {
            log.error("清理项目工作区目录失败, dir={}", dir, e);
        }
    }

    private ProjectResponse toResponse(Project project) {
        return ProjectResponse.builder()
                .id(project.getId())
                .name(project.getName())
                .description(project.getDescription())
                .language(project.getLanguage())
                .framework(project.getFramework())
                .gitUrl(project.getGitUrl())
                .version(project.getVersion())
                .status(project.getStatus())
                .analysisStatus(project.getAnalysisStatus())
                .createTime(project.getCreateTime())
                .build();
    }
}
