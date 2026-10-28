package com.example.archmind.service;

import com.example.archmind.dto.response.ClassDescriptionsResponse;
import com.example.archmind.dto.response.DescribeBatchResult;
import com.example.archmind.dto.response.DescriptionStatusResponse;

import java.util.List;

/**
 * 类简要描述：分析阶段批量生成核心类；运行期按画布内容按需补齐其余实现类；
 * R4 阶段按批扫描「描述为空」的类，直到覆盖全量。
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

    /**
     * 全量补齐的一批：服务端自己挑「描述为空」的类，调用一次 LLM 并写回。
     *
     * <p>幂等键是「类有没有描述」，不是批次记录：重复调用同一批，已写过的类
     * 不会再被选中。项目串行由 Redis 短锁保证。</p>
     *
     * @param projectId 项目 id
     * @param limit     本批最多处理多少个类；会被钳到 [5, 40]
     */
    DescribeBatchResult describePending(Long projectId, int limit);

    /** 描述覆盖情况：总数 / 可描述数 / 已描述 / 待描述 / 图版本 */
    DescriptionStatusResponse descriptionStatus(Long projectId);
}
