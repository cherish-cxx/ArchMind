package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassDescriptionBatch;
import com.example.archmind.dto.response.ClassDescriptionsResponse;
import com.example.archmind.service.ClassDescriptionService;
import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.ast.CodeGraphQueryService;
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
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 类简要描述：分析阶段批量生成核心类；运行期按画布内容按需补齐其余实现类。
 *
 * <p>两条路径共用同一套骨架查询和写回逻辑，区别只在「选谁」和「是否跳过已有描述」。
 * 核心类的判定统一走 {@link CodeGraphQueryService#coreClassUids(Long, int)}，
 * 避免「分析阶段选了 20 个」和「前端标的核心」各说各话。</p>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class ClassDescriptionServiceImpl implements ClassDescriptionService {

    private static final int DEFAULT_TOP_N = 20;
    /** prompt 里每个类最多列出的方法签名数，避免 prompt 过长 */
    private static final int MAX_METHODS_PER_CLASS = 8;
    /** 按需描述单次上限：再多就拆成多批，避免一次 LLM 调用超时 */
    private static final int MAX_ON_DEMAND_BATCH = 40;
    /** LLM 失败后的冷却时间，防止前端重试把失败的请求打成风暴 */
    private static final long FAILURE_COOLDOWN_MS = 30_000L;

    private static final String SKELETON = """
            MATCH (c:Class {projectId: $pid})
            WHERE c.uid IN $uids
            OPTIONAL MATCH (c)-[:EXTENDS|IMPLEMENTS]->(p:Class {projectId: $pid})
            OPTIONAL MATCH (m:Method {projectId: $pid, owner: c.uid})
            RETURN c.uid AS uid, c.name AS name, c.packageName AS pkg, c.kind AS kind,
                   coalesce(c.isAbstract, false) AS isAbstract,
                   coalesce(c.description, '') AS description,
                   collect(DISTINCT p.name) AS parents,
                   collect(DISTINCT m.signature) AS methods
            """;

    private static final String UPDATE_DESCRIPTION = """
            UNWIND $rows AS row
            MATCH (c:Class {projectId: row.projectId, uid: row.uid})
            SET c.description = row.description
            """;

    private final Driver driver;
    private final ChatModel chatModel;
    private final CodeGraphQueryService codeGraphQueryService;

    /** 项目级串行：同一项目的按需描述不并发调用 LLM */
    private final ConcurrentHashMap<Long, Object> describeLocks = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<Long, Long> cooldownUntil = new ConcurrentHashMap<>();

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

    @Override
    public ClassDescriptionsResponse describeClasses(Long projectId, List<String> uids, boolean force) {
        ClassDescriptionsResponse resp = new ClassDescriptionsResponse();
        resp.setItems(new ArrayList<>());
        resp.setSkipped(new ArrayList<>());
        if (uids == null || uids.isEmpty()) {
            return resp;
        }

        LinkedHashSet<String> distinct = new LinkedHashSet<>();
        for (String uid : uids) {
            if (uid != null && !uid.isBlank()) {
                distinct.add(uid);
            }
        }
        List<String> requested = new ArrayList<>(distinct);
        if (requested.isEmpty()) {
            return resp;
        }

        if (requested.size() > MAX_ON_DEMAND_BATCH) {
            resp.setRemaining(requested.size() - MAX_ON_DEMAND_BATCH);
            requested = new ArrayList<>(requested.subList(0, MAX_ON_DEMAND_BATCH));
        }

        Long until = cooldownUntil.get(projectId);
        if (until != null && until > System.currentTimeMillis()) {
            resp.setSkipped(requested);
            resp.setNote("描述服务刚刚失败，请稍后重试");
            return resp;
        }

        Object lock = describeLocks.computeIfAbsent(projectId, k -> new Object());
        synchronized (lock) {
            return describeLocked(projectId, requested, force, resp);
        }
    }

    private ClassDescriptionsResponse describeLocked(Long projectId, List<String> requested,
                                                     boolean force, ClassDescriptionsResponse resp) {
        List<Skeleton> pending;
        try {
            pending = loadPending(projectId, requested, force);
        } catch (Exception e) {
            log.error("读取类骨架失败 projectId={}", projectId, e);
            resp.setSkipped(requested);
            resp.setNote("读取类信息失败");
            return resp;
        }

        Set<String> pendingUids = new LinkedHashSet<>();
        for (Skeleton s : pending) {
            pendingUids.add(s.uid());
        }
        // 不存在 / 已有描述 / 类型不合适 —— 都算 skipped，前端不再重试
        for (String uid : requested) {
            if (!pendingUids.contains(uid)) {
                resp.getSkipped().add(uid);
            }
        }
        if (pending.isEmpty()) {
            return resp;
        }

        Set<String> coreUids = Set.copyOf(codeGraphQueryService.coreClassUids(projectId));
        ClassDescriptionBatch batch;
        try {
            batch = callLlm(buildOnDemandPrompt(pending));
        } catch (Exception e) {
            log.error("按需生成类描述失败 projectId={}, count={}", projectId, pending.size(), e);
            cooldownUntil.put(projectId, System.currentTimeMillis() + FAILURE_COOLDOWN_MS);
            resp.setSkipped(requested);
            resp.setNote("描述生成失败，请稍后重试");
            return resp;
        }

        List<ClassDescriptionsResponse.Item> items =
                writeBackOnDemand(projectId, batch, pendingUids, coreUids);
        Set<String> written = new HashSet<>();
        for (ClassDescriptionsResponse.Item item : items) {
            written.add(item.getUid());
        }
        for (String uid : pendingUids) {
            if (!written.contains(uid)) {
                resp.getSkipped().add(uid);
            }
        }
        resp.setItems(items);
        return resp;
    }

    // ==================== 查询 ====================

    List<CoreClass> queryCoreClasses(Long projectId, int limit) {
        List<String> uids = codeGraphQueryService.coreClassUids(projectId, limit);
        if (uids.isEmpty()) {
            return List.of();
        }

        Map<String, CoreClass> byUid = new LinkedHashMap<>();
        try (Session session = driver.session()) {
            Result skel = session.run(SKELETON, Map.of("pid", projectId, "uids", uids));
            while (skel.hasNext()) {
                Record r = skel.next();
                String uid = r.get("uid").asString();
                byUid.put(uid, new CoreClass(
                        uid,
                        strOrNull(r.get("name")),
                        strOrNull(r.get("pkg")),
                        strOrNull(r.get("kind")),
                        strings(r.get("parents")),
                        strings(r.get("methods"))));
            }
        }

        List<CoreClass> filled = new ArrayList<>();
        for (String uid : uids) {
            CoreClass c = byUid.get(uid);
            if (c != null) {
                filled.add(c);
            }
        }
        return filled;
    }

    /**
     * 按需描述只处理「还没有描述」的类。
     * force=false 时再排除接口 / 抽象类 / 枚举；force=true 只放宽类型，不覆盖已有描述。
     */
    private List<Skeleton> loadPending(Long projectId, List<String> uids, boolean force) {
        Map<String, Skeleton> byUid = new LinkedHashMap<>();
        try (Session session = driver.session()) {
            Result result = session.run(SKELETON, Map.of("pid", projectId, "uids", uids));
            while (result.hasNext()) {
                Record r = result.next();
                String existing = strOrNull(r.get("description"));
                if (existing != null && !existing.isBlank()) {
                    continue;
                }
                String kind = strOrNull(r.get("kind"));
                boolean isAbstract = r.get("isAbstract").asBoolean(false);
                if (!force && (!"CLASS".equals(kind) || isAbstract)) {
                    continue;
                }
                String uid = r.get("uid").asString();
                byUid.put(uid, new Skeleton(
                        uid,
                        strOrNull(r.get("name")),
                        kind,
                        strOrNull(r.get("pkg")),
                        strings(r.get("parents")),
                        strings(r.get("methods"))));
            }
        }

        List<Skeleton> ordered = new ArrayList<>();
        for (String uid : uids) {
            Skeleton s = byUid.get(uid);
            if (s != null) {
                ordered.add(s);
            }
        }
        return ordered;
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
            appendSkeleton(sb, i + 1, c.qualifiedName(), c.kind(), c.pkg(), c.parents(), c.methods());
        }
        return sb.toString();
    }

    String buildOnDemandPrompt(List<Skeleton> items) {
        StringBuilder sb = new StringBuilder();
        sb.append("你是代码理解助手。以下是某个 Java 项目中若干类的骨架（类名、包、继承/实现、方法签名）。\n");
        sb.append("请为每个类生成一句中文简介（不超过 30 字），说明它在项目中承担的业务职责，不要复述技术细节。\n");
        sb.append("qualifiedName 必须原样使用我给出的完整全限定名，不要改动或缩短。\n");
        sb.append("只输出 JSON，不要输出解释或代码块标记，格式：");
        sb.append("{\"items\":[{\"qualifiedName\":\"...\",\"description\":\"...\"}]}\n\n");
        sb.append("=== 类骨架 ===\n");
        for (int i = 0; i < items.size(); i++) {
            Skeleton s = items.get(i);
            appendSkeleton(sb, i + 1, s.uid(), s.kind(), s.pkg(), s.parents(), s.methods());
        }
        return sb.toString();
    }

    private void appendSkeleton(StringBuilder sb, int index, String uid, String kind,
                                String pkg, List<String> parents, List<String> methods) {
        sb.append(index).append(". ").append(uid);
        sb.append(" (").append(kind == null ? "CLASS" : kind).append(')');
        if (pkg != null && !pkg.isBlank()) {
            sb.append(" | 包: ").append(pkg);
        }
        if (!parents.isEmpty()) {
            sb.append(" | 继承/实现: ").append(String.join(", ", parents));
        }
        if (!methods.isEmpty()) {
            int n = Math.min(MAX_METHODS_PER_CLASS, methods.size());
            sb.append(" | 方法: ").append(String.join(", ", methods.subList(0, n)));
        }
        sb.append('\n');
    }

    private ClassDescriptionBatch callLlm(String prompt) {
        try {
            ChatClient chatClient = ChatClient.create(chatModel);
            return chatClient.prompt(prompt).call().entity(ClassDescriptionBatch.class);
        } catch (Exception e) {
            log.error("调用 LLM 生成类描述失败", e);
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
            String description = item.getDescription().trim();
            if (description.isEmpty()) {
                continue;
            }
            rows.add(row(projectId, item.getQualifiedName(), description));
        }
        writeRows(rows);
    }

    /**
     * 只写回本次真正请求过的 uid —— LLM 偶尔会多返回条目，
     * 不设白名单就可能给一个没请求的类写描述。
     */
    private List<ClassDescriptionsResponse.Item> writeBackOnDemand(
            Long projectId, ClassDescriptionBatch batch, Set<String> allowed, Set<String> coreUids) {
        List<ClassDescriptionsResponse.Item> items = new ArrayList<>();
        if (batch == null || batch.getItems() == null) {
            return items;
        }
        List<Map<String, Object>> rows = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (ClassDescriptionBatch.Item item : batch.getItems()) {
            String uid = item.getQualifiedName();
            if (uid == null || item.getDescription() == null) {
                continue;
            }
            if (!allowed.contains(uid) || !seen.add(uid)) {
                continue;
            }
            String description = item.getDescription().trim();
            if (description.isEmpty()) {
                continue;
            }
            rows.add(row(projectId, uid, description));

            ClassDescriptionsResponse.Item out = new ClassDescriptionsResponse.Item();
            out.setUid(uid);
            out.setDescription(description);
            out.setCore(coreUids.contains(uid));
            items.add(out);
        }
        writeRows(rows);
        return items;
    }

    private Map<String, Object> row(Long projectId, String uid, String description) {
        Map<String, Object> row = new HashMap<>();
        row.put("projectId", projectId);
        row.put("uid", uid);
        row.put("description", description);
        return row;
    }

    private void writeRows(List<Map<String, Object>> rows) {
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

    private String strOrNull(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private int countOf(ClassDescriptionBatch batch) {
        return batch == null || batch.getItems() == null ? 0 : batch.getItems().size();
    }

    record CoreClass(String qualifiedName, String name, String pkg, String kind,
                     List<String> parents, List<String> methods) {
    }

    record Skeleton(String uid, String name, String kind, String pkg,
                    List<String> parents, List<String> methods) {
    }
}
