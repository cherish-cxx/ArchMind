package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.CreateProjectWithSourceResponse;
import com.example.archmind.dto.response.ProjectResponse;
import com.example.archmind.dto.response.UploadResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ProjectCreationService;
import com.example.archmind.service.ProjectService;
import com.example.archmind.service.UploadService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.util.concurrent.TimeUnit;

/**
 * 创建 + 上传的一体化实现：
 * <ul>
 *   <li>先校验文件与参数，避免建了项目再失败；</li>
 *   <li>用 Redis SETNX 幂等键防重复提交（失败时释放，允许重试）；</li>
 *   <li>上传任一步失败就删除整个项目（库 + 磁盘 + 缓存），不留半成品。</li>
 * </ul>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class ProjectCreationServiceImpl implements ProjectCreationService {

    private static final String IDEM_KEY_PREFIX = "idem:project:";
    private static final String IN_PROGRESS = "IN_PROGRESS";
    private static final long IDEM_TTL_MINUTES = 10;

    private final ProjectService projectService;
    private final UploadService uploadService;
    private final ProjectMapper projectMapper;
    private final RedisUtil redisUtil;

    @Override
    public CreateProjectWithSourceResponse createWithSource(Long userId, ProjectCreateRequest request,
                                                            MultipartFile file, String idempotencyKey) {
        if (userId == null) {
            throw new BusinessException("未获取到登录用户");
        }
        validateRequest(request);
        uploadService.validateZip(file);

        String key = idempotencyKey == null || idempotencyKey.isBlank()
                ? null
                : IDEM_KEY_PREFIX + userId + ":" + idempotencyKey.trim();

        if (key != null) {
            Boolean got = redisUtil.setIfAbsent(key, IN_PROGRESS, IDEM_TTL_MINUTES, TimeUnit.MINUTES);
            if (!Boolean.TRUE.equals(got)) {
                return replayOrReject(key, userId);
            }
        }

        ProjectResponse project = null;
        try {
            project = projectService.createProject(userId, request);
            UploadResponse upload = uploadService.upload(project.getId(), file);
            if (key != null) {
                redisUtil.set(key, String.valueOf(project.getId()),
                        IDEM_TTL_MINUTES, TimeUnit.MINUTES);
            }
            return new CreateProjectWithSourceResponse(project, upload);
        } catch (RuntimeException e) {
            // 回滚：项目是本次请求刚建的，失败时必须整体清掉，否则重试会留下多个同名项目
            if (project != null) {
                try {
                    projectService.deleteProjectData(project.getId());
                } catch (Exception cleanupError) {
                    log.error("创建项目失败后回滚不完整 projectId={}", project.getId(), cleanupError);
                }
            }
            if (key != null) {
                redisUtil.delete(key);
            }
            throw e;
        }
    }

    /** 同一幂等键：已完成则直接返回原项目；仍处理中则拒绝重复提交 */
    private CreateProjectWithSourceResponse replayOrReject(String key, Long userId) {
        Object value = redisUtil.get(key);
        if (value instanceof String stored && !IN_PROGRESS.equals(stored)) {
            try {
                Project project = projectMapper.selectById(Long.valueOf(stored));
                if (project != null && userId.equals(project.getUserId())) {
                    return new CreateProjectWithSourceResponse(toResponse(project), null);
                }
            } catch (NumberFormatException ignored) {
                // 落到下面统一拒绝
            }
        }
        throw new BusinessException("相同请求正在处理中，请勿重复提交");
    }

    private void validateRequest(ProjectCreateRequest request) {
        if (request == null || request.getName() == null || request.getName().isBlank()) {
            throw new BusinessException("项目名称不能为空");
        }
        String name = request.getName().trim();
        if (name.length() > 50) {
            throw new BusinessException("项目名称长度需在 1-50 之间");
        }
        if (request.getDescription() != null && request.getDescription().length() > 200) {
            throw new BusinessException("项目描述不能超过 200 字");
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
