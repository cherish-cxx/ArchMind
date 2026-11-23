package com.example.archmind.service.domain;

import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

/**
 * 从 Neo4j 读回领域建模所需的基础图。
 * load 只负责跑查询并把每行转成 Map，装配逻辑集中在纯函数 assemble，便于无库单测。
 */
@Component
public class GraphSnapshotDao {

    private static final String CLASS_QUERY = """
            MATCH (c:Class {projectId:$pid})
            RETURN c.uid AS uid, c.name AS name, c.packageName AS packageName,
                   c.annotationNames AS annotationNames, c.interfaces AS interfaces,
                   c.superclass AS superclass
            ORDER BY c.uid
            """;

    private static final String CALL_QUERY = """
            MATCH (m:Method {projectId:$pid})-[r:CALLS]->(n:Method {projectId:$pid})
            RETURN m.uid AS from, n.uid AS to
            """;

    private static final String ENDPOINT_QUERY = """
            MATCH (e:Endpoint {projectId:$pid})
            RETURN e.uid AS uid, e.kind AS kind, e.handlerUid AS handlerUid
            ORDER BY e.uid
            """;

    private final Driver driver;

    public GraphSnapshotDao(Driver driver) {
        this.driver = driver;
    }

    /** 读取一个项目的类、CALLS 边与入口；类与入口按 uid 升序，调用边按 from 再按 to。 */
    public GraphSnapshot load(Long projectId) {
        try (Session session = driver.session()) {
            return session.executeRead(tx -> {
                List<Map<String, Object>> classRows =
                        tx.run(CLASS_QUERY, Map.of("pid", projectId)).list(record -> record.asMap());
                List<Map<String, Object>> callRows =
                        tx.run(CALL_QUERY, Map.of("pid", projectId)).list(record -> record.asMap());
                List<Map<String, Object>> endpointRows =
                        tx.run(ENDPOINT_QUERY, Map.of("pid", projectId)).list(record -> record.asMap());
                return assemble(classRows, callRows, endpointRows);
            });
        }
    }

    /**
     * 纯函数：把驱动返回的行装成快照。字段全部容错——
     * 缺失或类型不对时，字符串取空串、列表取空列表，并按 uid 排序保证输出稳定。
     */
    static GraphSnapshot assemble(List<Map<String, Object>> classRows,
                                  List<Map<String, Object>> callRows,
                                  List<Map<String, Object>> endpointRows) {
        List<GraphSnapshot.ClassSnapshot> classes = new ArrayList<>();
        for (Map<String, Object> row : nullSafe(classRows)) {
            classes.add(new GraphSnapshot.ClassSnapshot(
                    str(row, "uid"),
                    str(row, "name"),
                    str(row, "packageName"),
                    stringList(row, "annotationNames"),
                    stringList(row, "interfaces"),
                    str(row, "superclass")));
        }
        classes.sort(Comparator.comparing(GraphSnapshot.ClassSnapshot::uid));

        List<GraphSnapshot.CallEdgeSnapshot> calls = new ArrayList<>();
        for (Map<String, Object> row : nullSafe(callRows)) {
            calls.add(new GraphSnapshot.CallEdgeSnapshot(
                    str(row, "from"),
                    str(row, "to")));
        }
        calls.sort(Comparator.comparing(GraphSnapshot.CallEdgeSnapshot::fromMethodUid)
                .thenComparing(GraphSnapshot.CallEdgeSnapshot::toMethodUid));

        List<GraphSnapshot.EndpointSnapshot> endpoints = new ArrayList<>();
        for (Map<String, Object> row : nullSafe(endpointRows)) {
            endpoints.add(new GraphSnapshot.EndpointSnapshot(
                    str(row, "uid"),
                    str(row, "kind"),
                    str(row, "handlerUid")));
        }
        endpoints.sort(Comparator.comparing(GraphSnapshot.EndpointSnapshot::uid));

        return new GraphSnapshot(List.copyOf(classes), List.copyOf(calls), List.copyOf(endpoints));
    }

    private static String str(Map<String, Object> row, String key) {
        Object value = row == null ? null : row.get(key);
        return value instanceof String s ? s : "";
    }

    /** 列表属性容错：不是 List 就整体判空；元素不是字符串的直接丢弃，不抛异常。 */
    private static List<String> stringList(Map<String, Object> row, String key) {
        Object value = row == null ? null : row.get(key);
        if (!(value instanceof List<?> raw)) {
            return List.of();
        }
        List<String> out = new ArrayList<>(raw.size());
        for (Object element : raw) {
            if (element instanceof String s) {
                out.add(s);
            }
        }
        return List.copyOf(out);
    }

    private static <T> List<T> nullSafe(List<T> rows) {
        return rows == null ? List.of() : rows;
    }
}
