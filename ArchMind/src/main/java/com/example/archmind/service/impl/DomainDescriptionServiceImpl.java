package com.example.archmind.service.impl;

import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.domain.DomainDescriptionService;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 领域与能力描述：一条 Cypher 取「领域 → 能力 → 成员类（name, description）」，
 * 按领域分批（每批 ≤8 个领域）拼提示词交给模型，再按 uid 精确写回。
 *
 * <p>防脏数据两层：解析后只保留 <b>本次提示词里出现过的 uid</b>（模型编造的 uid 直接丢弃），
 * 写回 Cypher 又用 {@code MATCH} 而不是 {@code MERGE}——即使行漏过过滤，也只会写到已存在的节点，
 * 不会凭空造出领域或能力节点。</p>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class DomainDescriptionServiceImpl implements DomainDescriptionService {

    /** 单次调用最多喂给模型的领域数，避免 prompt 过长。 */
    private static final int MAX_AREAS_PER_BATCH = 8;
    /** 单个能力最多列出的成员类数，其余略过。 */
    private static final int MAX_MEMBERS_PER_CAPABILITY = 20;

    record DomainDescriptionBatch(List<AreaDesc> areas, List<CapDesc> capabilities) {
    }

    record AreaDesc(String uid, String description) {
    }

    record CapDesc(String uid, String description) {
    }

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final String LOAD = """
            MATCH (a:BusinessArea {projectId: $pid})
            OPTIONAL MATCH (cap:Capability {projectId: $pid})-[:IN_AREA]->(a)
            OPTIONAL MATCH (c:Class {projectId: $pid})-[:PART_OF_CAPABILITY]->(cap)
            RETURN a.uid AS areaUid, a.name AS areaName, a.kind AS kind,
                   cap.uid AS capUid, cap.name AS capName, cap.entryUid AS entryUid,
                   c.name AS className, coalesce(c.description, '') AS classDescription
            ORDER BY a.uid, cap.uid, c.name
            """;

    private static final String WRITE_AREA = """
            UNWIND $rows AS row
            MATCH (a:BusinessArea {projectId: $pid, uid: row.uid})
            SET a.description = row.description
            """;

    private static final String WRITE_CAPABILITY = """
            UNWIND $rows AS row
            MATCH (cap:Capability {projectId: $pid, uid: row.uid})
            SET cap.description = row.description
            """;

    private final ChatModel chatModel;
    private final Driver driver;

    @Override
    public void describe(Long projectId, ProgressReporter reporter) {
        reporter.report(0);
        List<AreaInfo> areas = load(projectId);
        reporter.report(50);
        if (areas.isEmpty()) {
            reporter.report(100);
            return;
        }

        for (int from = 0; from < areas.size(); from += MAX_AREAS_PER_BATCH) {
            List<AreaInfo> batchAreas =
                    areas.subList(from, Math.min(from + MAX_AREAS_PER_BATCH, areas.size()));
            Set<String> knownAreaUids = new LinkedHashSet<>();
            Set<String> knownCapUids = new LinkedHashSet<>();
            for (AreaInfo area : batchAreas) {
                knownAreaUids.add(area.uid());
                for (CapInfo cap : area.capabilities()) {
                    knownCapUids.add(cap.uid());
                }
            }

            DomainDescriptionBatch batch;
            try {
                batch = parse(callModel(buildPrompt(batchAreas)));
            } catch (Exception e) {
                // 描述是派生物：单批失败只丢这一批，不拖垮已完成的业务建模
                log.warn("生成领域描述失败 projectId={}, 领域数={}", projectId, batchAreas.size(), e);
                continue;
            }

            write(projectId, areaRows(batch, knownAreaUids), capabilityRows(batch, knownCapUids));
        }
        reporter.report(100);
    }

    /** 唯一的网络出口；测试可直接替换。 */
    String callModel(String prompt) {
        return ChatClient.create(chatModel).prompt(prompt).call().content();
    }

    /** 纯函数：解析失败返回空批次，丢弃描述为空的条目。 */
    DomainDescriptionBatch parse(String json) {
        if (json == null || json.isBlank()) {
            return emptyBatch();
        }
        try {
            return clean(MAPPER.readValue(json, DomainDescriptionBatch.class));
        } catch (Exception e) {
            log.warn("解析领域描述 JSON 失败", e);
            return emptyBatch();
        }
    }

    /** 只保留本次提示词里出现过的领域 uid，模型多给的 uid 一律丢弃。 */
    List<Map<String, Object>> areaRows(DomainDescriptionBatch batch, Set<String> knownAreaUids) {
        List<Map<String, Object>> rows = new ArrayList<>();
        if (batch == null || batch.areas() == null || knownAreaUids == null) {
            return rows;
        }
        for (AreaDesc area : batch.areas()) {
            String description = cleanDescription(area);
            if (description == null || !knownAreaUids.contains(area.uid())) {
                continue;
            }
            rows.add(row(area.uid(), description));
        }
        return rows;
    }

    /** 只保留本次提示词里出现过的能力 uid，模型多给的 uid 一律丢弃。 */
    List<Map<String, Object>> capabilityRows(DomainDescriptionBatch batch, Set<String> knownCapUids) {
        List<Map<String, Object>> rows = new ArrayList<>();
        if (batch == null || batch.capabilities() == null || knownCapUids == null) {
            return rows;
        }
        for (CapDesc cap : batch.capabilities()) {
            String description = cleanDescription(cap);
            if (description == null || !knownCapUids.contains(cap.uid())) {
                continue;
            }
            rows.add(row(cap.uid(), description));
        }
        return rows;
    }

    // ==================== 读图 ====================

    private List<AreaInfo> load(Long projectId) {
        Map<String, AreaBuilder> areas = new LinkedHashMap<>();
        try (Session session = driver.session()) {
            Result result = session.run(LOAD, Map.of("pid", projectId));
            while (result.hasNext()) {
                Record r = result.next();
                String areaUid = str(r.get("areaUid"));
                if (areaUid == null || areaUid.isBlank()) {
                    continue;
                }
                AreaBuilder area = areas.computeIfAbsent(areaUid,
                        key -> new AreaBuilder(key, str(r.get("areaName")), str(r.get("kind"))));

                String capUid = str(r.get("capUid"));
                if (capUid == null || capUid.isBlank()) {
                    continue;
                }
                CapBuilder cap = area.capabilities.computeIfAbsent(capUid,
                        key -> new CapBuilder(key, str(r.get("capName")), str(r.get("entryUid"))));

                String className = str(r.get("className"));
                if (className != null && !className.isBlank()) {
                    cap.members.putIfAbsent(className, str(r.get("classDescription")));
                }
            }
        }

        List<AreaInfo> out = new ArrayList<>();
        for (AreaBuilder area : areas.values()) {
            List<CapInfo> caps = new ArrayList<>();
            for (CapBuilder cap : area.capabilities.values()) {
                caps.add(new CapInfo(cap.uid, cap.name, cap.entryUid, cap.members));
            }
            out.add(new AreaInfo(area.uid, area.name, area.kind, caps));
        }
        return out;
    }

    // ==================== 拼提示词 ====================

    String buildPrompt(List<AreaInfo> areas) {
        StringBuilder sb = new StringBuilder();
        sb.append("你是代码理解助手。以下是一个 Java 项目中若干「业务领域」及其能力、成员类（类名 + 已有描述）。\n");
        sb.append("请为每个业务领域和每个能力各写 2–3 句中文描述，说明它承担的业务职责。\n");
        sb.append("不得发明代码里没有的能力；不得复述类名清单。\n");
        sb.append("uid 必须原样使用我给出的值，不要改动或缩短。\n");
        sb.append("只输出 JSON，不要输出解释或代码块标记，格式：");
        sb.append("{\"areas\":[{\"uid\":\"...\",\"description\":\"...\"}],")
                .append("\"capabilities\":[{\"uid\":\"...\",\"description\":\"...\"}]}\n\n");
        sb.append("=== 业务领域 ===\n");
        for (AreaInfo area : areas) {
            sb.append("领域 uid=").append(area.uid)
                    .append("（").append(area.kind == null ? "business" : area.kind).append("）");
            if (area.name != null && !area.name.isBlank()) {
                sb.append(" 名称=").append(area.name);
            }
            sb.append('\n');

            if (area.capabilities.isEmpty()) {
                sb.append("  （无能力）\n");
            }
            for (CapInfo cap : area.capabilities) {
                sb.append("  能力 uid=").append(cap.uid);
                if (cap.name != null && !cap.name.isBlank() && !cap.name.equals(cap.uid)) {
                    sb.append(" 名称=").append(cap.name);
                }
                if (cap.entryUid != null && !cap.entryUid.isBlank()) {
                    sb.append(" 入口=").append(cap.entryUid);
                }
                sb.append('\n');

                int shown = 0;
                for (Map.Entry<String, String> member : cap.members.entrySet()) {
                    if (shown >= MAX_MEMBERS_PER_CAPABILITY) {
                        sb.append("    （其余成员略）\n");
                        break;
                    }
                    String description = member.getValue();
                    sb.append("    成员类 ").append(member.getKey()).append("：")
                            .append(description == null || description.isBlank()
                                    ? "无描述" : description.trim())
                            .append('\n');
                    shown++;
                }
            }
        }
        return sb.toString();
    }

    // ==================== 写回 ====================

    private void write(Long projectId, List<Map<String, Object>> areaRows,
                       List<Map<String, Object>> capRows) {
        if (areaRows.isEmpty() && capRows.isEmpty()) {
            return;
        }
        try (Session session = driver.session()) {
            session.executeWrite(tx -> {
                if (!areaRows.isEmpty()) {
                    tx.run(WRITE_AREA, Map.of("pid", projectId, "rows", areaRows)).consume();
                }
                if (!capRows.isEmpty()) {
                    tx.run(WRITE_CAPABILITY, Map.of("pid", projectId, "rows", capRows)).consume();
                }
                return null;
            });
        }
    }

    // ==================== 工具 ====================

    private DomainDescriptionBatch clean(DomainDescriptionBatch raw) {
        List<AreaDesc> areas = new ArrayList<>();
        if (raw != null && raw.areas() != null) {
            for (AreaDesc area : raw.areas()) {
                String description = cleanDescription(area);
                if (description != null) {
                    areas.add(new AreaDesc(area.uid().trim(), description));
                }
            }
        }
        List<CapDesc> caps = new ArrayList<>();
        if (raw != null && raw.capabilities() != null) {
            for (CapDesc cap : raw.capabilities()) {
                String description = cleanDescription(cap);
                if (description != null) {
                    caps.add(new CapDesc(cap.uid().trim(), description));
                }
            }
        }
        return new DomainDescriptionBatch(areas, caps);
    }

    /** uid 缺失或描述全空白返回 null，否则返回 trim 后的描述。 */
    private String cleanDescription(AreaDesc area) {
        if (area == null || area.uid() == null || area.uid().isBlank()
                || area.description() == null || area.description().isBlank()) {
            return null;
        }
        return area.description().trim();
    }

    private String cleanDescription(CapDesc cap) {
        if (cap == null || cap.uid() == null || cap.uid().isBlank()
                || cap.description() == null || cap.description().isBlank()) {
            return null;
        }
        return cap.description().trim();
    }

    private Map<String, Object> row(String uid, String description) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("uid", uid);
        row.put("description", description);
        return row;
    }

    private String str(org.neo4j.driver.Value value) {
        return value == null || value.isNull() ? null : value.asString();
    }

    private DomainDescriptionBatch emptyBatch() {
        return new DomainDescriptionBatch(List.of(), List.of());
    }

    private record CapInfo(String uid, String name, String entryUid,
                           Map<String, String> members) {
    }

    private record AreaInfo(String uid, String name, String kind, List<CapInfo> capabilities) {
    }

    private static final class AreaBuilder {
        private final String uid;
        private final String name;
        private final String kind;
        private final Map<String, CapBuilder> capabilities = new LinkedHashMap<>();

        private AreaBuilder(String uid, String name, String kind) {
            this.uid = uid;
            this.name = name;
            this.kind = kind;
        }
    }

    private static final class CapBuilder {
        private final String uid;
        private final String name;
        private final String entryUid;
        private final Map<String, String> members = new LinkedHashMap<>();

        private CapBuilder(String uid, String name, String entryUid) {
            this.uid = uid;
            this.name = name;
            this.entryUid = entryUid;
        }
    }
}
