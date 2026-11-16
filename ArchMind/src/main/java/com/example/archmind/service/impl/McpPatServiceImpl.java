package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.exception.McpAuthException;
import com.example.archmind.dao.McpPatMapper;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.request.McpPatCreateRequest;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPatCreatedResponse;
import com.example.archmind.dto.response.McpPatView;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.dto.response.McpProjectItem;
import com.example.archmind.entity.McpPat;
import com.example.archmind.entity.Project;
import com.example.archmind.service.McpPatService;
import com.example.archmind.service.ast.GraphRevService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.LocalDateTime;
import java.util.Base64;
import java.util.List;

@Slf4j
@Service
@RequiredArgsConstructor
public class McpPatServiceImpl implements McpPatService {

    private static final String TOKEN_PREFIX = "am_pat_";
    private static final int TOKEN_BYTES = 32;
    private static final int PREFIX_LENGTH = 15;
    private static final int DEFAULT_EXPIRES_DAYS = 90;
    private static final int MIN_EXPIRES_DAYS = 1;
    private static final int MAX_EXPIRES_DAYS = 365;

    private final McpPatMapper patMapper;
    private final ProjectMapper projectMapper;
    private final GraphRevService graphRevService;

    private final SecureRandom secureRandom = new SecureRandom();

    @Override
    public McpPatCreatedResponse create(Long userId, McpPatCreateRequest req) {
        int expiresInDays = req.getExpiresInDays() == null
                ? DEFAULT_EXPIRES_DAYS : req.getExpiresInDays();
        if (expiresInDays < MIN_EXPIRES_DAYS || expiresInDays > MAX_EXPIRES_DAYS) {
            throw new BusinessException("有效期必须在 " + MIN_EXPIRES_DAYS + ".." + MAX_EXPIRES_DAYS + " 天之间");
        }

        byte[] randomBytes = new byte[TOKEN_BYTES];
        secureRandom.nextBytes(randomBytes);
        String token = TOKEN_PREFIX + Base64.getUrlEncoder().withoutPadding().encodeToString(randomBytes);

        LocalDateTime now = LocalDateTime.now();
        McpPat pat = new McpPat();
        pat.setUserId(userId);
        pat.setName(req.getName());
        pat.setTokenHash(sha256Hex(token));
        pat.setPrefix(token.substring(0, PREFIX_LENGTH));
        pat.setCreateTime(now);
        pat.setExpiresAt(now.plusDays(expiresInDays));

        patMapper.insert(pat);

        return McpPatCreatedResponse.builder()
                .id(pat.getId())
                .name(pat.getName())
                .token(token)
                .prefix(pat.getPrefix())
                .createTime(pat.getCreateTime())
                .expiresAt(pat.getExpiresAt())
                .build();
    }

    @Override
    public List<McpPatView> list(Long userId) {
        List<McpPat> pats = patMapper.selectList(new LambdaQueryWrapper<McpPat>()
                .eq(McpPat::getUserId, userId)
                .orderByDesc(McpPat::getCreateTime));
        return pats.stream()
                .map(p -> McpPatView.builder()
                        .id(p.getId())
                        .name(p.getName())
                        .prefix(p.getPrefix())
                        .createTime(p.getCreateTime())
                        .expiresAt(p.getExpiresAt())
                        .lastUsedAt(p.getLastUsedAt())
                        .revokedAt(p.getRevokedAt())
                        .build())
                .toList();
    }

    @Override
    public void revoke(Long userId, Long patId) {
        McpPat pat = patMapper.selectById(patId);
        if (pat == null || !userId.equals(pat.getUserId())) {
            throw new BusinessException("token 不存在");
        }
        pat.setRevokedAt(LocalDateTime.now());
        patMapper.updateById(pat);
    }

    @Override
    public McpPrincipal authenticate(String rawToken) {
        McpPat pat = patMapper.selectOne(new LambdaQueryWrapper<McpPat>()
                .eq(McpPat::getTokenHash, sha256Hex(rawToken)));
        if (pat == null) {
            throw new McpAuthException(401, "无效 token");
        }
        if (pat.getRevokedAt() != null) {
            throw new McpAuthException(401, "token 已吊销");
        }
        LocalDateTime now = LocalDateTime.now();
        if (pat.getExpiresAt() != null && pat.getExpiresAt().isBefore(now)) {
            throw new McpAuthException(401, "token 已过期");
        }

        pat.setLastUsedAt(now);
        patMapper.updateById(pat);

        return McpPrincipal.builder()
                .patId(pat.getId())
                .userId(pat.getUserId())
                .boundProjectId(pat.getProjectId())
                .expiresAt(pat.getExpiresAt())
                .build();
    }

    @Override
    public McpContext context(McpPrincipal principal, Long projectId) {
        Project project = projectMapper.selectById(projectId);
        if (project == null) {
            throw new McpAuthException(404, "项目不存在");
        }
        if (principal.getUserId() == null || !principal.getUserId().equals(project.getUserId())) {
            throw new McpAuthException(403, "无权访问该项目");
        }
        if (principal.getBoundProjectId() != null
                && !principal.getBoundProjectId().equals(projectId)) {
            throw new McpAuthException(403, "token 未绑定该项目");
        }

        return McpContext.builder()
                .userId(principal.getUserId())
                .patId(principal.getPatId())
                .projectId(project.getId())
                .projectName(project.getName())
                .analysisStatus(project.getAnalysisStatus())
                .graphRev(graphRevService.currentRev(projectId))
                .build();
    }

    @Override
    public List<McpProjectItem> projects(Long userId) {
        List<Project> projects = projectMapper.selectList(new LambdaQueryWrapper<Project>()
                .eq(Project::getUserId, userId)
                .orderByDesc(Project::getCreateTime));
        return projects.stream()
                .map(p -> McpProjectItem.builder()
                        .projectId(p.getId())
                        .name(p.getName())
                        .analysisStatus(p.getAnalysisStatus())
                        .graphRev(graphRevService.currentRev(p.getId()))
                        .build())
                .toList();
    }

    private static String sha256Hex(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(hash.length * 2);
            for (byte b : hash) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 不可用", e);
        }
    }
}
