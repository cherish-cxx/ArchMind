package com.example.archmind.service;

/**
 * 核心类简要描述：从 Neo4j 图挑出核心类 → LLM 批量生成一句话简介 → 写回节点 description 属性。
 */
public interface ClassDescriptionService {

    /**
     * 对某项目核心类生成简要描述并写回 Neo4j。
     * 幂等：每次重跑重新生成覆盖（图由 astStage 清空重建，描述在其后重写）。
     *
     * @param projectId 项目 id
     * @param reporter  进度回调（0~100，阶段内）
     */
    void describeCoreClasses(Long projectId, ProgressReporter reporter);
}
