package com.example.archmind.service.impl;

import com.example.archmind.dto.response.ArchitectureLayersResponse;
import com.example.archmind.service.domain.ArchitectureLayerResolver;
import com.example.archmind.service.domain.ArchitectureQueryService;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Session;
import org.neo4j.driver.Values;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 读类角色、包名与领域归属，再聚合成六层架构视图。查询只负责跑 Cypher 并取
 * {@code record.asMap()}；行到 resolver 输入的映射收在包级私有纯函数里，可脱库测试。
 */
@Component
public class ArchitectureQueryServiceImpl implements ArchitectureQueryService {

    /**
     * 全部类及其可选领域。OPTIONAL MATCH 保证未归属领域的类也会返回，
     * 此时 areaUid/areaName 为 null，由 resolver 归入「未归属领域」而不是消失。
     */
    private static final String CLASS_ROWS = """
            MATCH (c:Class {projectId:$pid})
            OPTIONAL MATCH (c)-[:BELONGS_TO]->(a:BusinessArea {projectId:$pid})
            RETURN c.uid AS uid, c.name AS name, c.role AS role, c.packageName AS packageName,
                   a.uid AS areaUid, a.name AS areaName
            """;

    /**
     * 类对之间的方法级调用次数。只统计两端 owner 不同的调用：同层判定与
     * 同层过滤交给 resolver，这里先把类内自调用排除。
     */
    private static final String CALL_ROWS = """
            MATCH (m1:Method {projectId:$pid})-[:CALLS]->(m2:Method {projectId:$pid})
            WHERE m1.owner <> m2.owner
            RETURN m1.owner AS fromUid, m2.owner AS toUid, count(*) AS count
            """;

    private static final int MAX_CLASSES_PER_GROUP = 12;
    private static final int MAX_SAMPLES_PER_EDGE = 5;

    private final Driver driver;

    public ArchitectureQueryServiceImpl(Driver driver) {
        this.driver = driver;
    }

    @Override
    public ArchitectureLayersResponse architecture(Long projectId) {
        try (Session session = driver.session()) {
            List<Map<String, Object>> classRows =
                    session.run(CLASS_ROWS, Values.parameters("pid", projectId))
                            .list(Record::asMap);
            List<Map<String, Object>> callRows =
                    session.run(CALL_ROWS, Values.parameters("pid", projectId))
                            .list(Record::asMap);
            return assemble(classRows, callRows);
        }
    }

    /** Map 行 → resolver 输入；null/缺字段不抛，字符串原样保留。 */
    static ArchitectureLayersResponse assemble(List<? extends Map<String, ?>> classes,
                                               List<? extends Map<String, ?>> calls) {
        List<ArchitectureLayerResolver.ClassRow> classRows = new ArrayList<>();
        if (classes != null) {
            for (Map<String, ?> row : classes) {
                if (row == null) {
                    continue;
                }
                classRows.add(new ArchitectureLayerResolver.ClassRow(
                        asString(row.get("uid")),
                        asString(row.get("name")),
                        asString(row.get("role")),
                        asString(row.get("packageName")),
                        asString(row.get("areaUid")),
                        asString(row.get("areaName"))));
            }
        }

        List<ArchitectureLayerResolver.PairCount> pairCounts = new ArrayList<>();
        if (calls != null) {
            for (Map<String, ?> row : calls) {
                if (row == null) {
                    continue;
                }
                pairCounts.add(new ArchitectureLayerResolver.PairCount(
                        asString(row.get("fromUid")),
                        asString(row.get("toUid")),
                        asInt(row.get("count"))));
            }
        }

        return new ArchitectureLayerResolver().resolve(classRows, pairCounts,
                MAX_CLASSES_PER_GROUP, MAX_SAMPLES_PER_EDGE);
    }

    private static String asString(Object value) {
        return value == null ? null : value.toString();
    }

    private static int asInt(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }
}
