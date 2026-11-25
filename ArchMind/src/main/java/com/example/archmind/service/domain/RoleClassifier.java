package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;

import java.util.ArrayList;
import java.util.EnumMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * 按「注解 → 父类/接口 → 包名段 → 类名后缀 → 同包多数派」五级顺序给类定角色。
 * 首个命中即停；五级都不命中为 {@link Role#OTHER}，basis 记 none。
 */
public class RoleClassifier {

    /** 分类结果；basis 为 annotation / supertype / package / name / package-majority / none。 */
    public record Classified(String uid, Role role, String basis) {
    }

    /** 注解简单名 → 角色；插入顺序即同级优先级。 */
    private static final Map<String, Role> ANNOTATION_ROLES = new LinkedHashMap<>();

    /** 包名段（小写）→ 角色。 */
    private static final Map<String, Role> PACKAGE_SEGMENT_ROLES = new LinkedHashMap<>();

    /** 类名后缀 → 角色；插入顺序即同级优先级。 */
    private static final Map<String, Role> NAME_SUFFIX_ROLES = new LinkedHashMap<>();

    static {
        for (String name : List.of("RestController", "Controller", "ControllerAdvice", "Scheduled",
                "RabbitListener", "KafkaListener", "EventListener")) {
            ANNOTATION_ROLES.put(name, Role.ENTRY);
        }
        ANNOTATION_ROLES.put("Service", Role.BUSINESS);
        for (String name : List.of("Mapper", "Repository", "Configuration",
                "ConfigurationProperties", "Aspect")) {
            ANNOTATION_ROLES.put(name, Role.INFRASTRUCTURE);
        }
        ANNOTATION_ROLES.put("Entity", Role.DOMAIN_MODEL);
        ANNOTATION_ROLES.put("TableName", Role.DOMAIN_MODEL);
        ANNOTATION_ROLES.put("FeignClient", Role.EXTERNAL);

        for (String seg : List.of("controller", "web", "rest", "listener")) {
            PACKAGE_SEGMENT_ROLES.put(seg, Role.ENTRY);
        }
        for (String seg : List.of("service", "biz", "usecase", "application")) {
            PACKAGE_SEGMENT_ROLES.put(seg, Role.BUSINESS);
        }
        for (String seg : List.of("domain", "model", "entity", "po", "vo", "dto", "bo", "enums")) {
            PACKAGE_SEGMENT_ROLES.put(seg, Role.DOMAIN_MODEL);
        }
        for (String seg : List.of("mapper", "dao", "repository", "infra", "config", "common",
                "util", "utils", "exception", "aspect", "filter", "filters", "interceptor")) {
            PACKAGE_SEGMENT_ROLES.put(seg, Role.INFRASTRUCTURE);
        }
        for (String seg : List.of("client", "feign", "remote")) {
            PACKAGE_SEGMENT_ROLES.put(seg, Role.EXTERNAL);
        }

        NAME_SUFFIX_ROLES.put("Controller", Role.ENTRY);
        for (String suffix : List.of("Service", "ServiceImpl", "Manager")) {
            NAME_SUFFIX_ROLES.put(suffix, Role.BUSINESS);
        }
        for (String suffix : List.of("Mapper", "Dao", "Repository", "Config", "Util", "Utils",
                "Helper", "Exception", "Interceptor", "Filter", "Aspect", "Constants")) {
            NAME_SUFFIX_ROLES.put(suffix, Role.INFRASTRUCTURE);
        }
        for (String suffix : List.of("Client", "Feign")) {
            NAME_SUFFIX_ROLES.put(suffix, Role.EXTERNAL);
        }
        for (String suffix : List.of("Entity", "PO", "DO", "DTO", "VO", "Query", "Command")) {
            NAME_SUFFIX_ROLES.put(suffix, Role.DOMAIN_MODEL);
        }
    }

    /** 返回按 uid 排序的分类结果。 */
    public Map<String, Classified> classify(List<ClassSnapshot> classes) {
        Map<String, Classified> byUid = new TreeMap<>();
        Map<String, String> packageByUid = new LinkedHashMap<>();
        if (classes == null) {
            return new LinkedHashMap<>();
        }
        for (ClassSnapshot cls : classes) {
            if (cls == null || cls.uid() == null) {
                continue;
            }
            byUid.put(cls.uid(), resolve(cls));
            packageByUid.put(cls.uid(), cls.packageName() == null ? "" : cls.packageName());
        }

        applyPackageMajority(byUid, packageByUid);
        return new LinkedHashMap<>(byUid);
    }

    private static Classified resolve(ClassSnapshot cls) {
        String uid = cls.uid();

        Role role = fromAnnotations(cls.annotationNames());
        if (role != null) {
            return new Classified(uid, role, "annotation");
        }
        role = fromSupertypes(cls.interfaces(), cls.superclass());
        if (role != null) {
            return new Classified(uid, role, "supertype");
        }
        role = fromPackage(cls.packageName());
        if (role != null) {
            return new Classified(uid, role, "package");
        }
        role = fromName(cls);
        if (role != null) {
            return new Classified(uid, role, "name");
        }
        return new Classified(uid, Role.OTHER, "none");
    }

    private static Role fromAnnotations(List<String> annotationNames) {
        if (annotationNames == null) {
            return null;
        }
        Set<String> simpleNames = new LinkedHashSet<>();
        for (String name : annotationNames) {
            if (name != null) {
                simpleNames.add(simpleName(name));
            }
        }
        for (Map.Entry<String, Role> entry : ANNOTATION_ROLES.entrySet()) {
            if (simpleNames.contains(entry.getKey())) {
                return entry.getValue();
            }
        }
        return null;
    }

    private static Role fromSupertypes(List<String> interfaces, String superclass) {
        if (interfaces != null) {
            for (String itf : interfaces) {
                String name = simpleName(itf);
                if ("Mapper".equals(name) || "BaseMapper".equals(name)) {
                    return Role.INFRASTRUCTURE;
                }
            }
        }
        String superName = simpleName(superclass);
        if (superName.endsWith("Controller")) {
            return Role.ENTRY;
        }
        if (superName.endsWith("Exception")) {
            return Role.INFRASTRUCTURE;
        }
        return null;
    }

    private static Role fromPackage(String packageName) {
        if (packageName == null || packageName.isBlank()) {
            return null;
        }
        // 从右往左：越靠后的段越具体。`common.controller` 应判成入口而不是基础设施。
        String[] segments = packageName.split("\\.");
        for (int i = segments.length - 1; i >= 0; i--) {
            Role role = PACKAGE_SEGMENT_ROLES.get(segments[i].toLowerCase(Locale.ROOT));
            if (role != null) {
                return role;
            }
        }
        return null;
    }

    private static Role fromName(ClassSnapshot cls) {
        String simpleName = cls.name();
        if (simpleName == null || simpleName.isBlank()) {
            simpleName = simpleName(cls.uid());
        }
        for (Map.Entry<String, Role> entry : NAME_SUFFIX_ROLES.entrySet()) {
            if (simpleName.endsWith(entry.getKey())) {
                return entry.getValue();
            }
        }
        return null;
    }

    /**
     * 直接包下至少 3 个已确定类、且 ≥70% 同一角色时，把该包的 OTHER 改成多数派角色。
     * 只改 OTHER，已确定的类保持原 basis。
     */
    private static void applyPackageMajority(Map<String, Classified> byUid,
                                             Map<String, String> packageByUid) {
        Map<String, List<String>> uidsByPackage = new LinkedHashMap<>();
        for (Map.Entry<String, String> entry : packageByUid.entrySet()) {
            uidsByPackage.computeIfAbsent(entry.getValue(), key -> new ArrayList<>()).add(entry.getKey());
        }

        Map<String, Role> majorityByPackage = new LinkedHashMap<>();
        for (Map.Entry<String, List<String>> entry : uidsByPackage.entrySet()) {
            Map<Role, Integer> counts = new EnumMap<>(Role.class);
            int determined = 0;
            for (String uid : entry.getValue()) {
                Role role = byUid.get(uid).role();
                if (role != Role.OTHER) {
                    counts.merge(role, 1, Integer::sum);
                    determined++;
                }
            }
            if (determined < 3) {
                continue;
            }
            Role best = null;
            int bestCount = 0;
            for (Role role : Role.values()) {
                int count = counts.getOrDefault(role, 0);
                if (count > bestCount) {
                    best = role;
                    bestCount = count;
                }
            }
            if (best != null && bestCount * 10 >= determined * 7) {
                majorityByPackage.put(entry.getKey(), best);
            }
        }

        for (Map.Entry<String, Classified> entry : byUid.entrySet()) {
            Classified classified = entry.getValue();
            if (classified.role() != Role.OTHER) {
                continue;
            }
            Role majority = majorityByPackage.get(packageByUid.get(entry.getKey()));
            if (majority != null) {
                entry.setValue(new Classified(classified.uid(), majority, "package-majority"));
            }
        }
    }

    private static String simpleName(String qualifiedName) {
        if (qualifiedName == null) {
            return "";
        }
        int dot = qualifiedName.lastIndexOf('.');
        return dot >= 0 ? qualifiedName.substring(dot + 1) : qualifiedName;
    }
}
