package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 类卡片的上游摘要（谁指向它）。
 *
 * <p>三类关系分开给，因为语义不同：callers 是方法调用按 owner 聚合，
 * dependents 是类级 DEPENDS，subclasses 是 EXTENDS/IMPLEMENTS。
 * 列表按 uid 截断到 {@code cap}，{@code *Count} 是全量数量。</p>
 */
@Data
public class UpstreamDTO {

    private List<String> callers = List.of();
    private int callerCount;

    private List<String> dependents = List.of();
    private int dependentCount;

    private List<String> subclasses = List.of();
    private int subclassCount;
}
