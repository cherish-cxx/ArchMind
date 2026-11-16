package com.example.archmind.service;

import com.example.archmind.dto.response.BriefResponse;

public interface ProjectBriefService {

    /**
     * 渲染项目说明草稿。
     *
     * @param projectId 项目 id
     * @param target    {@code claude} 或 {@code agents}
     * @throws com.example.archmind.common.exception.BusinessException 目标未知或项目尚未生成概况
     */
    BriefResponse render(Long projectId, String target);
}
