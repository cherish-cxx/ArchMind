package com.example.archmind.service;

import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.CreateProjectWithSourceResponse;
import org.springframework.web.multipart.MultipartFile;

/**
 * 创建项目 + 上传源码的一体化入口：两者要么都成功，要么都不留痕。
 */
public interface ProjectCreationService {

    /**
     * @param idempotencyKey 前端同一次提交生成并复用的键；为空时不做幂等保护
     */
    CreateProjectWithSourceResponse createWithSource(Long userId, ProjectCreateRequest request,
                                                     MultipartFile file, String idempotencyKey);
}
