package com.example.archmind.service.impl;

import com.example.archmind.dto.response.CapabilityCardDTO;
import com.example.archmind.dto.response.CapabilityCardPageResponse;
import com.example.archmind.dto.response.CapabilityMemberDTO;
import com.example.archmind.service.ast.GraphRevService;
import com.example.archmind.service.domain.CapabilityCardQueryService;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Session;
import org.neo4j.driver.Values;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 读 DOMAIN 阶段落图的能力，按 uid 游标分页产出索引卡片。
 *
 * <p>查询只负责跑 Cypher 并取 {@code record.asMap()}；行到 DTO 的映射收在包级
 * 私有纯函数 {@link #assemble} 里，可脱库测试。</p>
 *
 * <p>能力 uid 是不透明字符串（可能含空格、{@code /}、{@code #}），全程原样传递，
 * 不 trim、不拆分、不放 path 段。</p>
 */
@Component
public class CapabilityCardQueryServiceImpl implements CapabilityCardQueryService {

    /** 每张卡片最多带多少个成员类；与成员查询里的 {@code [0..8]} 保持一致 */
    private static final int MEMBER_CAP = 8;

    /**
     * 能力分页 + 卡片摘要。先按 uid 做 keyset 分页，再取所属领域、成员计数与
     * 入口类型（Endpoint 与能力同 uid），每条能力一行。
     */
    static final String CAPABILITY_PAGE = """
            MATCH (cap:Capability:CodeNode {projectId:$pid})
            WHERE $afterUid IS NULL OR cap.uid > $afterUid
            WITH cap ORDER BY cap.uid LIMIT $limit
            MATCH (cap)-[:IN_AREA]->(a:BusinessArea {projectId:$pid})
            OPTIONAL MATCH (cap)<-[p:PART_OF_CAPABILITY]-(c:Class {projectId:$pid})
            WITH cap, a, count(c) AS memberCount
            OPTIONAL MATCH (e:Endpoint {projectId:$pid}) WHERE e.uid = cap.uid
            RETURN cap.uid AS uid, cap.name AS name, cap.description AS description,
                   a.uid AS areaUid, a.name AS areaName, a.description AS areaDescription,
                   cap.entryUid AS entryUid, head(collect(DISTINCT e.kind)) AS entryKind,
                   memberCount
            ORDER BY uid
            """;

    /** 本页能力的成员类，按 depth、再按 uid 排序，每能力最多 {@code MEMBER_CAP} 个。 */
    static final String CAPABILITY_MEMBERS = """
            MATCH (cap:Capability:CodeNode {projectId:$pid})<-[p:PART_OF_CAPABILITY]-(c:Class {projectId:$pid})
            WHERE cap.uid IN $uids
            WITH cap, c, p ORDER BY p.depth, c.uid
            RETURN cap.uid AS capUid,
                   collect({uid:c.uid, name:c.name, role:c.role, depth:p.depth})[0..8] AS members,
                   count(c) AS total
            """;

    private final Driver driver;

    private final GraphRevService graphRevService;

    public CapabilityCardQueryServiceImpl(Driver driver, GraphRevService graphRevService) {
        this.driver = driver;
        this.graphRevService = graphRevService;
    }

    @Override
    public CapabilityCardPageResponse listCapabilityCardsPage(Long projectId, String afterUid, int limit) {
        int size = Math.max(1, Math.min(limit, 200));
        try (Session session = driver.session()) {
            // 多取一条，用来判断是否还有下一页，省掉一次 count
            List<Map<String, Object>> rows = session.run(CAPABILITY_PAGE, Values.parameters(
                            "pid", projectId, "afterUid", afterUid, "limit", size + 1))
                    .list(Record::asMap);

            boolean hasMore = rows.size() > size;
            List<Map<String, Object>> caps =
                    hasMore ? new ArrayList<>(rows.subList(0, size)) : rows;
            String cursor = hasMore ? asString(caps.get(caps.size() - 1).get("uid")) : null;

            Map<String, List<Map<String, Object>>> members = new HashMap<>();
            if (!caps.isEmpty()) {
                List<String> uids = caps.stream()
                        .map(row -> asString(row.get("uid")))
                        .toList();
                for (Map<String, Object> row : session.run(CAPABILITY_MEMBERS,
                                Values.parameters("pid", projectId, "uids", uids))
                        .list(Record::asMap)) {
                    members.put(asString(row.get("capUid")), asMapList(row.get("members")));
                }
            }
            return assemble(caps, members, graphRevService.currentRev(projectId), cursor);
        }
    }

    /**
     * 行 → 卡片页：能力按 uid 排序，成员按 depth、再按 uid 排序并截到 {@link #MEMBER_CAP}；
     * {@code membersTruncated} 用全量成员数判定，不能只看本页成员。
     */
    static CapabilityCardPageResponse assemble(List<Map<String, Object>> caps,
                                               Map<String, List<Map<String, Object>>> members,
                                               long rev, String cursor) {
        List<Map<String, Object>> sorted = new ArrayList<>(caps);
        sorted.sort(Comparator.comparing(row -> asString(row.get("uid"))));

        List<CapabilityCardDTO> items = new ArrayList<>();
        for (Map<String, Object> row : sorted) {
            String uid = asString(row.get("uid"));
            List<CapabilityMemberDTO> memberDtos = new ArrayList<>();
            for (Map<String, Object> member : members.getOrDefault(uid, List.of())) {
                CapabilityMemberDTO dto = new CapabilityMemberDTO();
                dto.setUid(asString(member.get("uid")));
                dto.setName(asString(member.get("name")));
                dto.setRole(asString(member.get("role")));
                dto.setDepth(asInt(member.get("depth")));
                memberDtos.add(dto);
            }
            memberDtos.sort(Comparator.comparingInt(CapabilityMemberDTO::getDepth)
                    .thenComparing(CapabilityMemberDTO::getUid));
            if (memberDtos.size() > MEMBER_CAP) {
                memberDtos = new ArrayList<>(memberDtos.subList(0, MEMBER_CAP));
            }

            int memberCount = asInt(row.get("memberCount"));
            CapabilityCardDTO card = new CapabilityCardDTO();
            card.setUid(uid);
            card.setName(asString(row.get("name")));
            card.setDescription(asString(row.get("description")));
            card.setAreaUid(asString(row.get("areaUid")));
            card.setAreaName(asString(row.get("areaName")));
            card.setAreaDescription(asString(row.get("areaDescription")));
            card.setEntryUid(asString(row.get("entryUid")));
            card.setEntryKind(asString(row.get("entryKind")));
            card.setMemberCount(memberCount);
            card.setMembers(memberDtos);
            card.setMembersTruncated(memberCount > memberDtos.size());
            items.add(card);
        }

        CapabilityCardPageResponse page = new CapabilityCardPageResponse();
        page.setRev(rev);
        page.setItems(items);
        page.setNextCursor(cursor);
        return page;
    }

    private static String asString(Object value) {
        return value == null ? null : value.toString();
    }

    private static int asInt(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }

    private static List<Map<String, Object>> asMapList(Object value) {
        if (!(value instanceof Iterable<?> iterable)) {
            return List.of();
        }
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Object item : iterable) {
            if (item instanceof Map<?, ?> map) {
                Map<String, Object> row = new HashMap<>();
                map.forEach((key, val) -> row.put(String.valueOf(key), val));
                rows.add(row);
            }
        }
        return rows;
    }
}
