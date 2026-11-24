package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.CallEdgeSnapshot;
import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.GraphSnapshot.EndpointSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
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
 *
 * <p>包名划分不能把入口放进业务领域时（技术分层单体），改用 Controller 策略：按入口类名
 * （去掉 Controller/Resource/Action 后缀）划分领域，非入口类沿 CALLS 按到达它的入口数多数归属。
 * 没有端点或包名策略已经能放下入口时保持包名结果。
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
    public static final String STRATEGY_CONTROLLER_MODULES = "controller-modules";

    /** 入口层类名后缀；剥离后作为领域 key。 */
    private static final List<String> CONTROLLER_SUFFIXES = List.of("Controller", "Resource", "Action");

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
        return resolve(classes, roles, projectName, List.of(), List.of());
    }

    /**
     * 两套策略：包名能划分出承载入口的业务领域时保持原样；否则退回按入口层 Controller 名划分。
     * 没有入口时不触发 Controller 策略，保证纯包名调用的行为不变。
     */
    public AreaResult resolve(List<ClassSnapshot> classes,
                              Map<String, Classified> roles,
                              String projectName,
                              List<EndpointSnapshot> endpoints,
                              List<CallEdgeSnapshot> calls) {
        List<ClassSnapshot> safeClasses = classes == null ? List.of() : classes;
        Map<String, Classified> safeRoles = roles == null ? Map.of() : roles;

        AreaResult packageResult = resolveByPackage(safeClasses, safeRoles, projectName);
        if (!shouldTryControllerModules(packageResult, safeRoles, endpoints)) {
            return packageResult;
        }
        AreaResult controllerResult = resolveByControllers(safeClasses, safeRoles,
                endpoints == null ? List.of() : endpoints,
                calls == null ? List.of() : calls);
        return controllerResult == null ? packageResult : controllerResult;
    }

    private AreaResult resolveByPackage(List<ClassSnapshot> safeClasses,
                                        Map<String, Classified> safeRoles,
                                        String projectName) {
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

    // ==================== Controller 策略 ====================

    /** 仅当包名策略没能把入口放进业务领域（single，或入口全在基础设施）时才尝试。 */
    private static boolean shouldTryControllerModules(AreaResult packageResult,
                                                      Map<String, Classified> roles,
                                                      List<EndpointSnapshot> endpoints) {
        if (endpoints == null || endpoints.isEmpty()) {
            return false;
        }
        boolean hasEntryEndpoint = false;
        boolean entryInBusinessArea = false;
        for (EndpointSnapshot endpoint : endpoints) {
            if (endpoint == null || endpoint.handlerUid() == null) {
                continue;
            }
            String ownerClass = CapabilityResolver.owner(endpoint.handlerUid());
            Classified classified = roles.get(ownerClass);
            if (classified == null || classified.role() != Role.ENTRY) {
                continue;
            }
            hasEntryEndpoint = true;
            String area = packageResult.classToArea().get(ownerClass);
            if (area != null && !INFRASTRUCTURE.equals(area)) {
                entryInBusinessArea = true;
                break;
            }
        }
        if (!hasEntryEndpoint) {
            return false;
        }
        return STRATEGY_SINGLE.equals(packageResult.strategy()) || !entryInBusinessArea;
    }

    /** 无法从入口类派生出 key 时返回 null，调用方保留包名结果。 */
    private static AreaResult resolveByControllers(List<ClassSnapshot> classes,
                                                   Map<String, Classified> roles,
                                                   List<EndpointSnapshot> endpoints,
                                                   List<CallEdgeSnapshot> calls) {
        Map<String, ClassSnapshot> classByUid = new LinkedHashMap<>();
        for (ClassSnapshot cls : classes) {
            if (cls != null && cls.uid() != null) {
                classByUid.put(cls.uid(), cls);
            }
        }

        // 入口类 → 领域 key
        Map<String, String> keyByClass = new LinkedHashMap<>();
        for (EndpointSnapshot endpoint : endpoints) {
            if (endpoint == null || endpoint.handlerUid() == null) {
                continue;
            }
            String ownerClass = CapabilityResolver.owner(endpoint.handlerUid());
            Classified classified = roles.get(ownerClass);
            if (classified == null || classified.role() != Role.ENTRY) {
                continue;
            }
            ClassSnapshot cls = classByUid.get(ownerClass);
            if (cls == null) {
                continue;
            }
            String key = controllerKey(cls.name());
            if (key != null) {
                keyByClass.put(ownerClass, key);
            }
        }
        if (keyByClass.isEmpty()) {
            return null;
        }

        // 每个领域的种子方法：属于该领域入口类的 endpoint handler
        List<EndpointSnapshot> sortedEndpoints = new ArrayList<>();
        for (EndpointSnapshot endpoint : endpoints) {
            if (endpoint != null && endpoint.uid() != null && endpoint.handlerUid() != null) {
                sortedEndpoints.add(endpoint);
            }
        }
        sortedEndpoints.sort(Comparator.comparing(EndpointSnapshot::uid)
                .thenComparing(EndpointSnapshot::handlerUid));
        Map<String, TreeSet<String>> seedsByArea = new TreeMap<>();
        for (EndpointSnapshot endpoint : sortedEndpoints) {
            String key = keyByClass.get(CapabilityResolver.owner(endpoint.handlerUid()));
            if (key != null) {
                seedsByArea.computeIfAbsent(key, k -> new TreeSet<>()).add(endpoint.handlerUid());
            }
        }

        // 类 → (领域 → 到达它的种子数)，按种子去重
        Map<String, List<String>> adjacency = CapabilityResolver.indexCalls(calls);
        Map<String, Map<String, Integer>> weightByClass = new HashMap<>();
        for (Map.Entry<String, TreeSet<String>> areaSeeds : seedsByArea.entrySet()) {
            for (String seed : areaSeeds.getValue()) {
                for (String classUid : reachableClasses(seed, adjacency, roles)) {
                    weightByClass.computeIfAbsent(classUid, key -> new HashMap<>())
                            .merge(areaSeeds.getKey(), 1, Integer::sum);
                }
            }
        }

        Map<String, String> classToArea = new LinkedHashMap<>();
        boolean hasInfrastructure = false;
        for (ClassSnapshot cls : classes) {
            if (cls == null || cls.uid() == null) {
                continue;
            }
            String area = keyByClass.get(cls.uid());
            if (area == null) {
                area = isInfrastructureRole(roles.get(cls.uid()))
                        ? INFRASTRUCTURE
                        : winner(weightByClass.get(cls.uid()));
                if (area == null) {
                    area = INFRASTRUCTURE;
                }
            }
            classToArea.put(cls.uid(), area);
            hasInfrastructure |= INFRASTRUCTURE.equals(area);
        }

        List<BusinessArea> areas = new ArrayList<>();
        for (String key : new TreeSet<>(keyByClass.values())) {
            areas.add(new BusinessArea(key, key, BUSINESS));
        }
        if (hasInfrastructure) {
            areas.add(new BusinessArea(INFRASTRUCTURE, INFRASTRUCTURE, INFRASTRUCTURE));
        }
        return new AreaResult(List.copyOf(areas), classToArea, STRATEGY_CONTROLLER_MODULES);
    }

    /** 从入口方法出发收集可达类；EXTERNAL 与其它 ENTRY 类不进入也不再展开。 */
    private static Set<String> reachableClasses(String seedMethodUid,
                                                Map<String, List<String>> adjacency,
                                                Map<String, Classified> roles) {
        Set<String> reached = new LinkedHashSet<>();
        Set<String> visitedMethods = new HashSet<>();
        Deque<String> queue = new ArrayDeque<>();
        visitedMethods.add(seedMethodUid);
        queue.add(seedMethodUid);
        reached.add(CapabilityResolver.owner(seedMethodUid));

        while (!queue.isEmpty()) {
            String methodUid = queue.poll();
            for (String callee : adjacency.getOrDefault(methodUid, List.of())) {
                if (!visitedMethods.add(callee)) {
                    continue;
                }
                String targetClass = CapabilityResolver.owner(callee);
                Classified classified = roles.get(targetClass);
                if (classified != null && (classified.role() == Role.EXTERNAL
                        || classified.role() == Role.ENTRY)) {
                    continue;
                }
                reached.add(targetClass);
                queue.add(callee);
            }
        }
        return reached;
    }

    /** 唯一次数最多的领域；没有权重或并列时返回 null（留基础设施）。 */
    private static String winner(Map<String, Integer> weights) {
        if (weights == null || weights.isEmpty()) {
            return null;
        }
        String best = null;
        int bestWeight = -1;
        boolean tie = false;
        for (Map.Entry<String, Integer> entry : weights.entrySet()) {
            int weight = entry.getValue();
            if (weight > bestWeight) {
                best = entry.getKey();
                bestWeight = weight;
                tie = false;
            } else if (weight == bestWeight) {
                tie = true;
            }
        }
        return tie ? null : best;
    }

    /** 完整 Controller 名转领域 key；保留名加 -module，测试类不参与。 */
    static String controllerKey(String simpleName) {
        if (simpleName == null || simpleName.isBlank() || simpleName.contains("$")
                || simpleName.endsWith("Test") || simpleName.endsWith("Tests")) {
            return null;
        }
        String base = simpleName;
        for (String suffix : CONTROLLER_SUFFIXES) {
            if (base.endsWith(suffix)) {
                if (base.length() == suffix.length()) {
                    return null;
                }
                base = base.substring(0, base.length() - suffix.length());
                break;
            }
        }
        String key = kebab(base);
        if (key.isEmpty()) {
            return null;
        }
        return SINGLE_AREA_UID.equals(key) || INFRASTRUCTURE.equals(key) ? key + "-module" : key;
    }

    /** CamelCase → kebab-case：McpPat → mcp-pat，DomainMap → domain-map。 */
    private static String kebab(String name) {
        String spaced = name
                .replaceAll("(?<=[a-z0-9])(?=[A-Z])", "-")
                .replaceAll("(?<=[A-Z])(?=[A-Z][a-z])", "-");
        return spaced.toLowerCase(Locale.ROOT)
                .replaceAll("[^a-z0-9]+", "-")
                .replaceAll("^-+", "")
                .replaceAll("-+$", "");
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
