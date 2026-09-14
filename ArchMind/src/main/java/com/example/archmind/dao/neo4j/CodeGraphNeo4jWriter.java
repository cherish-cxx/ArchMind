package com.example.archmind.dao.neo4j;

import com.example.archmind.model.ast.EdgeType;
import com.example.archmind.model.ast.ParsedClass;
import com.example.archmind.model.ast.ParsedEdge;
import com.example.archmind.model.ast.ParsedFile;
import com.example.archmind.model.ast.ParsedMethod;
import com.example.archmind.model.ast.ParsedProject;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.neo4j.driver.TransactionContext;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 把解析结果写进 Neo4j 的 Cypher 执行器。
 * 用底层 Driver 直写（绕开 SDN 高层封装），UNWIND 批写、MERGE 幂等。
 * 节点统一 label :CodeNode 便于按项目清理；类型区分用 :Class / :Method。
 * 唯一键统一为 (projectId, uid)：Class 的 uid = 全限定名，Method 的 uid = owner#signature，
 * 正好与 RelationResolver 产边的 source/target 一一对应。
 */
@Component
@Slf4j
public class CodeGraphNeo4jWriter {

    private static final int BATCH_SIZE = 500;

    private static final String CONSTRAINT_CLASS =
            "CREATE CONSTRAINT class_uid IF NOT EXISTS FOR (c:Class) REQUIRE (c.projectId, c.uid) IS UNIQUE";
    private static final String CONSTRAINT_METHOD =
            "CREATE CONSTRAINT method_uid IF NOT EXISTS FOR (m:Method) REQUIRE (m.projectId, m.uid) IS UNIQUE";
    private static final String INDEX_PROJECT =
            "CREATE INDEX code_node_project IF NOT EXISTS FOR (n:CodeNode) ON (n.projectId)";

    private static final String CLEAR =
            "MATCH (n:CodeNode {projectId: $pid}) DETACH DELETE n";

    private static final String MERGE_CLASS = """
            UNWIND $rows AS row
            MERGE (c:Class:CodeNode {projectId: row.projectId, uid: row.uid})
            SET c.name = row.name, c.kind = row.kind, c.packageName = row.packageName,
                c.fileId = row.fileId, c.filePath = row.filePath, c.startLine = row.startLine,
                c.endLine = row.endLine, c.visibility = row.visibility, c.isAbstract = row.isAbstract
            """;

    private static final String MERGE_METHOD = """
            UNWIND $rows AS row
            MERGE (m:Method:CodeNode {projectId: row.projectId, uid: row.uid})
            SET m.qualifiedName = row.qualifiedName, m.signature = row.signature, m.name = row.name,
                m.returnType = row.returnType, m.fileId = row.fileId, m.startLine = row.startLine,
                m.endLine = row.endLine, m.visibility = row.visibility,
                m.isStatic = row.isStatic, m.isConstructor = row.isConstructor
            """;

    private static final String MERGE_CALLS = """
            UNWIND $rows AS row
            MATCH (a:Method {projectId: row.projectId, uid: row.source})
            MATCH (b:Method {projectId: row.projectId, uid: row.target})
            MERGE (a)-[e:CALLS]->(b)
            SET e.count = row.count
            """;

    private static final String MERGE_EXTENDS = """
            UNWIND $rows AS row
            MATCH (a:Class {projectId: row.projectId, uid: row.source})
            MATCH (b:Class {projectId: row.projectId, uid: row.target})
            MERGE (a)-[e:EXTENDS]->(b)
            """;

    private static final String MERGE_IMPLEMENTS = """
            UNWIND $rows AS row
            MATCH (a:Class {projectId: row.projectId, uid: row.source})
            MATCH (b:Class {projectId: row.projectId, uid: row.target})
            MERGE (a)-[e:IMPLEMENTS]->(b)
            """;

    private static final String MERGE_DEPENDS = """
            UNWIND $rows AS row
            MATCH (a:Class {projectId: row.projectId, uid: row.source})
            MATCH (b:Class {projectId: row.projectId, uid: row.target})
            MERGE (a)-[e:DEPENDS]->(b)
            SET e.via = row.via
            """;

    private final Driver driver;

    public CodeGraphNeo4jWriter(Driver driver) {
        this.driver = driver;
    }

    /** 清旧图 → 写节点 → 写边，整个流程在一个写事务里。重跑安全（先清后写 + MERGE）。 */
    public void replaceProjectGraph(Long projectId, ParsedProject project) {
        ensureConstraints();
        try (Session session = driver.session()) {
            session.executeWrite(tx -> {
                tx.run(CLEAR, Map.of("pid", projectId));
                writeInBatches(tx, MERGE_CLASS, classRows(projectId, project));
                writeInBatches(tx, MERGE_METHOD, methodRows(projectId, project));
                writeEdges(tx, projectId, project.getEdges());
                return null;
            });
        }
        log.info("Neo4j 图写入完成 projectId={}, classes={}, methods={}, edges={}",
                projectId, project.getFiles().stream().mapToInt(f -> f.getClasses().size()).sum(),
                project.getFiles().stream()
                        .flatMap(f -> f.getClasses().stream())
                        .mapToInt(c -> c.getMethods().size()).sum(),
                project.getEdges().size());
    }

    /**
     * 约束/索引：IF NOT EXISTS 幂等，重复执行无害，保证 Neo4j 被清空重建后也能自愈。
     * 用 UNIQUE 而非 NODE KEY——NODE KEY 是 Neo4j 企业版专有，社区版建它直接报错。
     * UNIQUE 已足够：MERGE 按 (projectId, uid) 匹配，唯一约束保证同键只有一个节点。
     */
    private void ensureConstraints() {
        try (Session session = driver.session()) {
            session.run(CONSTRAINT_CLASS).consume();
            session.run(CONSTRAINT_METHOD).consume();
            session.run(INDEX_PROJECT).consume();
        }
    }

    private void writeEdges(TransactionContext tx, Long projectId, List<ParsedEdge> edges) {
        Map<EdgeType, List<Map<String, Object>>> grouped = new HashMap<>();
        for (ParsedEdge e : edges) {
            Map<String, Object> row = new HashMap<>();
            row.put("projectId", projectId);
            row.put("source", e.source());
            row.put("target", e.target());
            row.put("count", e.count());
            row.put("via", e.via());
            grouped.computeIfAbsent(e.type(), k -> new ArrayList<>()).add(row);
        }

        writeInBatches(tx, MERGE_CALLS, grouped.getOrDefault(EdgeType.CALLS, List.of()));
        writeInBatches(tx, MERGE_EXTENDS, grouped.getOrDefault(EdgeType.EXTENDS, List.of()));
        writeInBatches(tx, MERGE_IMPLEMENTS, grouped.getOrDefault(EdgeType.IMPLEMENTS, List.of()));
        writeInBatches(tx, MERGE_DEPENDS, grouped.getOrDefault(EdgeType.DEPENDS, List.of()));
    }

    private List<Map<String, Object>> classRows(Long projectId, ParsedProject project) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (ParsedFile pf : project.getFiles()) {
            for (ParsedClass pc : pf.getClasses()) {
                Map<String, Object> row = new HashMap<>();
                row.put("projectId", projectId);
                row.put("uid", pc.getQualifiedName());
                row.put("name", pc.getSimpleName());
                row.put("kind", pc.getKind().name());
                row.put("packageName", pc.getPackageName());
                row.put("fileId", pc.getFileId());
                row.put("filePath", pc.getFilePath());
                row.put("startLine", pc.getStartLine());
                row.put("endLine", pc.getEndLine());
                row.put("visibility", pc.getVisibility());
                row.put("isAbstract", pc.isAbstract());
                rows.add(row);
            }
        }
        return rows;
    }

    private List<Map<String, Object>> methodRows(Long projectId, ParsedProject project) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (ParsedFile pf : project.getFiles()) {
            for (ParsedClass pc : pf.getClasses()) {
                for (ParsedMethod pm : pc.getMethods()) {
                    Map<String, Object> row = new HashMap<>();
                    row.put("projectId", projectId);
                    row.put("uid", methodUid(pm));
                    row.put("qualifiedName", pm.getQualifiedName());
                    row.put("signature", pm.getSignature());
                    row.put("name", pm.getName());
                    row.put("returnType", pm.getReturnType());
                    row.put("fileId", pc.getFileId());
                    row.put("startLine", pm.getStartLine());
                    row.put("endLine", pm.getEndLine());
                    row.put("visibility", pm.getVisibility());
                    row.put("isStatic", pm.isStatic());
                    row.put("isConstructor", pm.isConstructor());
                    rows.add(row);
                }
            }
        }
        return rows;
    }

    /** 与 RelationResolver.methodUid 保持一致：owner#signature，重载不撞键 */
    private String methodUid(ParsedMethod pm) {
        return pm.getOwnerQualifiedName() + "#" + pm.getSignature();
    }

    private void writeInBatches(TransactionContext tx, String cypher, List<Map<String, Object>> rows) {
        for (int from = 0; from < rows.size(); from += BATCH_SIZE) {
            int to = Math.min(from + BATCH_SIZE, rows.size());
            tx.run(cypher, Map.of("rows", rows.subList(from, to))).consume();
        }
    }
}
