package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;
import com.example.archmind.dto.response.RelatedClassDTO;
import com.example.archmind.service.ast.CodeGraphQueryService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 用底层 Driver 直查 Neo4j。每条边 6 个方向各一条 Cypher，Java 侧按「目标类」聚合成类级关联。
 * 方法↔类的归属靠 :Method 节点的 owner 属性（与 CodeGraphNeo4jWriter 写入保持一致）。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class CodeGraphQueryServiceImpl implements CodeGraphQueryService {

    private static final String LIST_CLASSES =
            "MATCH (c:Class {projectId: $pid}) RETURN c ORDER BY c.packageName, c.name";

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

    private final Driver driver;

    @Override
    public List<ClassNodeDTO> listClasses(Long projectId) {
        try (Session session = driver.session()) {
            Result result = session.run(LIST_CLASSES, Map.of("pid", projectId));
            List<ClassNodeDTO> list = new ArrayList<>();
            while (result.hasNext()) {
                list.add(toClassNode(result.next().get("c")));
            }
            return list;
        }
    }

    @Override
    public ClassRelationsResponse classRelations(Long projectId, String uid) {
        try (Session session = driver.session()) {
            Map<String, Object> params = Map.of("pid", projectId, "uid", uid);

            ClassNodeDTO center = null;
            Result centerRes = session.run(FIND_CENTER, params);
            if (centerRes.hasNext()) {
                center = toClassNode(centerRes.next().get("c"));
            }
            if (center == null) {
                throw new BusinessException("类不存在: " + uid);
            }

            ClassRelationsResponse resp = new ClassRelationsResponse();
            resp.setCenter(center);

            ClassRelationsResponse.Upstream up = new ClassRelationsResponse.Upstream();
            up.setCallers(aggregateCalls(session.run(CALLERS, params)));
            up.setDependents(aggregateDepends(session.run(DEPENDENTS, params)));
            up.setSubclasses(aggregateInherit(session.run(SUBCLASSES, params)));
            resp.setUpstream(up);

            ClassRelationsResponse.Downstream down = new ClassRelationsResponse.Downstream();
            down.setCallees(aggregateCalls(session.run(CALLEES, params)));
            down.setDependencies(aggregateDepends(session.run(DEPENDENCIES, params)));
            down.setParents(aggregateInherit(session.run(PARENTS, params)));
            resp.setDownstream(down);

            return resp;
        }
    }

    // ==================== 聚合：把行按「目标类 uid」归并 ====================

    /** CALLS 方向：同一对 (来源类→目标类) 可能多条方法级边，聚成一条，evidence 逐条累加 */
    private List<RelatedClassDTO> aggregateCalls(Result result) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            RelatedClassDTO rel = map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc")));
                x.setRelationType("CALLS");
                x.setEvidence(new ArrayList<>());
                return x;
            });
            rel.getEvidence().add(strOrNull(r.get("fromSig")) + " → " + strOrNull(r.get("toSig")));
        }
        return new ArrayList<>(map.values());
    }

    /** DEPENDS 方向：via 作为证据（field / signature） */
    private List<RelatedClassDTO> aggregateDepends(Result result) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            RelatedClassDTO rel = map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc")));
                x.setRelationType("DEPENDS");
                x.setEvidence(new ArrayList<>());
                return x;
            });
            rel.getEvidence().add(strOrNull(r.get("via")));
        }
        return new ArrayList<>(map.values());
    }

    /** EXTENDS / IMPLEMENTS 方向：关系类型即 rel，无额外证据 */
    private List<RelatedClassDTO> aggregateInherit(Result result) {
        Map<String, RelatedClassDTO> map = new LinkedHashMap<>();
        while (result.hasNext()) {
            Record r = result.next();
            String type = strOrNull(r.get("rel"));
            map.computeIfAbsent(r.get("cc").get("uid").asString(), k -> {
                RelatedClassDTO x = new RelatedClassDTO();
                x.setClazz(toClassNode(r.get("cc")));
                x.setRelationType(type);
                x.setEvidence(new ArrayList<>());
                return x;
            });
        }
        return new ArrayList<>(map.values());
    }

    // ==================== 工具 ====================

    private ClassNodeDTO toClassNode(Value v) {
        ClassNodeDTO d = new ClassNodeDTO();
        d.setQualifiedName(v.get("uid").asString());
        d.setSimpleName(v.get("name").asString());
        d.setPackageName(v.get("packageName").asString());
        d.setKind(v.get("kind").asString());
        d.setDescription(strOrNull(v.get("description")));
        d.setFilePath(strOrNull(v.get("filePath")));
        d.setStartLine(intOrNull(v.get("startLine")));
        d.setEndLine(intOrNull(v.get("endLine")));
        return d;
    }

    private String strOrNull(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private Integer intOrNull(Value v) {
        return v == null || v.isNull() ? null : (int) v.asLong();
    }
}
