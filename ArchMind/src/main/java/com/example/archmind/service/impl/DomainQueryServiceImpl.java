package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.AreaCallDto;
import com.example.archmind.dto.response.AreaDto;
import com.example.archmind.dto.response.CapabilityDetailResponse;
import com.example.archmind.dto.response.CapabilityDto;
import com.example.archmind.dto.response.DomainMapResponse;
import com.example.archmind.dto.response.EdgeDto;
import com.example.archmind.dto.response.MemberDto;
import com.example.archmind.service.domain.DomainQueryService;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Session;
import org.neo4j.driver.Values;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 读 DOMAIN 阶段落图的领域与能力。查询只负责跑 Cypher 并取 {@code record.asMap()}；
 * 行到 DTO 的映射全部收在包级私有纯函数里，可脱库测试。
 */
@Component
public class DomainQueryServiceImpl implements DomainQueryService {

    /**
     * 一条查询摊平「领域 → 能力 → 成员计数」。用 OPTIONAL MATCH 保证没有能力的领域
     * （基础设施、或能力生成失败的领域）也会返回一行，此时 capUid 为 null。
     * classCount 走 BELONGS_TO 单独计数：一个类可属于多个能力，能力成员数相加会重复。
     */
    private static final String DOMAIN_MAP = """
            MATCH (a:BusinessArea {projectId:$pid})
            CALL {
              WITH a
              MATCH (ac:Class {projectId:$pid})-[:BELONGS_TO]->(a)
              RETURN count(ac) AS classCount
            }
            OPTIONAL MATCH (cap:Capability {projectId:$pid})-[:IN_AREA]->(a)
            OPTIONAL MATCH (c:Class {projectId:$pid})-[:PART_OF_CAPABILITY]->(cap)
            RETURN a.uid AS areaUid, a.name AS areaName, a.kind AS kind, a.description AS description,
                   classCount,
                   cap.uid AS capUid, cap.name AS capName, cap.description AS capDescription,
                   cap.entryUid AS entryUid,
                   count(c) AS memberCount
            ORDER BY a.uid, cap.uid
            """;

    private static final String CAPABILITY = """
            MATCH (cap:Capability {projectId:$pid, uid:$uid})
            RETURN cap.uid AS uid, cap.name AS name, cap.description AS description, cap.entryUid AS entryUid
            """;

    private static final String CAPABILITY_MEMBERS = """
            MATCH (c:Class {projectId:$pid})-[r:PART_OF_CAPABILITY]->(cap:Capability {projectId:$pid, uid:$uid})
            RETURN c.uid AS uid, c.name AS name, r.depth AS depth, c.role AS role
            """;

    /**
     * 成员类之间的调用边：先取能力成员，再把方法级 CALLS 收窄到两端都在成员内的边，
     * 按 owner 类去重。链路的诚实来源只有这里，不能让前端按 depth 猜。
     */
    private static final String CAPABILITY_EDGES = """
            MATCH (cap:Capability {projectId:$pid, uid:$uid})<-[:PART_OF_CAPABILITY]-(c:Class {projectId:$pid})
            WITH cap, collect(DISTINCT c.uid) AS memberUids
            MATCH (m1:Method {projectId:$pid})-[:CALLS]->(m2:Method {projectId:$pid})
            WHERE m1.owner IN memberUids AND m2.owner IN memberUids AND m1.owner <> m2.owner
            RETURN DISTINCT m1.owner AS fromUid, m2.owner AS toUid
            """;

    /** 本区域出去的全部跨领域调用。能力经 IN_AREA 找到所属领域，再取该领域的 AREA_CALLS。 */
    private static final String AREA_CALLS = """
            MATCH (cap:Capability {projectId:$pid, uid:$uid})-[:IN_AREA]->(a:BusinessArea {projectId:$pid})
            MATCH (a)-[e:AREA_CALLS]->(to:BusinessArea {projectId:$pid})
            RETURN to.uid AS toAreaUid, to.name AS toAreaName, e.count AS count,
                   coalesce(e.fromClassUids, []) AS fromClassUids
            """;

    private final Driver driver;

    public DomainQueryServiceImpl(Driver driver) {
        this.driver = driver;
    }

    @Override
    public DomainMapResponse domainMap(Long projectId) {
        try (Session session = driver.session()) {
            List<Map<String, Object>> rows = new ArrayList<>();
            for (Record record : session.run(DOMAIN_MAP, Values.parameters("pid", projectId)).list()) {
                rows.add(record.asMap());
            }
            return assembleMap(rows);
        }
    }

    @Override
    public CapabilityDetailResponse capability(Long projectId, String uid) {
        try (Session session = driver.session()) {
            List<Map<String, Object>> capRows =
                    session.run(CAPABILITY, Values.parameters("pid", projectId, "uid", uid))
                            .list(Record::asMap);
            if (capRows.isEmpty()) {
                throw new BusinessException("能力不存在: " + uid);
            }
            List<Map<String, Object>> memberRows =
                    session.run(CAPABILITY_MEMBERS, Values.parameters("pid", projectId, "uid", uid))
                            .list(Record::asMap);
            List<Map<String, Object>> edgeRows =
                    session.run(CAPABILITY_EDGES, Values.parameters("pid", projectId, "uid", uid))
                            .list(Record::asMap);
            List<Map<String, Object>> areaCallRows =
                    session.run(AREA_CALLS, Values.parameters("pid", projectId, "uid", uid))
                            .list(Record::asMap);
            return assembleDetail(capRows.get(0), memberRows, edgeRows, areaCallRows);
        }
    }

    /** 按 areaUid 分组；capUid 为空的行只产出区域，不产出能力。 */
    static DomainMapResponse assembleMap(List<Map<String, Object>> rows) {
        Map<String, AreaAccumulator> areas = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String areaUid = asString(row.get("areaUid"));
            AreaAccumulator area = areas.computeIfAbsent(areaUid, key -> new AreaAccumulator(
                    asString(row.get("areaName")), asString(row.get("kind")), asString(row.get("description")),
                    asInt(row.get("classCount"))));
            String capUid = asString(row.get("capUid"));
            if (capUid != null) {
                area.capabilities.add(new CapabilityDto(capUid,
                        orDefault(row.get("capName"), capUid),
                        asString(row.get("capDescription")),
                        asString(row.get("entryUid")),
                        asInt(row.get("memberCount"))));
            }
        }

        List<AreaDto> result = new ArrayList<>();
        areas.entrySet().stream()
                .sorted(Map.Entry.comparingByKey())
                .forEach(entry -> {
                    List<CapabilityDto> capabilities = new ArrayList<>(entry.getValue().capabilities);
                    capabilities.sort(Comparator.comparing(CapabilityDto::uid));
                    result.add(new AreaDto(entry.getKey(), entry.getValue().name, entry.getValue().kind,
                            entry.getValue().description, entry.getValue().classCount, List.copyOf(capabilities)));
                });
        return new DomainMapResponse(List.copyOf(result));
    }

    /** 成员按 depth、再按 uid 排序；边按两端 uid 排序；跨领域调用按目标领域 uid 排序。 */
    static CapabilityDetailResponse assembleDetail(Map<String, Object> capRow,
                                                   List<Map<String, Object>> memberRows,
                                                   List<Map<String, Object>> edgeRows,
                                                   List<Map<String, Object>> areaCallRows) {
        List<MemberDto> members = new ArrayList<>();
        for (Map<String, Object> row : memberRows) {
            members.add(new MemberDto(asString(row.get("uid")), asString(row.get("name")),
                    asInt(row.get("depth")), asString(row.get("role"))));
        }
        members.sort(Comparator.comparingInt(MemberDto::depth).thenComparing(MemberDto::uid));

        List<EdgeDto> edges = new ArrayList<>();
        for (Map<String, Object> row : edgeRows) {
            String from = asString(row.get("fromUid"));
            String to = asString(row.get("toUid"));
            if (from != null && to != null) {
                edges.add(new EdgeDto(from, to));
            }
        }
        edges.sort(Comparator.comparing(EdgeDto::fromUid).thenComparing(EdgeDto::toUid));

        List<AreaCallDto> areaCalls = new ArrayList<>();
        for (Map<String, Object> row : areaCallRows) {
            areaCalls.add(new AreaCallDto(asString(row.get("toAreaUid")), asString(row.get("toAreaName")),
                    asInt(row.get("count")), asStringList(row.get("fromClassUids"))));
        }
        areaCalls.sort(Comparator.comparing(AreaCallDto::toAreaUid));

        CapabilityDto capability = new CapabilityDto(asString(capRow.get("uid")),
                asString(capRow.get("name")), asString(capRow.get("description")),
                asString(capRow.get("entryUid")), members.size());
        return new CapabilityDetailResponse(capability, List.copyOf(members), List.copyOf(edges),
                List.copyOf(areaCalls));
    }

    private static String asString(Object value) {
        return value == null ? null : value.toString();
    }

    private static String orDefault(Object value, String fallback) {
        String text = asString(value);
        return text == null ? fallback : text;
    }

    private static int asInt(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }

    private static List<String> asStringList(Object value) {
        if (!(value instanceof Iterable<?> iterable)) {
            return List.of();
        }
        List<String> values = new ArrayList<>();
        for (Object item : iterable) {
            if (item != null) {
                values.add(item.toString());
            }
        }
        return List.copyOf(values);
    }

    /** 同一领域内累积能力；首行决定领域元信息。 */
    private static final class AreaAccumulator {
        private final String name;
        private final String kind;
        private final String description;
        private final int classCount;
        private final List<CapabilityDto> capabilities = new ArrayList<>();

        private AreaAccumulator(String name, String kind, String description, int classCount) {
            this.name = name;
            this.kind = kind;
            this.description = description;
            this.classCount = classCount;
        }
    }
}
