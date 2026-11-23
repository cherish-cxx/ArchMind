package com.example.archmind.service.domain;

import com.example.archmind.service.ProgressReporter;

/**
 * 业务建模阶段：把代码图升级成「业务领域 → 能力 → 代码」三级结构。
 * 全部由规则与调用图算出，不调模型；对账失败时抛错且不写库。
 */
public interface DomainModelService {

    /**
     * 读快照 → role 分类 → 业务领域 → 能力 → 对账 → 写回派生节点与边。
     *
     * @param projectId 项目 id
     * @param reporter  阶段内进度回调（0/20/40/60/80/95/100）
     */
    void build(Long projectId, ProgressReporter reporter);
}
