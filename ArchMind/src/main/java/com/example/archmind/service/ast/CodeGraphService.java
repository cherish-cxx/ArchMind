package com.example.archmind.service.ast;

import com.example.archmind.service.ProgressReporter;

/**
 * 代码图谱服务：定位项目源码 → 筛选 Java 文件 → 解析 → 写入 Neo4j。
 * 供 AnalysisPipelineService.astStage 调用。
 */
public interface CodeGraphService {

    /**
     * 重建某项目的代码图：清旧图，重新解析全部 Java 源码并写回 Neo4j。
     * 按文件数线性上报阶段内进度（0~100）。幂等：重跑先清后写，节点数不翻倍。
     *
     * @param projectId 项目 id
     * @param reporter  进度回调（由调用方决定写到哪里）
     */
    void rebuild(Long projectId, ProgressReporter reporter);
}
