package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.AreaCall;
import com.example.archmind.service.domain.Capability.CapabilityResult;
import com.example.archmind.service.domain.Capability.Member;
import com.example.archmind.service.domain.GraphSnapshot.CallEdgeSnapshot;
import com.example.archmind.service.domain.GraphSnapshot.EndpointSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * 从入口沿 CALLS 向下展开，生成能力与跨领域调用。纯函数、不读库。
 *
 * <p>每个入口做一次 BFS：入口方法所属类必为成员且 depth = 0；逐条调用边先看目标类 role，
 * {@link Role#EXTERNAL} 记 gap 并停止该分支；再看目标领域，属于另一个业务领域时记
 * {@link AreaCall} 并停止；基础设施领域的类照常加入成员并继续展开。
 * 方法 uid 去重防止环，类 uid 去重且 depth 取最短跳数。入口领域为空或为基础设施时跳过该入口。
 */
public class CapabilityResolver {

    private static final String INFRASTRUCTURE_AREA = BusinessAreaResolver.INFRASTRUCTURE;
    private static final String GAP_EXTERNAL_PREFIX = "external:";

    public CapabilityResult resolve(List<EndpointSnapshot> endpoints,
                                    Map<String, String> classToArea,
                                    Map<String, Classified> roles,
                                    List<CallEdgeSnapshot> calls) {
        Map<String, String> safeAreas = classToArea == null ? Map.of() : classToArea;
        Map<String, Classified> safeRoles = roles == null ? Map.of() : roles;
        Map<String, List<String>> adjacency = indexCalls(calls);

        List<Capability> capabilities = new ArrayList<>();
        Set<AreaCall> areaCalls = new LinkedHashSet<>();
        Set<String> gaps = new LinkedHashSet<>();

        List<EndpointSnapshot> safeEndpoints = endpoints == null ? List.of() : endpoints;
        // 同一路径可能有多份实现（单体与微服务并存）。按 uid + handler 排序后只取第一份，
        // 保证结果稳定；基础设施那份通常被后面的 entryArea 检查跳过。
        safeEndpoints = new ArrayList<>(safeEndpoints);
        safeEndpoints.sort(Comparator.comparing(EndpointSnapshot::uid)
                .thenComparing(EndpointSnapshot::handlerUid));
        Set<String> seenCapabilityUids = new LinkedHashSet<>();
        for (EndpointSnapshot endpoint : safeEndpoints) {
            if (endpoint == null || endpoint.uid() == null || endpoint.handlerUid() == null) {
                continue;
            }
            String entryClass = owner(endpoint.handlerUid());
            String entryArea = safeAreas.get(entryClass);
            if (entryArea == null || entryArea.isBlank() || INFRASTRUCTURE_AREA.equals(entryArea)) {
                continue;
            }
            if (!seenCapabilityUids.add(endpoint.uid())) {
                continue;
            }

            Traversal traversal = traverse(entryClass, entryArea, safeAreas, safeRoles, adjacency,
                    endpoint.handlerUid());
            areaCalls.addAll(traversal.areaCalls());
            gaps.addAll(traversal.gaps());
            capabilities.add(new Capability(endpoint.uid(), endpoint.uid(), entryArea,
                    endpoint.handlerUid(), traversal.members()));
        }

        capabilities.sort(Comparator.comparing(Capability::uid));

        List<AreaCall> sortedAreaCalls = new ArrayList<>(areaCalls);
        sortedAreaCalls.sort(Comparator.comparing(AreaCall::toAreaUid)
                .thenComparing(AreaCall::fromAreaUid)
                .thenComparing(AreaCall::fromClassUid)
                .thenComparing(AreaCall::toClassUid));

        List<String> sortedGaps = new ArrayList<>(gaps);
        Collections.sort(sortedGaps);

        return new CapabilityResult(List.copyOf(capabilities), List.copyOf(sortedAreaCalls),
                List.copyOf(sortedGaps));
    }

    /** fromMethodUid → 目标方法 uid 列表，保持调用边输入顺序。 */
    static Map<String, List<String>> indexCalls(List<CallEdgeSnapshot> calls) {
        Map<String, List<String>> adjacency = new LinkedHashMap<>();
        if (calls == null) {
            return adjacency;
        }
        for (CallEdgeSnapshot call : calls) {
            if (call == null || call.fromMethodUid() == null || call.toMethodUid() == null) {
                continue;
            }
            adjacency.computeIfAbsent(call.fromMethodUid(), key -> new ArrayList<>())
                    .add(call.toMethodUid());
        }
        return adjacency;
    }

    private static Traversal traverse(String entryClass,
                                      String entryArea,
                                      Map<String, String> classToArea,
                                      Map<String, Classified> roles,
                                      Map<String, List<String>> adjacency,
                                      String entryMethodUid) {
        // 方法 uid → 从入口方法出发的最短跳数；同时充当访问集合，防止环。
        Map<String, Integer> depthByMethod = new HashMap<>();
        // 类 uid → 成员最短跳数；TreeMap 保证按 uid 有序。
        Map<String, Integer> depthByClass = new TreeMap<>();
        Set<AreaCall> areaCalls = new LinkedHashSet<>();
        Set<String> gaps = new LinkedHashSet<>();

        depthByMethod.put(entryMethodUid, 0);
        depthByClass.merge(entryClass, 0, Math::min);
        Deque<String> queue = new ArrayDeque<>();
        queue.add(entryMethodUid);

        while (!queue.isEmpty()) {
            String methodUid = queue.poll();
            int depth = depthByMethod.getOrDefault(methodUid, 0);
            String currentClass = owner(methodUid);

            for (String callee : adjacency.getOrDefault(methodUid, List.of())) {
                if (depthByMethod.containsKey(callee)) {
                    continue;
                }
                depthByMethod.put(callee, depth + 1);

                String targetClass = owner(callee);
                Classified classified = roles.get(targetClass);
                if (classified != null && classified.role() == Role.EXTERNAL) {
                    gaps.add(GAP_EXTERNAL_PREFIX + callee);
                    continue;
                }

                String targetArea = classToArea.get(targetClass);
                if (targetArea != null && !targetArea.equals(entryArea)
                        && !INFRASTRUCTURE_AREA.equals(targetArea)) {
                    areaCalls.add(new AreaCall(entryArea, targetArea, currentClass, targetClass));
                    continue;
                }

                // 同领域、不在领域映射里，或基础设施领域：都是成员，并继续展开。
                depthByClass.merge(targetClass, depth + 1, Math::min);
                queue.add(callee);
            }
        }

        List<Member> members = new ArrayList<>(depthByClass.size());
        for (Map.Entry<String, Integer> entry : depthByClass.entrySet()) {
            members.add(new Member(entry.getKey(), entry.getValue()));
        }
        return new Traversal(List.copyOf(members), List.copyOf(areaCalls), List.copyOf(gaps));
    }

    /** 方法 uid 形如 owner#signature，owner 即所属类 uid。 */
    static String owner(String methodUid) {
        return methodUid.split("#")[0];
    }

    private record Traversal(List<Member> members, List<AreaCall> areaCalls, List<String> gaps) {
    }
}
