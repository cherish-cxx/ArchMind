package com.example.archmind.service.impl;

import com.example.archmind.common.constant.GraphPredicates;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassPageResponse;
import com.example.archmind.dto.response.ClassRelationsResponse;
import com.example.archmind.dto.response.RelatedClassDTO;
import com.example.archmind.service.ast.CodeGraphQueryService;
import com.example.archmind.service.ast.RelationDirection;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.neo4j.driver.Values;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 用底层 Driver 直查 Neo4j。每条边 6 个方向各一条 Cypher，Java 侧按「目标类」聚合成类级关联。
 * 方法↔类的归属靠 :Method 节点的 owner 属性（与 CodeGraphNeo4jWriter 写入保持一致）。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class CodeGraphQueryServiceImpl implements CodeGraphQueryService {

    private static final String LIST_CLASSES =
            "MATCH (c:Class {projectId: $pid}) RETURN c ORDER BY c.packageName, c.name LIMIT $limit";

    /**
     * keyset 分页：按 uid 升序，游标是上一页最后一个 uid。
     * 只返回顶层类 —— 与描述补齐的 describable 口径一致，否则索引数量校验会多出内部类。
     */
    private static final String LIST_CLASSES_PAGE = """
            MATCH (c:Class {projectId: $pid})
            WHERE ($afterUid IS NULL OR c.uid > $afterUid)
              AND %s
            RETURN c ORDER BY c.uid LIMIT $limit
            """.formatted(GraphPredicates.TOP_LEVEL);

    private static final String FIND_CENTER =
            "MATCH (c:Class {projectId: $pid, uid: $uid}) RETURN c";

    // 上游：谁调我
    private static final String CALLERS = """
            MATCH (m:Method {projectId: $pid})-[r:CALLS]->(t:Method {projectId: $pid, owner: $uid})
            MATCH (cc:Class {projectId: $pid, uid: m.owner})
            RETURN cc, m.signature AS fromSig, t.signature AS toSig
            """;

    // 上游：谁依赖我
    private static final String DEPENDENTS = """
            MATCH (c:Class {projectId: $pid})-[r:DEPENDS]->(t:Class {projectId: $pid, uid: $uid})
            RETURN c AS cc, r.via AS via
            """;

    // 上游：谁继承/实现我
    private static final String SUBCLASSES = """
            MATCH (c:Class {projectId: $pid})-[r:EXTENDS|IMPLEMENTS]->(t:Class {projectId: $pid, uid: $uid})
            RETURN c AS cc, type(r) AS rel
            """;

    // 下游：我调谁
    private static final String CALLEES = """
            MATCH (m:Method {projectId: $pid, owner: $uid})-[r:CALLS]->(t:Method {projectId: $pid})
            MATCH (cc:Class {projectId: $pid, uid: t.owner})
            RETURN cc, m.signature AS fromSig, t.signature AS toSig
            """;

    // 下游：我依赖谁
    private static final String DEPENDENCIES = """
            MATCH (c:Class {projectId: $pid, uid: $uid})-[r:DEPENDS]->(t:Class {projectId: $pid})
            RETURN t AS cc, r.via AS via
            """;

    // 下游：我继承/实现谁
    private static final String PARENTS = """
            MATCH (c:Class {projectId: $pid, uid: $uid})-[r:EXTENDS|IMPLEMENTS]->(t:Class {projectId: $pid})
            RETURN t AS cc, type(r) AS rel
            """;

    // P4.3 网关的 uiCommands 弱校验：这批 uid 里哪些真的存在。一次 IN，不逐条查。
    private static final String EXISTING_UIDS =
            "MATCH (c:Class {projectId: $pid}) WHERE c.uid IN $uids RETURN c.uid AS uid";

    /** 图版本号。核心类集合按 (projectId, rev) 缓存，重分析后自动失效 */
    private static final String GRAPH_REV =
            "MATCH (g:GraphMeta {projectId: $pid}) RETURN g.rev AS rev";

    /**
     * 核心类 = 关联度 Top N。与 ClassDescriptionServiceImpl 的分析阶段选类同一口径，
     * 保证「谁有核心标记」和「谁被分析阶段描述过」不会各说各话。
     */
    private static final String TOP_CORE_CLASSES = """
            MATCH (c:Class {projectId: $pid})
            WITH c,
              size([(c)-[:EXTENDS|IMPLEMENTS|DEPENDS]->(:Class {projectId: $pid}) | 1]) AS outRel,
              size([(:Class {projectId: $pid})-[:EXTENDS|IMPLEMENTS|DEPENDS]->(c) | 1]) AS inRel,
              size([(m:Method {projectId: $pid, owner: c.uid})-[:CALLS]->(:Method {projectId: $pid}) | 1]) AS outCalls,
              size([(:Method {projectId: $pid})-[:CALLS]->(m:Method {projectId: $pid, owner: c.uid}) | 1]) AS inCalls
            RETURN c.uid AS uid
            ORDER BY outRel + inRel + outCalls + inCalls DESC
            LIMIT $n
            """;

    private final Driver driver;

    private record CoreCache(long rev, List<String> uids) {
    }

    private final ConcurrentHashMap<Long, CoreCache> coreCache = new ConcurrentHashMap<>();

    @Override
    public List<ClassNodeDTO> listClasses(Long projectId) {
        return listClasses(projectId, Integer.MAX_VALUE);
    }

    /**
     *
     * @param projectId 项目Id
     * @param limit
     * 归根结底是通过Neo4j的session建立通信，cypher查询内容
     * @return 所有被查询的列表加添加到list里
     */
    @Override
    public List<ClassNodeDTO> listClasses(Long projectId, int limit) {
        Set<String> coreUids = Set.copyOf(coreClassUids(projectId));
        try (Session session = driver.session()) {
            Result result = session.run(LIST_CLASSES, Map.of("pid", projectId, "limit", limit));
            List<ClassNodeDTO> list = new ArrayList<>();
            while (result.hasNext()) {
                list.add(toClassNode(result.next().get("c"), coreUids));
            }
            return list;
        }
    }

    @Override
    public ClassPageResponse listClassesPage(Long projectId, String afterUid, int limit) {
        int size = Math.max(1, Math.min(limit, 200));
        Set<String> coreUids = Set.copyOf(coreClassUids(projectId));

        List<ClassNodeDTO> items = new ArrayList<>();
        try (Session session = driver.session()) {
            // 多取一条，用来判断是否还有下一页，省掉一次 count
            Result result = session.run(LIST_CLASSES_PAGE, Values.parameters(
                    "pid", projectId, "afterUid", afterUid, "limit", size + 1));
            while (result.hasNext()) {
                items.add(toClassNode(result.next().get("c"), coreUids));
            }
        }

        String nextCursor = null;
        if (items.size() > size) {
            items = new ArrayList<>(items.subList(0, size));
            nextCursor = items.get(items.size() - 1).getQualifiedName();
        }

        ClassPageResponse resp = new ClassPageResponse();
        resp.setItems(items);
        resp.setNextCursor(nextCursor);
        resp.setRev(graphRev(projectId));
        return resp;
    }

    @Override
    public ClassRelationsResponse classRelations(Long projectId, String uid) {
        return classRelations(projectId, uid, RelationDirection.BOTH);
    }

    /**
     *
     * @param projectId 项目id
     * @param uid 类id
     * @param direction  描述
     * @return 返回关系内容响应
     * 实际流程：
     * 1.建立通信
     * 2.用projectId，和uid两个参数确定Neo4j的节点
     * 3.根据查询的方向，用Cypher查找用户所选哟参数内容并记录到up/down中
     */
    @Override
    public ClassRelationsResponse classRelations(Long projectId, String uid, RelationDirection direction) {
        Set<String> coreUids = Set.copyOf(coreClassUids(projectId));
//        先建立Neo4j的通信
        try (Session session = driver.session()) {
//           将参数放以键值的方式存储到变量params
            Map<String, Object> params = Map.of("pid", projectId, "uid", uid);

            ClassNodeDTO center = null;
//            将params与cypher一同合成查询语句，进行查询
            Result centerRes = session.run(FIND_CENTER, params);

            if (centerRes.hasNext()) {
                center = toClassNode(centerRes.next().get("c"), coreUids);
            }
            if (center == null) {
                throw new BusinessException("类不存在: " + uid);
            }

            ClassRelationsResponse resp = new ClassRelationsResponse();
            resp.setCenter(center);

            // 只跑需要的那一侧：原来固定跑 6 条 Cypher，单方向调用会白跑 5 条
            if (direction != RelationDirection.DOWN) {
                ClassRelationsResponse.Upstream up = new ClassRelationsResponse.Upstream();
                up.setCallers(aggregateCalls(session.run(CALLERS, params), coreUids));
                up.setDependents(aggregateDepends(session.run(DEPENDENTS, params), coreUids));
                up.setSubclasses(aggregateInherit(session.run(SUBCLASSES, params), coreUids));
                resp.setUpstream(up);
            }
            if (direction != RelationDirection.UP) {
                ClassRelationsResponse.Downstream down = new ClassRelationsResponse.Downstream();
                down.setCallees(aggregateCalls(session.run(CALLEES, params), coreUids));
                down.setDependencies(aggregateDepends(session.run(DEPENDENCIES, params), coreUids));
                down.setParents(aggregateInherit(session.run(PARENTS, params), coreUids));
                resp.setDownstream(down);
            }

            return resp;
        }
    }

    /**
     * {@inheritDoc}
     *
     * <p>空集合直接短路，不发查询 —— 澄清轮 / 空响应没有 uiCommand，
     * 不该为它白跑一次 Neo4j。</p>
     */
    @Override
    public Set<String> existingClassUids(Long projectId, Collection<String> uids) {
        if (uids == null || uids.isEmpty()) {
            return Set.of();
        }
        try (Session session = driver.session()) {
            Result result = session.run(
                    EXISTING_UIDS, Map.of("pid", projectId, "uids", List.copyOf(uids)));
            Set<String> found = new HashSet<>();
            while (result.hasNext()) {
                found.add(result.next().get("uid").asString());
            }
            return found;
        }
    }

    // ==================== 聚合：把行按「目标类 uid」归并 ====================

    /** CALLS 方向：同一对 (来源类→目标类) 可能多条方法级边，聚成一条，evidence 逐条累加 */
    private List<RelatedClassDTO> aggregateCalls(Result result, Set<String> coreUids) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            RelatedClassDTO rel = map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc"), coreUids));
                x.setRelationType("CALLS");
                x.setEvidence(new ArrayList<>());
                return x;
            });
            rel.getEvidence().add(strOrNull(r.get("fromSig")) + " → " + strOrNull(r.get("toSig")));
        }
        return new ArrayList<>(map.values());
    }

    /** DEPENDS 方向：via 作为证据（field / signature） */
    private List<RelatedClassDTO> aggregateDepends(Result result, Set<String> coreUids) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            RelatedClassDTO rel = map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc"), coreUids));
                x.setRelationType("DEPENDS");
                x.setEvidence(new ArrayList<>());
                return x;
            });
            rel.getEvidence().add(strOrNull(r.get("via")));
        }
        return new ArrayList<>(map.values());
    }

    /** EXTENDS / IMPLEMENTS 方向：关系类型即 rel，无额外证据 */
    private List<RelatedClassDTO> aggregateInherit(Result result, Set<String> coreUids) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            String type = strOrNull(r.get("rel"));
            map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc"), coreUids));
                x.setRelationType(type);
                x.setEvidence(new ArrayList<>());
                return x;
            });
        }
        return new ArrayList<>(map.values());
    }

    // ==================== 核心类 ====================

    @Override
    public List<String> coreClassUids(Long projectId) {
        return coreClassUids(projectId, 20);
    }

    /**
     * 核心类 uid 列表，按关联度降序。按 (projectId, rev) 缓存 ——
     * 每次请求只多一条 rev 查询，避免把同一份排名反复算。
     */
    @Override
    public List<String> coreClassUids(Long projectId, int limit) {
        long rev = graphRev(projectId);
        CoreCache cached = coreCache.get(projectId);
        if (cached != null && cached.rev() == rev) {
            return cached.uids();
        }
        List<String> uids = new ArrayList<>();
        try (Session session = driver.session()) {
            Result result = session.run(TOP_CORE_CLASSES, Map.of("pid", projectId, "n", limit));
            while (result.hasNext()) {
                Value uid = result.next().get("uid");
                if (uid != null && !uid.isNull()) {
                    uids.add(uid.asString());
                }
            }
        }
        List<String> frozen = List.copyOf(uids);
        coreCache.put(projectId, new CoreCache(rev, frozen));
        return frozen;
    }

    private long graphRev(Long projectId) {
        try (Session session = driver.session()) {
            Result result = session.run(GRAPH_REV, Map.of("pid", projectId));
            if (!result.hasNext()) {
                return 0L;
            }
            Value rev = result.next().get("rev");
            return rev == null || rev.isNull() ? 0L : rev.asLong(0L);
        }
    }

    // ==================== 工具 ====================

    private ClassNodeDTO toClassNode(Value v, Set<String> coreUids) {
        ClassNodeDTO d = new ClassNodeDTO();
        String uid = v.get("uid").asString();
        d.setQualifiedName(uid);
        d.setSimpleName(v.get("name").asString());
        d.setPackageName(v.get("packageName").asString());
        d.setKind(v.get("kind").asString());
        d.setDescription(strOrNull(v.get("description")));
        d.setIsAbstract(boolOrNull(v.get("isAbstract")));
        d.setCore(coreUids.contains(uid));
        d.setFilePath(strOrNull(v.get("filePath")));
        d.setStartLine(intOrNull(v.get("startLine")));
        d.setEndLine(intOrNull(v.get("endLine")));
        return d;
    }

    private String strOrNull(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private Boolean boolOrNull(Value v) {
        return v == null || v.isNull() ? null : v.asBoolean();
    }

    private Integer intOrNull(Value v) {
        return v == null || v.isNull() ? null : (int) v.asLong();
    }
}
