package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassDescriptionBatch;
import com.example.archmind.service.ClassDescriptionService;
import com.example.archmind.service.ProgressReporter;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * 核心类简要描述：从 Neo4j 图按「关联度」取 Top N 核心类 → 组装骨架 prompt →
 * LLM 一次批量生成一句话简介 → 写回 :Class 节点的 description 属性。
 *
 * 关联度 = DEPENDS/EXTENDS/IMPLEMENTS 出入边 + 方法级 CALLS 出入边（按 owner 聚合到类）。
 * 核心类筛选只在已落图（astStage 之后）的项目上有意义；owner 字段旧图没有，缺失时调用维度自动退化为 0。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class ClassDescriptionServiceImpl implements ClassDescriptionService {

    private static final int DEFAULT_TOP_N = 20;
    /** prompt 里每个类最多列出的方法签名数，避免 prompt 过长 */
    private static final int MAX_METHODS_PER_CLASS = 8;

    private static final String TOP_CORE_CLASSES = """
            MATCH (c:Class {projectId: $pid})
            WITH c,
              size([(c)-[:EXTENDS|IMPLEMENTS|DEPENDS]->(:Class {projectId: $pid}) | 1]) AS outRel,
              size([(:Class {projectId: $pid})-[:EXTENDS|IMPLEMENTS|DEPENDS]->(c) | 1]) AS inRel,
              size([(m:Method {projectId: $pid, owner: c.uid})-[:CALLS]->(:Method {projectId: $pid}) | 1]) AS outCalls,
              size([(:Method {projectId: $pid})-[:CALLS]->(m:Method {projectId: $pid, owner: c.uid}) | 1]) AS inCalls
            RETURN c.uid AS uid, c.name AS name, c.packageName AS pkg, c.kind AS kind,
              outRel + inRel + outCalls + inCalls AS score
            ORDER BY score DESC
            LIMIT $n
            """;

    private static final String SKELETON = """
            MATCH (c:Class {projectId: $pid})
            WHERE c.uid IN $uids
            OPTIONAL MATCH (c)-[:EXTENDS|IMPLEMENTS]->(p:Class {projectId: $pid})
            OPTIONAL MATCH (m:Method {projectId: $pid, owner: c.uid})
            RETURN c.uid AS uid, collect(DISTINCT p.name) AS parents, collect(DISTINCT m.signature) AS methods
            """;

    private static final String UPDATE_DESCRIPTION = """
            UNWIND $rows AS row
            MATCH (c:Class {projectId: row.projectId, uid: row.uid})
            SET c.description = row.description
            """;

    private final Driver driver;
    private final ChatModel chatModel;

    @Override
    public void describeCoreClasses(Long projectId, ProgressReporter reporter) {
        List<CoreClass> cores = queryCoreClasses(projectId, DEFAULT_TOP_N);
        reporter.report(20);
        if (cores.isEmpty()) {
            log.info("无核心类可描述 projectId={}", projectId);
            reporter.report(100);
            return;
        }

        String prompt = buildPrompt(cores);
        reporter.report(40);

        ClassDescriptionBatch batch = callLlm(prompt);
        reporter.report(80);

        writeBack(projectId, batch);
        reporter.report(100);
        log.info("核心类描述完成 projectId={}, 候选={}, 写入={}",
                projectId, cores.size(), countOf(batch));
    }

    // ==================== 查询核心类 ====================

    List<CoreClass> queryCoreClasses(Long projectId, int limit) {
        try (Session session = driver.session()) {
            List<CoreClass> cores = new ArrayList<>();
            Result top = session.run(TOP_CORE_CLASSES, Map.of("pid", projectId, "n", limit));
            while (top.hasNext()) {
                Record r = top.next();
                cores.add(new CoreClass(
                        r.get("uid").asString(),
                        r.get("name").asString(),
                        r.get("pkg").asString(),
                        r.get("kind").asString(),
                        List.of(), List.of()));
            }
            if (cores.isEmpty()) {
                return cores;
            }

            List<String> uids = cores.stream().map(CoreClass::qualifiedName).toList();
            Result skel = session.run(SKELETON, Map.of("pid", projectId, "uids", uids));
            Map<String, List<String>> parentsMap = new HashMap<>();
            Map<String, List<String>> methodsMap = new HashMap<>();
            while (skel.hasNext()) {
                Record r = skel.next();
                String uid = r.get("uid").asString();
                parentsMap.put(uid, strings(r.get("parents")));
                methodsMap.put(uid, strings(r.get("methods")));
            }

            List<CoreClass> filled = new ArrayList<>();
            for (CoreClass c : cores) {
                filled.add(new CoreClass(c.qualifiedName(), c.name(), c.pkg(), c.kind(),
                        parentsMap.getOrDefault(c.qualifiedName(), List.of()),
                        methodsMap.getOrDefault(c.qualifiedName(), List.of())));
            }
            return filled;
        }
    }

    // ==================== 组装 prompt + 调 LLM ====================

    String buildPrompt(List<CoreClass> cores) {
        StringBuilder sb = new StringBuilder();
        sb.append("你是代码理解助手。以下是某个 Java 项目中「核心类」的骨架（类名、继承/实现、方法签名）。\n");
        sb.append("请为每个核心类生成一句中文简介（不超过 30 字），说明它在项目中承担的业务职责，不要复述技术细节。\n");
        sb.append("qualifiedName 必须原样使用我给出的完整全限定名，不要改动或缩短。\n");
        sb.append("只输出 JSON，不要输出解释或代码块标记，格式：");
        sb.append("{\"items\":[{\"qualifiedName\":\"...\",\"description\":\"...\"}]}\n\n");
        sb.append("=== 核心类骨架 ===\n");
        for (int i = 0; i < cores.size(); i++) {
            CoreClass c = cores.get(i);
            sb.append(i + 1).append(". ").append(c.qualifiedName());
            sb.append(" (").append(c.kind()).append(')');
            if (!c.parents().isEmpty()) {
                sb.append(" | 继承/实现: ").append(String.join(", ", c.parents()));
            }
            if (!c.methods().isEmpty()) {
                int n = Math.min(MAX_METHODS_PER_CLASS, c.methods().size());
                sb.append(" | 方法: ").append(String.join(", ", c.methods().subList(0, n)));
            }
            sb.append('\n');
        }
        return sb.toString();
    }

    private ClassDescriptionBatch callLlm(String prompt) {
        try {
            ChatClient chatClient = ChatClient.create(chatModel);
            return chatClient.prompt(prompt).call().entity(ClassDescriptionBatch.class);
        } catch (Exception e) {
            log.error("调用 LLM 生成核心类描述失败", e);
            throw new BusinessException("调用 LLM 失败: " + e.getMessage());
        }
    }

    // ==================== 写回 ====================

    private void writeBack(Long projectId, ClassDescriptionBatch batch) {
        if (batch == null || batch.getItems() == null || batch.getItems().isEmpty()) {
            return;
        }
        List<Map<String, Object>> rows = new ArrayList<>();
        for (ClassDescriptionBatch.Item item : batch.getItems()) {
            if (item.getQualifiedName() == null || item.getDescription() == null) {
                continue;
            }
            Map<String, Object> row = new HashMap<>();
            row.put("projectId", projectId);
            row.put("uid", item.getQualifiedName());
            row.put("description", item.getDescription());
            rows.add(row);
        }
        if (rows.isEmpty()) {
            return;
        }
        try (Session session = driver.session()) {
            session.executeWrite(tx -> {
                tx.run(UPDATE_DESCRIPTION, Map.of("rows", rows)).consume();
                return null;
            });
        }
    }

    // ==================== 工具 ====================

    private List<String> strings(Value v) {
        if (v == null || v.isNull()) {
            return List.of();
        }
        return v.asList(Value::asString).stream().filter(Objects::nonNull).toList();
    }

    private int countOf(ClassDescriptionBatch batch) {
        return batch == null || batch.getItems() == null ? 0 : batch.getItems().size();
    }

    record CoreClass(String qualifiedName, String name, String pkg, String kind,
                     List<String> parents, List<String> methods) {
    }
}
