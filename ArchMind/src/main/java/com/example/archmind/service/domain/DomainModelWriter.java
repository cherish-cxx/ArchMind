package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.AreaCall;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * 把领域模型写进 Neo4j。先按项目清掉上一次的 BusinessArea / Capability 派生节点，
 * 再按 WRITE_ORDER 逐条 UNWIND 批写，避免重建后残留孤儿领域与能力。
 */
@Component
public class DomainModelWriter {

    /** 六组待写行；plan() 返回后测试可直接检查行内容，不连库。 */
    record DomainWritePlan(List<Map<String, Object>> areas,
                           List<Map<String, Object>> classAreas,
                           List<Map<String, Object>> roles,
                           List<Map<String, Object>> capabilities,
                           List<Map<String, Object>> members,
                           List<Map<String, Object>> areaCalls) {
    }

    /** 执行顺序：前两条必须是删除，否则旧派生节点会残留。 */
    static final List<String> WRITE_ORDER = List.of(
            "MATCH (a:BusinessArea {projectId:$pid}) DETACH DELETE a",
            "MATCH (c:Capability {projectId:$pid}) DETACH DELETE c",

            "UNWIND $rows AS row "
                    + "MERGE (a:BusinessArea:CodeNode {projectId:$pid, uid:row.uid}) "
                    + "SET a.name = row.name, a.kind = row.kind",

            "UNWIND $rows AS row "
                    + "MATCH (c:Class {projectId:$pid, uid:row.classUid}) "
                    + "MATCH (a:BusinessArea {projectId:$pid, uid:row.areaUid}) "
                    + "MERGE (c)-[:BELONGS_TO]->(a)",

            "UNWIND $rows AS row "
                    + "MATCH (c:Class {projectId:$pid, uid:row.classUid}) "
                    + "SET c.role = row.role, c.roleBasis = row.basis",

            "UNWIND $rows AS row "
                    + "MERGE (cap:Capability:CodeNode {projectId:$pid, uid:row.uid}) "
                    + "SET cap.name = row.name, cap.areaUid = row.areaUid, cap.entryUid = row.entryUid",

            "UNWIND $rows AS row "
                    + "MATCH (a:BusinessArea {projectId:$pid, uid:row.areaUid}) "
                    + "MATCH (cap:Capability {projectId:$pid, uid:row.uid}) "
                    + "MERGE (cap)-[:IN_AREA]->(a)",

            "UNWIND $rows AS row "
                    + "MATCH (c:Class {projectId:$pid, uid:row.uid}) "
                    + "MATCH (cap:Capability {projectId:$pid, uid:row.capabilityUid}) "
                    + "MERGE (c)-[m:PART_OF_CAPABILITY]->(cap) "
                    + "SET m.depth = row.depth",

            "UNWIND $rows AS row "
                    + "MATCH (from:BusinessArea {projectId:$pid, uid:row.fromAreaUid}) "
                    + "MATCH (to:BusinessArea {projectId:$pid, uid:row.toAreaUid}) "
                    + "MERGE (from)-[e:AREA_CALLS]->(to) "
                    + "SET e.count = coalesce(e.count, 0) + 1, "
                    + "e.fromClassUids = coalesce(e.fromClassUids, []) + "
                    + "  CASE WHEN row.fromClassUid IS NULL THEN [] "
                    + "       WHEN row.fromClassUid IN coalesce(e.fromClassUids, []) THEN [] "
                    + "       ELSE [row.fromClassUid] END, "
                    + "e.toUid = row.toClassUid"
    );

    private final Driver driver;

    public DomainModelWriter(Driver driver) {
        this.driver = driver;
    }

    /** 纯函数：只把模型摊平成行，不触碰数据库。 */
    DomainWritePlan plan(Long projectId, DomainModel model) {
        List<Map<String, Object>> areas = new ArrayList<>();
        for (BusinessArea area : model.areas()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("projectId", projectId);
            row.put("uid", area.uid());
            row.put("name", area.name());
            row.put("kind", area.kind());
            areas.add(row);
        }

        List<Map<String, Object>> classAreas = new ArrayList<>();
        for (Map.Entry<String, String> entry : model.classToArea().entrySet()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("projectId", projectId);
            row.put("classUid", entry.getKey());
            row.put("areaUid", entry.getValue());
            classAreas.add(row);
        }

        List<Map<String, Object>> roles = new ArrayList<>();
        for (Map.Entry<String, Classified> entry : model.roles().entrySet()) {
            Classified classified = entry.getValue();
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("projectId", projectId);
            row.put("classUid", entry.getKey());
            row.put("role", classified == null ? null : classified.role().name());
            row.put("basis", classified == null ? null : classified.basis());
            roles.add(row);
        }

        List<Map<String, Object>> capabilities = new ArrayList<>();
        List<Map<String, Object>> members = new ArrayList<>();
        for (Capability capability : model.capabilities()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("projectId", projectId);
            row.put("uid", capability.uid());
            row.put("name", capability.name());
            row.put("areaUid", capability.areaUid());
            row.put("entryUid", capability.entryUid());
            capabilities.add(row);

            for (Capability.Member member : capability.members()) {
                Map<String, Object> memberRow = new LinkedHashMap<>();
                memberRow.put("projectId", projectId);
                memberRow.put("uid", member.classUid());
                memberRow.put("capabilityUid", capability.uid());
                memberRow.put("depth", member.depth());
                members.add(memberRow);
            }
        }

        List<Map<String, Object>> areaCalls = new ArrayList<>();
        for (AreaCall call : model.areaCalls()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("projectId", projectId);
            row.put("fromAreaUid", call.fromAreaUid());
            row.put("toAreaUid", call.toAreaUid());
            row.put("fromClassUid", call.fromClassUid());
            row.put("toClassUid", call.toClassUid());
            areaCalls.add(row);
        }

        return new DomainWritePlan(areas, classAreas, roles, capabilities, members, areaCalls);
    }

    /** 按 WRITE_ORDER 在同一事务里逐条执行；前两条删除旧 BusinessArea / Capability。 */
    public void write(Long projectId, DomainModel model) {
        Objects.requireNonNull(driver, "driver");
        DomainWritePlan plan = plan(projectId, model);

        List<List<Map<String, Object>>> rowsPerStatement = List.of(
                List.of(),
                List.of(),
                plan.areas(),
                plan.classAreas(),
                plan.roles(),
                plan.capabilities(),
                plan.capabilities(),
                plan.members(),
                plan.areaCalls()
        );

        try (Session session = driver.session()) {
            session.executeWrite(tx -> {
                for (int i = 0; i < WRITE_ORDER.size(); i++) {
                    tx.run(WRITE_ORDER.get(i),
                            Map.of("pid", projectId, "rows", rowsPerStatement.get(i)));
                }
                return null;
            });
        }
    }
}
