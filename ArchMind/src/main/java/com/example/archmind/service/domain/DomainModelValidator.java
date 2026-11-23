package com.example.archmind.service.domain;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * 领域模型对账：五条断言任一失败即拒绝落库，避免写半成品。
 */
public class DomainModelValidator {

    public ValidationReport validate(DomainModel model, int totalClasses) {
        List<String> failures = new ArrayList<>();

        Map<String, RoleClassifier.Classified> roles = model.roles();
        if (roles == null) {
            failures.add("role coverage mismatch: roles is null, expected " + totalClasses);
        } else {
            if (roles.size() != totalClasses) {
                failures.add("role coverage mismatch: expected " + totalClasses + " classes, got " + roles.size());
            }
            roles.forEach((uid, classified) -> {
                if (classified == null) {
                    failures.add("role is null for class " + uid);
                }
            });
        }

        Map<String, String> classToArea = model.classToArea();
        if (classToArea == null) {
            failures.add("BELONGS_TO coverage mismatch: classToArea is null, expected " + totalClasses);
        } else if (classToArea.size() != totalClasses) {
            failures.add("BELONGS_TO coverage mismatch: expected " + totalClasses
                    + " classes, got " + classToArea.size());
        }

        Set<String> areaUids = new HashSet<>();
        for (BusinessArea area : model.areas()) {
            if (area != null && area.uid() != null) {
                areaUids.add(area.uid());
            }
        }

        Map<String, Integer> classesPerArea = new HashMap<>();
        if (classToArea != null) {
            for (String areaUid : classToArea.values()) {
                if (areaUid != null) {
                    classesPerArea.merge(areaUid, 1, Integer::sum);
                }
            }
        }

        Set<String> areasWithCapability = new HashSet<>();
        for (Capability capability : model.capabilities()) {
            if (capability == null) {
                failures.add("capability is null");
                continue;
            }
            String uid = capability.uid();
            if (isBlank(capability.areaUid())) {
                failures.add("capability " + uid + " has empty areaUid");
            } else if (!areaUids.contains(capability.areaUid())) {
                failures.add("capability " + uid + " references unknown area " + capability.areaUid());
            } else {
                areasWithCapability.add(capability.areaUid());
            }
            if (isBlank(capability.entryUid())) {
                failures.add("capability " + uid + " has empty entryUid");
            }
        }

        for (BusinessArea area : model.areas()) {
            if (area == null) {
                continue;
            }
            if (classesPerArea.getOrDefault(area.uid(), 0) == 0) {
                failures.add("empty area " + area.uid() + " has no classes");
            }
            if ("business".equals(area.kind()) && !areasWithCapability.contains(area.uid())) {
                failures.add("business area " + area.uid() + " has no capability");
            }
        }

        return failures.isEmpty() ? new ValidationReport(true, List.of())
                : new ValidationReport(false, List.copyOf(failures));
    }

    private static boolean isBlank(String value) {
        return Objects.isNull(value) || value.isBlank();
    }
}
