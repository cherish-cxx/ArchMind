package com.example.archmind;

import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshotDao;
import com.example.archmind.service.domain.RoleClassifier;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * R8 角色复抽（只读）：从已有项目的图重新分类，排除调参样本后等距抽 20 个新类。
 *
 * <pre>
 * ./mvnw -Dtest=R8RoleResampleIT -Dr8.resample=true \
 *   -Dr8.projectId=2122566927033626625 \
 *   -Dr8.tuning="uid1,uid2,..." test
 * </pre>
 *
 * 不写库、不改任何节点。
 */
@SpringBootTest
class R8RoleResampleIT {

    @Autowired
    private GraphSnapshotDao snapshotDao;

    @Test
    void resampleFreshTwenty() {
        Assumptions.assumeTrue(Boolean.getBoolean("r8.resample"),
                "需要 -Dr8.resample=true 才执行");
        String projectId = System.getProperty("r8.projectId");
        Assumptions.assumeTrue(projectId != null && !projectId.isBlank(), "需要 -Dr8.projectId");

        long pid = Long.parseLong(projectId);
        Set<String> tuning = new LinkedHashSet<>();
        String rawTuning = System.getProperty("r8.tuning", "");
        if (!rawTuning.isBlank()) {
            tuning.addAll(Arrays.asList(rawTuning.split(",")));
        }

        GraphSnapshot snapshot = snapshotDao.load(pid);
        var roles = new RoleClassifier().classify(snapshot.classes());

        System.out.printf("%n=== tuning sample after fix (%d) ===%n", tuning.size());
        for (String uid : tuning) {
            var classified = roles.get(uid);
            if (classified == null) {
                System.out.printf("TUNING %s -> MISSING%n", uid);
            } else {
                System.out.printf("TUNING %s -> %s/%s%n", uid, classified.role(), classified.basis());
            }
        }

        List<GraphSnapshot.ClassSnapshot> fresh = new ArrayList<>();
        for (GraphSnapshot.ClassSnapshot cls : snapshot.classes()) {
            if (!tuning.contains(cls.uid())) {
                fresh.add(cls);
            }
        }
        fresh.sort((a, b) -> a.uid().compareTo(b.uid()));

        int n = fresh.size();
        int k = Math.min(20, n);
        System.out.printf("%n=== fresh sample: population=%d sampled=%d ===%n", n, k);
        for (int i = 0; i < k; i++) {
            int idx = k == 1 ? 0 : Math.round((float) i * (n - 1) / (k - 1));
            var cls = fresh.get(idx);
            var classified = roles.get(cls.uid());
            System.out.printf("SAMPLE %s | %s | %s | %s | %s%n",
                    cls.uid(),
                    classified == null ? "?" : classified.role(),
                    classified == null ? "?" : classified.basis(),
                    cls.packageName(),
                    cls.annotationNames());
        }
    }
}
