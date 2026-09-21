package com.example.archmind.service;

import com.example.archmind.common.constant.AnalysisStage;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.dto.response.TaskResponse;

public interface TaskService {

    // ==================== 提交 ====================

    /**
     * 提交分析任务。
     * 非 force 且已有概况 → 不建任务，直接返回一个"已完成"的合成响应；
     * 否则抢锁 → 建 PENDING 任务 → 投递线程池 → 立即返回 taskId。
     */
    TaskResponse submitAnalysis(Long projectId, Long userId, boolean force);

    // ==================== 查询 ====================

    TaskResponse get(Long taskId, Long currentUserId);

    /** 项目最近一条任务；没有则返回 null（前端按"从未分析"处理） */
    TaskResponse getLatestByProject(Long projectId, Long currentUserId);


    // ==================== 状态机（只给 AnalysisTaskExecutor 调）====================

    void markRunning(Long taskId);

    /** 阶段切换：写 DB */
    void enterStage(Long taskId, AnalysisStage stage);

    /** 阶段内高频进度：只写 Redis */
    void reportProgress(Long taskId, AnalysisStage stage, int pctWithin);

    void markSuccess(Long taskId, ProjectOverviewResponse overview);

    void markFailed(Long taskId, String error);

    /** 释放项目级分析锁 */
    void releaseLock(Long projectId);
}
