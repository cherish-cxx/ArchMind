package com.example.archmind.common.util;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.security.SecurityUser;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.entity.Project;
import lombok.RequiredArgsConstructor;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

@Component
@RequiredArgsConstructor
public class CheckProjectUtil {

    private final ProjectMapper projectMapper;

    public void checkProject(Long projectId) {
        if (projectId == null) {
            throw new BusinessException("项目ID为空");
        }
        Project project = projectMapper.selectById(projectId);
        if (project == null) {
            throw new BusinessException("项目不存在: " + projectId);
        }
        checkOwnership(project);
    }

    /**
     * 内部接口专用：只确认项目存在，**不校验归属**。
     *
     * <p>{@code /internal/**} 没有登录态（无 JWT），拿不到当前用户，因此不能走
     * {@link #checkProject}。租户边界不靠这里守 —— 靠「projectId 只由 Java 注入、
     * Agent 不能指定」，且所有 Cypher 的 pid 由后端拼。</p>
     */
    public void checkProjectExists(Long projectId) {
        if (projectId == null) {
            throw new BusinessException("项目ID为空");
        }
        if (projectMapper.selectById(projectId) == null) {
            throw new BusinessException("项目不存在: " + projectId);
        }
    }

    public Long getCurrentUserId() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication != null && authentication.getPrincipal() instanceof SecurityUser securityUser) {
            return securityUser.getUserId();
        }
        return null;
    }

    private void checkOwnership(Project project) {
        Long userId = getCurrentUserId();
        if (userId != null && project.getUserId() != null && !userId.equals(project.getUserId())) {
            throw new BusinessException("无权操作该项目");
        }
    }
}
