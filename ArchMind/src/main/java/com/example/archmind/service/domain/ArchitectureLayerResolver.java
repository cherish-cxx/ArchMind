package com.example.archmind.service.domain;

import com.example.archmind.dto.response.ArchitectureClassDto;
import com.example.archmind.dto.response.ArchitectureEdgeSampleDto;
import com.example.archmind.dto.response.ArchitectureGroupDto;
import com.example.archmind.dto.response.ArchitectureLayerDto;
import com.example.archmind.dto.response.ArchitectureLayerEdgeDto;
import com.example.archmind.dto.response.ArchitectureLayersResponse;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 把「类角色 + 业务领域 + 包名」投影成六层架构视图，并聚合跨层调用。
 * 纯函数：不读库、不注入 Spring；输入列表不被修改。
 */
public class ArchitectureLayerResolver {

    /** 类行：uid/name/role/packageName/areaUid/areaName，均可能为 null。 */
    public record ClassRow(String uid, String name, String role, String packageName,
                           String areaUid, String areaName) {
    }

    /** 调用行：fromUid 调用 toUid 的总次数。 */
    public record PairCount(String fromUid, String toUid, int count) {
    }

    private static final String UNASSIGNED_KEY = "unassigned";
    private static final String UNASSIGNED_NAME = "未归属领域";
    private static final String DEFAULT_SEGMENT = "default";

    /**
     * @param classes             类行；null uid 的行被忽略
     * @param calls               类对调用次数；两端都必须在本批类中且层不同才计入
     * @param maxClassesPerGroup  每组最多返回的类数，超出记 truncated
     * @param maxSamplesPerEdge   每条层间边最多返回的类对样本数
     */
    public ArchitectureLayersResponse resolve(List<ClassRow> classes, List<PairCount> calls,
                                              int maxClassesPerGroup, int maxSamplesPerEdge) {
        int classCap = Math.max(0, maxClassesPerGroup);
        int sampleCap = Math.max(0, maxSamplesPerEdge);

        Map<ArchitectureLayer, List<ClassRow>> rowsByLayer = new EnumMap<>(ArchitectureLayer.class);
        for (ArchitectureLayer layer : ArchitectureLayer.values()) {
            rowsByLayer.put(layer, new ArrayList<>());
        }

        Map<String, ArchitectureLayer> layerByUid = new HashMap<>();
        int totalClasses = 0;
        if (classes != null) {
            for (ClassRow row : classes) {
                if (row == null || row.uid() == null) {
                    continue;
                }
                ArchitectureLayer layer = ArchitectureLayer.fromRole(row.role());
                layerByUid.put(row.uid(), layer);
                rowsByLayer.get(layer).add(row);
                totalClasses++;
            }
        }

        List<ArchitectureLayerDto> layers = new ArrayList<>(ArchitectureLayer.values().length);
        for (ArchitectureLayer layer : ArchitectureLayer.values()) {
            List<ClassRow> rows = rowsByLayer.get(layer);
            layers.add(new ArchitectureLayerDto(layer.key(), layer.label(), rows.size(),
                    buildGroups(layer, rows, classCap)));
        }

        return new ArchitectureLayersResponse(layers, buildEdges(layerByUid, calls, sampleCap),
                totalClasses);
    }

    private static List<ArchitectureGroupDto> buildGroups(ArchitectureLayer layer, List<ClassRow> rows,
                                                          int classCap) {
        boolean byArea = layer == ArchitectureLayer.ENTRY
                || layer == ArchitectureLayer.BUSINESS
                || layer == ArchitectureLayer.DOMAIN_MODEL;

        Map<String, GroupAccumulator> groups = new LinkedHashMap<>();
        for (ClassRow row : rows) {
            String key;
            String name;
            String detail;
            if (byArea) {
                key = blankTo(row.areaUid(), UNASSIGNED_KEY);
                name = blankTo(row.areaName(), UNASSIGNED_NAME);
                detail = null;
            } else {
                key = lastPackageSegment(row.packageName());
                name = key;
                detail = row.packageName();
            }
            groups.computeIfAbsent(key, k -> new GroupAccumulator(k, name, detail)).rows.add(row);
        }

        List<ArchitectureGroupDto> result = new ArrayList<>(groups.size());
        for (GroupAccumulator group : groups.values()) {
            List<ClassRow> sorted = new ArrayList<>(group.rows);
            sorted.sort(Comparator
                    .comparing((ClassRow r) -> r.name(), Comparator.nullsLast(String::compareTo))
                    .thenComparing(ClassRow::uid));
            boolean truncated = sorted.size() > classCap;
            int shown = Math.min(sorted.size(), classCap);
            List<ArchitectureClassDto> classes = new ArrayList<>(shown);
            for (int i = 0; i < shown; i++) {
                ClassRow row = sorted.get(i);
                classes.add(new ArchitectureClassDto(row.uid(), row.name(), row.role()));
            }
            result.add(new ArchitectureGroupDto(group.key, group.name, group.detail,
                    sorted.size(), classes, truncated));
        }

        // 类多的组在前；同数按 key 倒序，保证输出稳定且与计划中的示例一致。
        result.sort(Comparator.comparingInt(ArchitectureGroupDto::classCount).reversed()
                .thenComparing(ArchitectureGroupDto::key, Comparator.reverseOrder()));
        return result;
    }

    private static List<ArchitectureLayerEdgeDto> buildEdges(Map<String, ArchitectureLayer> layerByUid,
                                                             List<PairCount> calls, int sampleCap) {
        Map<EdgeKey, EdgeAccumulator> byEdge = new HashMap<>();
        if (calls != null) {
            for (PairCount call : calls) {
                if (call == null || call.fromUid() == null || call.toUid() == null) {
                    continue;
                }
                ArchitectureLayer from = layerByUid.get(call.fromUid());
                ArchitectureLayer to = layerByUid.get(call.toUid());
                if (from == null || to == null || from == to) {
                    continue;
                }
                byEdge.computeIfAbsent(new EdgeKey(from, to), k -> new EdgeAccumulator(from, to))
                        .add(call);
            }
        }

        List<ArchitectureLayerEdgeDto> edges = new ArrayList<>(byEdge.size());
        for (EdgeAccumulator edge : byEdge.values()) {
            List<ArchitectureEdgeSampleDto> samples = new ArrayList<>(edge.samples);
            samples.sort(Comparator.comparingInt(ArchitectureEdgeSampleDto::count).reversed()
                    .thenComparing(ArchitectureEdgeSampleDto::fromUid)
                    .thenComparing(ArchitectureEdgeSampleDto::toUid));
            if (samples.size() > sampleCap) {
                samples = new ArrayList<>(samples.subList(0, sampleCap));
            }
            edges.add(new ArchitectureLayerEdgeDto(edge.from.key(), edge.to.key(), edge.count,
                    List.copyOf(samples)));
        }

        edges.sort(Comparator.comparingInt(ArchitectureLayerEdgeDto::count).reversed()
                .thenComparing(ArchitectureLayerEdgeDto::fromLayer)
                .thenComparing(ArchitectureLayerEdgeDto::toLayer));
        return edges;
    }

    private static String blankTo(String value, String fallback) {
        return value == null || value.isBlank() ? fallback : value;
    }

    private static String lastPackageSegment(String packageName) {
        if (packageName == null || packageName.isBlank()) {
            return DEFAULT_SEGMENT;
        }
        String[] segments = packageName.trim().split("\\.");
        for (int i = segments.length - 1; i >= 0; i--) {
            if (!segments[i].isEmpty()) {
                return segments[i];
            }
        }
        return DEFAULT_SEGMENT;
    }

    private record EdgeKey(ArchitectureLayer from, ArchitectureLayer to) {
    }

    private static final class GroupAccumulator {
        private final String key;
        private final String name;
        private final String detail;
        private final List<ClassRow> rows = new ArrayList<>();

        private GroupAccumulator(String key, String name, String detail) {
            this.key = key;
            this.name = name;
            this.detail = detail;
        }
    }

    private static final class EdgeAccumulator {
        private final ArchitectureLayer from;
        private final ArchitectureLayer to;
        private final List<ArchitectureEdgeSampleDto> samples = new ArrayList<>();
        private int count;

        private EdgeAccumulator(ArchitectureLayer from, ArchitectureLayer to) {
            this.from = from;
            this.to = to;
        }

        private void add(PairCount call) {
            count += call.count();
            samples.add(new ArchitectureEdgeSampleDto(call.fromUid(), call.toUid(), call.count()));
        }
    }
}
