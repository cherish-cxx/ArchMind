package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 类卡片的下游摘要（它指向谁）。
 *
 * <p>callees 是方法调用按 owner 聚合，dependencies 是类级 DEPENDS，
 * parents 是 EXTENDS/IMPLEMENTS。列表截断到 cap，{@code *Count} 是全量数量。</p>
 */
@Data
public class DownstreamDTO {

    private List<String> callees = List.of();
    private int calleeCount;

    private List<String> dependencies = List.of();
    private int dependencyCount;

    private List<String> parents = List.of();
    private int parentCount;
}
