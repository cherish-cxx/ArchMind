package com.example.archmind.service;

import com.example.archmind.dto.response.ClassDescriptionsResponse;

import java.util.List;

/**
 * 类简要描述：分析阶段批量生成核心类；运行期按画布内容按需补齐其余实现类。
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

    /**
     * 按需生成：只处理「还没有描述」的类。
     *
     * <p>默认只覆盖非抽象的实现类（kind=CLASS）；{@code force=true} 时放宽类型，
     * 但仍跳过已有描述的类。项目级串行 + 失败冷却，避免并发重复调用 LLM。</p>
     *
     * @param projectId 项目 id
     * @param uids      候选类 uid（顺序会被保留，单次最多处理 40 个）
     * @param force     是否放宽类型限制（中心节点用）
     */
    ClassDescriptionsResponse describeClasses(Long projectId, List<String> uids, boolean force);
}
