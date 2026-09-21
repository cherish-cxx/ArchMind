package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.CallChainResponse;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.service.ast.CallChainService;
import com.example.archmind.service.ast.ChainDirection;
import com.example.archmind.service.ast.GapReason;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 调用链：Java 侧逐层 BFS，不用变长 Cypher。
 *
 * <p>三层原因：① 走到接口断点要换成 {@code IMPLEMENTS} 继续，变长模式表达不了；
 * ② 变长路径是引擎内部枚举所有路径，外层 LIMIT 拦不住，逐层才能每层限流；
 * ③ 「断在哪、为什么断」在逐层推进里是自然事件，变长查询只知道总深度。</p>
 *
 * <p>最常见的断点是「接口无出边」—— 标准 Spring 项目 {@code @Autowired} 注入接口，
 * 边指向接口方法，而接口方法没有方法体。扇出到实现类把它从死路变成分支点。</p>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class CallChainServiceImpl implements CallChainService {

    /** 硬上限：链在稠密图上会发散，深度由调用方给，但服务端再 clamp 一次 */
    private static final int MAX_DEPTH_HARD = 3;
    private static final int MAX_NODES_HARD = 200;

    /** 命中这些特征且接口无实现 → 判为「调用跨到了另一个服务」。行业惯例，属启发式 */
    private static final List<String> API_MODULE_SUFFIXES = List.of("-api");
    private static final List<String> CLIENT_PACKAGE_MARKERS = List.of(".client", ".feign");

    private static final String START_METHODS = """
            MATCH (m:Method {projectId: $pid, owner: $uid})
            WHERE $methodUid IS NULL OR m.uid = $methodUid
            RETURN m.uid AS uid, m.owner AS owner, m.signature AS signature
            ORDER BY m.startLine
            """;

    private static final String BATCH_1HOP = """
            MATCH (m:Method {projectId: $pid})-[:CALLS]->(t:Method {projectId: $pid})
            WHERE m.uid IN $uids
            RETURN m.uid AS fromMethod, m.owner AS fromOwner,
                   t.uid AS toMethod, t.owner AS toOwner, t.signature AS toSignature
            """;

    private static final String BATCH_CLASS_INFO = """
            MATCH (c:Class {projectId: $pid})
            WHERE c.uid IN $uids
            RETURN c.uid AS uid, c.name AS name, c.kind AS kind, c.packageName AS packageName,
                   c.filePath AS filePath, c.startLine AS startLine, c.endLine AS endLine,
                   c.description AS description, split(c.filePath, '/')[1] AS module
            """;

    /**
     * 扇出：接口方法 → 实现类里**签名相同**的方法。
     * 签名用的是参数类型的源码字面量，接口与实现声明同一方法时必然相同，直接相等配对即可。
     */
    private static final String BATCH_FANOUT = """
            UNWIND $rows AS row
            MATCH (impl:Class {projectId: $pid})-[:IMPLEMENTS]->(iface:Class {projectId: $pid, uid: row.ifaceUid})
            MATCH (im:Method {projectId: $pid, owner: impl.uid, signature: row.signature})
            RETURN row.ifaceUid AS ifaceUid, im.uid AS toMethod,
                   im.owner AS toOwner, im.signature AS toSignature
            """;

    /** 反向扇出（UP 用）：这个类实现了哪些接口 */
    private static final String BATCH_INTERFACES_OF = """
            MATCH (c:Class {projectId: $pid})-[:IMPLEMENTS]->(iface:Class {projectId: $pid})
            WHERE c.uid IN $uids
            RETURN iface.uid AS ifaceUid
            """;

    /** 反向扇出（UP 用）：按签名在接口上找对应方法 */
    private static final String BATCH_IFACE_METHOD_BY_SIG = """
            UNWIND $rows AS row
            MATCH (im:Method {projectId: $pid, owner: row.ifaceUid, signature: row.signature})
            RETURN row.implMethodUid AS implMethodUid, im.uid AS ifaceMethodUid
            """;

    private static final String BATCH_CALLERS = """
            MATCH (c:Method {projectId: $pid})-[:CALLS]->(t:Method {projectId: $pid})
            WHERE t.uid IN $uids
            RETURN c.owner AS fromOwner, t.uid AS toMethod
            """;

    private final Driver driver;

    // ==================== 入口 ====================

    @Override
    public CallChainResponse callChain(Long projectId, String uid, String methodUid,
                                       ChainDirection direction, int maxDepth, int maxNodes) {
        if (projectId == null || uid == null || uid.isBlank()) {
            throw new BusinessException("缺少 projectId 或 uid");
        }
        int depth = Math.max(1, Math.min(maxDepth, MAX_DEPTH_HARD));
        int nodes = Math.max(1, Math.min(maxNodes, MAX_NODES_HARD));

        Map<String, ClassInfo> cache = new HashMap<>();
        ClassInfo start = loadClass(projectId, uid, cache);
        if (start == null) {
            throw new BusinessException("类不存在: " + uid);
        }
        return direction == ChainDirection.UP
                ? up(projectId, uid, methodUid, nodes, start, cache)
                : down(projectId, uid, methodUid, depth, nodes, start, cache);
    }

    // ==================== DOWN：谁调用了谁 ====================

    private CallChainResponse down(Long pid, String uid, String methodUid, int maxDepth, int maxNodes,
                                   ClassInfo start, Map<String, ClassInfo> cache) {
        CallChainResponse resp = new CallChainResponse();
        resp.setStart(toClassNode(start));

        Map<String, CallChainResponse.ChainNode> nodeByUid = new LinkedHashMap<>();
        Map<String, CallChainResponse.ChainEdge> edgeByKey = new LinkedHashMap<>();
        Set<String> fanoutClasses = new LinkedHashSet<>();
        GapCollector gaps = new GapCollector();
        Set<String> visited = new HashSet<>();

        addNode(resp, nodeByUid, start, 0, false);
        List<MRef> frontier = startMethods(pid, uid, methodUid, 0);
        frontier.forEach(m -> visited.add(m.uid()));

        for (int level = 1; level <= maxDepth && !frontier.isEmpty(); level++) {
            // 扇出与断点判定都在「查下游之前」，扇出来的实现类方法留在同一层（不计 depth）
            frontier = expandFanout(pid, frontier, cache, gaps, nodeByUid, edgeByKey, fanoutClasses, resp);

            List<HopRow> rows = hop(pid, frontier);
            if (rows.isEmpty()) {
                break;
            }

            Set<String> ownerUids = new LinkedHashSet<>();
            rows.forEach(r -> ownerUids.add(r.toOwner()));
            loadClasses(pid, ownerUids, cache);

            List<MRef> next = new ArrayList<>();
            for (HopRow r : rows) {
                ClassInfo to = cache.get(r.toOwner());
                ClassInfo from = cache.get(r.fromOwner());
                if (to == null || from == null) {
                    continue;
                }
                CallChainResponse.ChainNode node = nodeByUid.get(to.uid());
                if (node == null) {
                    node = addNode(resp, nodeByUid, to, level, fanoutClasses.contains(to.uid()));
                }

                // 自环（本类方法调本类方法）不画边 —— 类级视图里它只是个圈，信息价值低。
                // 但**不能 continue**：这条调用的下游还要继续走，否则链路会在这里断掉。
                if (!from.uid().equals(to.uid())) {
                    node.setEdgeCount(node.getEdgeCount() + 1);
                    addEdge(edgeByKey, resp, from.uid(), to.uid(), "CALLS", false);
                }

                if (visited.add(r.toMethod())) {
                    next.add(new MRef(r.toMethod(), r.toOwner(), r.toSignature(), level));
                }
            }

            if (next.size() > maxNodes) {
                next = new ArrayList<>(next.subList(0, maxNodes));
                resp.setTruncated(true);
            }
            frontier = next;
        }

        resp.getGaps().addAll(gaps.list());
        return resp;
    }

    // ==================== UP：谁调用了它（1 层） ====================

    private CallChainResponse up(Long pid, String uid, String methodUid, int maxNodes,
                                 ClassInfo start, Map<String, ClassInfo> cache) {
        CallChainResponse resp = new CallChainResponse();
        resp.setStart(toClassNode(start));

        Map<String, CallChainResponse.ChainNode> nodeByUid = new LinkedHashMap<>();
        Map<String, CallChainResponse.ChainEdge> edgeByKey = new LinkedHashMap<>();
        addNode(resp, nodeByUid, start, 0, false);

        List<MRef> own = startMethods(pid, uid, methodUid, 0);
        Map<String, Boolean> targetFanout = new LinkedHashMap<>();
        own.forEach(m -> targetFanout.put(m.uid(), false));

        // 反向扇出：@Autowired 注入的是接口，直接调实现类的人极少；
        // 不把它实现的接口上的同签名方法一并算进来，问「谁调用它」会得到 0
        if (!"INTERFACE".equals(start.kind())) {
            reverseFanout(pid, uid, own, targetFanout);
        }
        if (targetFanout.isEmpty()) {
            return resp;
        }

        Map<String, Object> params = new HashMap<>();
        params.put("pid", pid);
        params.put("uids", new ArrayList<>(targetFanout.keySet()));

        Set<String> callerClasses = new LinkedHashSet<>();
        try (Session session = driver.session()) {
            Result result = session.run(BATCH_CALLERS, params);
            while (result.hasNext()) {
                Record r = result.next();
                String fromOwner = r.get("fromOwner").asString();
                if (!callerClasses.contains(fromOwner) && callerClasses.size() >= maxNodes) {
                    resp.setTruncated(true);
                    continue;
                }
                callerClasses.add(fromOwner);
                loadClass(pid, fromOwner, cache);
                ClassInfo from = cache.get(fromOwner);
                if (from == null) {
                    continue;
                }
                boolean viaIface = Boolean.TRUE.equals(targetFanout.get(r.get("toMethod").asString()));

                CallChainResponse.ChainNode node = nodeByUid.get(from.uid());
                if (node == null) {
                    node = addNode(resp, nodeByUid, from, 1, false);
                }
                // 自环同样不画：起点类的方法调本类另一个方法，不构成「谁调用了它」的信息
                if (!from.uid().equals(start.uid())) {
                    node.setEdgeCount(node.getEdgeCount() + 1);
                    addEdge(edgeByKey, resp, from.uid(), start.uid(), "CALLS", viaIface);
                }
            }
        }
        return resp;
    }

    /** 把「起点类实现的接口上的同签名方法」加入反查目标；命中的标 true，表示这一跳实际走的是接口 */
    private void reverseFanout(Long pid, String uid, List<MRef> own, Map<String, Boolean> targetFanout) {
        List<String> ifaceUids = new ArrayList<>();
        try (Session session = driver.session()) {
            Result r = session.run(BATCH_INTERFACES_OF, Map.of("pid", pid, "uids", List.of(uid)));
            while (r.hasNext()) {
                ifaceUids.add(r.next().get("ifaceUid").asString());
            }
        }
        if (ifaceUids.isEmpty()) {
            return;
        }

        List<Map<String, Object>> rows = new ArrayList<>();
        for (MRef m : own) {
            if (m.signature() == null) {
                continue;
            }
            for (String ifaceUid : ifaceUids) {
                rows.add(Map.of("implMethodUid", m.uid(), "ifaceUid", ifaceUid, "signature", m.signature()));
            }
        }
        if (rows.isEmpty()) {
            return;
        }
        try (Session session = driver.session()) {
            Result r = session.run(BATCH_IFACE_METHOD_BY_SIG, Map.of("pid", pid, "rows", rows));
            while (r.hasNext()) {
                targetFanout.put(r.next().get("ifaceMethodUid").asString(), true);
            }
        }
    }

    // ==================== 扇出与断点 ====================

    /**
     * 把 frontier 里的接口方法替换成「实现类的同签名方法」，**留在同一层**，并登记实现类节点与
     * {@code IMPLEMENTS} 边。
     *
     * <p>不计 depth：接口方法与它的实现是同一次调用的两面，从接口绕到实现没有看到新东西，
     * 只是终于找着门了。算成走了路，用户会觉得预算莫名少了一层。</p>
     *
     * <p>扇不出去的接口方法记 gap —— 这正是「诚实地说断在哪」的地方。</p>
     */
    private List<MRef> expandFanout(Long pid, List<MRef> frontier, Map<String, ClassInfo> cache,
                                    GapCollector gaps,
                                    Map<String, CallChainResponse.ChainNode> nodeByUid,
                                    Map<String, CallChainResponse.ChainEdge> edgeByKey,
                                    Set<String> fanoutClasses, CallChainResponse resp) {
        List<MRef> ifaceMethods = new ArrayList<>();
        for (MRef m : frontier) {
            ClassInfo owner = cache.get(m.owner());
            if (owner != null && "INTERFACE".equals(owner.kind()) && m.signature() != null) {
                ifaceMethods.add(m);
            }
        }
        if (ifaceMethods.isEmpty()) {
            return frontier;
        }

        Map<String, Integer> ifaceDepth = new HashMap<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (MRef m : ifaceMethods) {
            ifaceDepth.putIfAbsent(m.owner(), m.depth());
            rows.add(Map.of("ifaceUid", m.owner(), "signature", m.signature()));
        }

        List<MRef> result = new ArrayList<>(frontier);
        Set<String> matched = new HashSet<>();
        Set<String> seenMethods = new HashSet<>();
        frontier.forEach(m -> seenMethods.add(m.uid()));

        try (Session session = driver.session()) {
            Result r = session.run(BATCH_FANOUT, Map.of("pid", pid, "rows", rows));
            while (r.hasNext()) {
                Record rec = r.next();
                String ifaceUid = rec.get("ifaceUid").asString();
                String toOwner = rec.get("toOwner").asString();
                String toMethod = rec.get("toMethod").asString();
                matched.add(ifaceUid);

                fanoutClasses.add(toOwner);
                loadClass(pid, toOwner, cache);
                ClassInfo impl = cache.get(toOwner);
                if (impl != null) {
                    int depth = ifaceDepth.getOrDefault(ifaceUid, 0);
                    CallChainResponse.ChainNode node = nodeByUid.get(toOwner);
                    if (node == null) {
                        node = addNode(resp, nodeByUid, impl, depth, true);
                    }
                    node.setViaFanout(true);
                    addEdge(edgeByKey, resp, ifaceUid, toOwner, "IMPLEMENTS", true);
                }

                if (seenMethods.add(toMethod)) {
                    result.add(new MRef(toMethod, toOwner,
                            rec.get("toSignature").asString(),
                            ifaceDepth.getOrDefault(ifaceUid, 0)));
                }
            }
        }

        for (MRef m : ifaceMethods) {
            ClassInfo iface = cache.get(m.owner());
            if (iface != null && !matched.contains(m.owner())) {
                gaps.add(iface.uid(), reasonFor(iface));
            }
        }
        return result;
    }

    /** API 契约模块 / client 包里的接口无实现 → 判为远程调用（启发式，靠命名约定） */
    private GapReason reasonFor(ClassInfo iface) {
        String module = iface.module() == null ? "" : iface.module().toLowerCase(Locale.ROOT);
        String pkg = iface.packageName() == null ? "" : iface.packageName().toLowerCase(Locale.ROOT);
        boolean apiModule = API_MODULE_SUFFIXES.stream().anyMatch(module::endsWith);
        boolean clientPkg = CLIENT_PACKAGE_MARKERS.stream().anyMatch(pkg::contains);
        return (apiModule || clientPkg) ? GapReason.REMOTE_SERVICE : GapReason.INTERFACE_NO_IMPLEMENTATION;
    }

    // ==================== 查询封装 ====================

    private List<MRef> startMethods(Long pid, String uid, String methodUid, int depth) {
        Map<String, Object> params = new HashMap<>();
        params.put("pid", pid);
        params.put("uid", uid);
        params.put("methodUid", methodUid);

        List<MRef> list = new ArrayList<>();
        try (Session session = driver.session()) {
            Result r = session.run(START_METHODS, params);
            while (r.hasNext()) {
                Record rec = r.next();
                list.add(new MRef(rec.get("uid").asString(), rec.get("owner").asString(),
                        rec.get("signature").asString(), depth));
            }
        }
        if (list.isEmpty()) {
            throw new BusinessException("该类下没有方法，或指定方法不存在: " + uid);
        }
        return list;
    }

    private List<HopRow> hop(Long pid, List<MRef> frontier) {
        Map<String, Object> params = new HashMap<>();
        params.put("pid", pid);
        params.put("uids", frontier.stream().map(MRef::uid).toList());

        List<HopRow> rows = new ArrayList<>();
        try (Session session = driver.session()) {
            Result r = session.run(BATCH_1HOP, params);
            while (r.hasNext()) {
                Record rec = r.next();
                rows.add(new HopRow(rec.get("fromMethod").asString(), rec.get("fromOwner").asString(),
                        rec.get("toMethod").asString(), rec.get("toOwner").asString(),
                        rec.get("toSignature").asString()));
            }
        }
        return rows;
    }

    private ClassInfo loadClass(Long pid, String uid, Map<String, ClassInfo> cache) {
        ClassInfo cached = cache.get(uid);
        if (cached != null) {
            return cached;
        }
        loadClasses(pid, List.of(uid), cache);
        return cache.get(uid);
    }

    private void loadClasses(Long pid, Iterable<String> uids, Map<String, ClassInfo> cache) {
        List<String> missing = new ArrayList<>();
        for (String u : uids) {
            if (!cache.containsKey(u)) {
                missing.add(u);
            }
        }
        if (missing.isEmpty()) {
            return;
        }
        try (Session session = driver.session()) {
            Result r = session.run(BATCH_CLASS_INFO, Map.of("pid", pid, "uids", missing));
            while (r.hasNext()) {
                Record rec = r.next();
                ClassInfo info = new ClassInfo(
                        rec.get("uid").asString(),
                        str(rec.get("name")),
                        str(rec.get("kind")),
                        str(rec.get("packageName")),
                        str(rec.get("filePath")),
                        str(rec.get("description")),
                        intOrNull(rec.get("startLine")),
                        intOrNull(rec.get("endLine")),
                        str(rec.get("module")));
                cache.put(info.uid(), info);
            }
        }
    }

    // ==================== 组装 ====================

    private CallChainResponse.ChainNode addNode(CallChainResponse resp,
                                                Map<String, CallChainResponse.ChainNode> byUid,
                                                ClassInfo info, int depth, boolean viaFanout) {
        CallChainResponse.ChainNode node = byUid.get(info.uid());
        if (node != null) {
            return node;
        }
        node = new CallChainResponse.ChainNode();
        node.setUid(info.uid());
        node.setName(info.name());
        node.setKind(info.kind());
        node.setModule(info.module());
        node.setDepth(depth);
        node.setViaFanout(viaFanout);
        node.setEdgeCount(0);
        byUid.put(info.uid(), node);
        resp.getNodes().add(node);
        return node;
    }

    private void addEdge(Map<String, CallChainResponse.ChainEdge> byKey, CallChainResponse resp,
                         String from, String to, String rel, boolean viaFanout) {
        String key = from + "|" + to;
        CallChainResponse.ChainEdge edge = byKey.get(key);
        if (edge == null) {
            edge = new CallChainResponse.ChainEdge();
            edge.setFrom(from);
            edge.setTo(to);
            edge.setRel(rel);
            edge.setViaFanout(viaFanout);
            byKey.put(key, edge);
            resp.getEdges().add(edge);
        }
        edge.setCallCount(edge.getCallCount() + 1);
    }

    private ClassNodeDTO toClassNode(ClassInfo info) {
        ClassNodeDTO d = new ClassNodeDTO();
        d.setQualifiedName(info.uid());
        d.setSimpleName(info.name());
        d.setPackageName(info.packageName());
        d.setKind(info.kind());
        d.setDescription(info.description());
        d.setFilePath(info.filePath());
        d.setStartLine(info.startLine());
        d.setEndLine(info.endLine());
        return d;
    }

    private static String str(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private static Integer intOrNull(Value v) {
        return v == null || v.isNull() ? null : (int) v.asLong();
    }

    // ==================== 内部小类型 ====================

    /** 一个方法引用：frontier 的元素 */
    private record MRef(String uid, String owner, String signature, int depth) {
    }

    private record HopRow(String fromMethod, String fromOwner, String toMethod,
                          String toOwner, String toSignature) {
    }

    private record ClassInfo(String uid, String name, String kind, String packageName,
                             String filePath, String description, Integer startLine,
                             Integer endLine, String module) {
    }

    private record Gap(String at, GapReason reason) {
    }

    /** 收集并去重 gap：同一个接口在不同层级出现时只报一次 */
    private static final class GapCollector {
        private final List<Gap> gaps = new ArrayList<>();
        private final Set<String> seen = new HashSet<>();

        void add(String at, GapReason reason) {
            if (seen.add(at + "|" + reason)) {
                gaps.add(new Gap(at, reason));
            }
        }

        List<CallChainResponse.Gap> list() {
            List<CallChainResponse.Gap> out = new ArrayList<>();
            for (Gap g : gaps) {
                CallChainResponse.Gap x = new CallChainResponse.Gap();
                x.setAt(g.at());
                x.setReason(g.reason().name());
                out.add(x);
            }
            return out;
        }
    }
}
