package com.example.archmind.service;

import com.example.archmind.dto.response.ProjectOverviewResponse;

/**
 * 分析流水线的三个阶段：只负责"每个阶段怎么算"，不关心任务状态、线程与成败。
 */
public interface AnalysisPipelineService {

    /** 阶段一：文件扫描（P1：上传时已扫描入库，此处为占位/校验） */
    void scanStage(Long projectId, ProgressReporter reporter);

    /** 阶段二：AST 提取（定位源码 → JavaParser 解析 → 写入 Neo4j 图） */
    void astStage(Long projectId, ProgressReporter reporter);

    /** 阶段三：概况生成（读 pom/README → 调 LLM → 落库） */
    ProjectOverviewResponse overviewStage(Long projectId, ProgressReporter reporter);
}
