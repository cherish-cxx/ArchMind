package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * 按包名结构划业务领域，纯函数、不读库。
 *
 * <p>算法：先按段计算所有包名的公共前缀并去掉；剩余段的<b>第一段</b>是非技术词时作为业务 key。
 * 第一段是技术段（controller/service/dto…）说明这个分支按层组织，key 为空——不再往更深的
 * 段找，否则 {@code service.agent}、{@code dto.request} 这类层内分组会被误判成业务领域。
 * 所有 key 都为空时退化为单领域（uid = main，name = 项目名）；
 * 存在业务 key 时每个 key 一个领域，期间只要有一个类第一段是技术段即为 technical-layered。
 * 基础设施领域收 role 为 INFRASTRUCTURE/OTHER 的类、无业务 key 的类，以及映射不到业务领域的类。
 */
public class BusinessAreaResolver {

    /** 业务领域划分结果；strategy 为 business-modules / technical-layered / single。 */
    public record AreaResult(List<BusinessArea> areas,
                             Map<String, String> classToArea,
                             String strategy) {
    }

    public static final String BUSINESS = "business";
    public static final String INFRASTRUCTURE = "infrastructure";
    public static final String SINGLE_AREA_UID = "main";

    public static final String STRATEGY_BUSINESS_MODULES = "business-modules";
    public static final String STRATEGY_TECHNICAL_LAYERED = "technical-layered";
    public static final String STRATEGY_SINGLE = "single";

    /** 只用于领域划分的技术段词表，与 role 分类互不影响。 */
    private static final Set<String> TECHNICAL_SEGMENTS = Set.of(
            "controller", "web", "rest", "api",
            "service", "biz", "usecase", "application",
            "domain", "model", "entity", "po", "vo", "dto", "bo",
            "mapper", "dao", "repository", "impl", "infra", "infrastructure",
            "config", "common", "util", "utils", "exception", "aspect", "filter", "interceptor",
            "client", "feign", "remote", "gateway", "fallback",
            "core", "base", "shared", "support", "query", "filters", "router", "routers");

    public AreaResult resolve(List<ClassSnapshot> classes,
                              Map<String, Classified> roles,
                              String projectName) {
        List<ClassSnapshot> safeClasses = classes == null ? List.of() : classes;
        Map<String, Classified> safeRoles = roles == null ? Map.of() : roles;

        List<List<String>> remainders = computeRemainders(safeClasses);

        Map<String, String> keyByUid = new LinkedHashMap<>();
        boolean skippedAnyTechnical = false;
        for (int i = 0; i < safeClasses.size(); i++) {
            ClassSnapshot cls = safeClasses.get(i);
            if (cls == null || cls.uid() == null) {
                continue;
            }
            KeyScan scan = scanKey(remainders.get(i));
            keyByUid.put(cls.uid(), scan.key());
            skippedAnyTechnical |= scan.skippedTechnical();
        }

        boolean allKeysEmpty = keyByUid.values().stream().allMatch(String::isEmpty);
        return allKeysEmpty
                ? resolveSingle(safeClasses, safeRoles, keyByUid, projectName)
                : resolveByKey(safeClasses, safeRoles, keyByUid, skippedAnyTechnical);
    }

    /** 全部 key 为空：所有非基础设施类归一个业务领域。 */
    private AreaResult resolveSingle(List<ClassSnapshot> classes,
                                     Map<String, Classified> roles,
                                     Map<String, String> keyByUid,
                                     String projectName) {
        String areaName = projectName == null ? "" : projectName;
        BusinessArea main = new BusinessArea(SINGLE_AREA_UID, areaName, BUSINESS);

        Map<String, String> classToArea = new LinkedHashMap<>();
        boolean hasInfrastructure = false;
        for (ClassSnapshot cls : classes) {
            if (cls == null || cls.uid() == null) {
                continue;
            }
            if (isInfrastructureRole(roles.get(cls.uid()))) {
                classToArea.put(cls.uid(), INFRASTRUCTURE);
                hasInfrastructure = true;
            } else {
                classToArea.put(cls.uid(), SINGLE_AREA_UID);
            }
        }

        List<BusinessArea> areas = new ArrayList<>();
        areas.add(main);
        if (hasInfrastructure) {
            areas.add(new BusinessArea(INFRASTRUCTURE, INFRASTRUCTURE, INFRASTRUCTURE));
        }
        return new AreaResult(List.copyOf(areas), classToArea, STRATEGY_SINGLE);
    }

    /** 存在业务 key：每个 key 一个领域，剩余类进基础设施。 */
    private AreaResult resolveByKey(List<ClassSnapshot> classes,
                                    Map<String, Classified> roles,
                                    Map<String, String> keyByUid,
                                    boolean skippedAnyTechnical) {
        // 只有真正会落到业务领域的类才产生 key：基础设施/OTHER 角色或 key 为空的类直接归基础设施。
        // 否则会出现「有 key 但一个类都没有」的空领域。
        Map<String, String> businessKey = new LinkedHashMap<>();
        for (ClassSnapshot cls : classes) {
            if (cls == null || cls.uid() == null) {
                continue;
            }
            String key = keyByUid.getOrDefault(cls.uid(), "");
            businessKey.put(cls.uid(),
                    (isInfrastructureRole(roles.get(cls.uid())) || key.isEmpty()) ? "" : key);
        }

        TreeSet<String> keys = new TreeSet<>();
        for (String key : businessKey.values()) {
            if (!key.isEmpty()) {
                keys.add(key);
            }
        }

        Map<String, String> classToArea = new LinkedHashMap<>();
        boolean hasInfrastructure = false;
        for (ClassSnapshot cls : classes) {
            if (cls == null || cls.uid() == null) {
                continue;
            }
            String key = businessKey.getOrDefault(cls.uid(), "");
            if (key.isEmpty()) {
                classToArea.put(cls.uid(), INFRASTRUCTURE);
                hasInfrastructure = true;
            } else {
                classToArea.put(cls.uid(), key);
            }
        }

        // TreeSet 已按 uid 排序，infrastructure 追加在最后。
        List<BusinessArea> areas = new ArrayList<>();
        for (String key : keys) {
            areas.add(new BusinessArea(key, key, BUSINESS));
        }
        if (hasInfrastructure) {
            areas.add(new BusinessArea(INFRASTRUCTURE, INFRASTRUCTURE, INFRASTRUCTURE));
        }

        String strategy = skippedAnyTechnical
                ? STRATEGY_TECHNICAL_LAYERED
                : STRATEGY_BUSINESS_MODULES;
        return new AreaResult(List.copyOf(areas), classToArea, strategy);
    }

    private static boolean isInfrastructureRole(Classified classified) {
        if (classified == null || classified.role() == null) {
            return false;
        }
        return classified.role() == Role.INFRASTRUCTURE || classified.role() == Role.OTHER;
    }

    /** 按段计算所有非空包名的公共前缀，返回每个类去掉前缀后的段序列。 */
    private static List<List<String>> computeRemainders(List<ClassSnapshot> classes) {
        List<List<String>> segmentsByClass = new ArrayList<>(classes.size());
        List<String> commonPrefix = null;
        for (ClassSnapshot cls : classes) {
            List<String> segments = splitPackage(cls == null ? null : cls.packageName());
            segmentsByClass.add(segments);
            if (!segments.isEmpty()) {
                commonPrefix = commonPrefix == null
                        ? segments
                        : commonSegments(commonPrefix, segments);
            }
        }

        int prefixLength = commonPrefix == null ? 0 : commonPrefix.size();
        List<List<String>> remainders = new ArrayList<>(segmentsByClass.size());
        for (List<String> segments : segmentsByClass) {
            if (segments.size() <= prefixLength) {
                remainders.add(List.of());
            } else {
                remainders.add(List.copyOf(segments.subList(prefixLength, segments.size())));
            }
        }
        return remainders;
    }

    private static List<String> splitPackage(String packageName) {
        if (packageName == null || packageName.isBlank()) {
            return List.of();
        }
        return List.of(packageName.split("\\."));
    }

    private static List<String> commonSegments(List<String> a, List<String> b) {
        int length = Math.min(a.size(), b.size());
        int i = 0;
        while (i < length && a.get(i).equals(b.get(i))) {
            i++;
        }
        return a.subList(0, i);
    }

    /** 业务 key 只能是第一段：第一段是技术段时该分支没有业务信息（层内分组不算领域）。 */
    private static KeyScan scanKey(List<String> remainder) {
        if (remainder.isEmpty()) {
            return new KeyScan("", false);
        }
        String first = remainder.get(0);
        if (TECHNICAL_SEGMENTS.contains(first.toLowerCase(Locale.ROOT))) {
            return new KeyScan("", true);
        }
        return new KeyScan(first, false);
    }

    private record KeyScan(String key, boolean skippedTechnical) {
    }
}
