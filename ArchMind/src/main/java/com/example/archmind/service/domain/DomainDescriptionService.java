package com.example.archmind.service.domain;

import com.example.archmind.service.ProgressReporter;

/**
 * 领域描述阶段：结构定型之后，读业务领域 / 能力 / 成员类已有的类描述，
 * 批量让模型写出领域与能力的中文说明。只消费图上的描述，不重读源码。
 */
public interface DomainDescriptionService {

    /**
     * 读领域与能力 → 按领域分批拼提示词 → 调模型 → 只写回库里已知的 uid。
     *
     * @param projectId 项目 id
     * @param reporter  阶段内进度回调（0/50/100）
     */
    void describe(Long projectId, ProgressReporter reporter);
}
