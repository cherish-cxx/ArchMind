package com.example.archmind.service.impl;

import com.example.archmind.common.constant.GraphPredicates;
import com.example.archmind.common.constant.ProjectAnalysisStatus;
import com.example.archmind.common.constant.RedisKeys;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.response.ClassDescriptionBatch;
import com.example.archmind.dto.response.ClassDescriptionsResponse;
import com.example.archmind.dto.response.DescribeBatchResult;
import com.example.archmind.dto.response.DescriptionStatusResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ClassDescriptionService;
import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.ast.CodeGraphQueryService;
import com.example.archmind.service.ast.GraphRevService;
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
import java.util.concurrent.TimeUnit;

/**
 * 类简要描述：分析阶段批量生成核心类；运行期按画布内容按需补齐；
 * R4 阶段按批扫描「描述为空」的类，直到覆盖全量。
 *
 * <p>三条路径共用同一套骨架查询和写回逻辑，区别只在「选谁」：
 * 核心类走关联度 ranking，按需走前端给的 uid，补齐走「描述为空」。
 * 幂等键是类本身有没有描述，**不是批次记录**——已写过的类不会再被选中。</p>
 *
 * <p>项目级串行由 Redis 短锁按批保证：画布按需与批量补齐共用同一把锁，
 * 因此两条路径不会并发写同一批节点；锁按批释放，不是整轮持有。</p>
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

    /** 批量补齐默认/边界 */
    private static final int DEFAULT_BATCH_LIMIT = 25;
    private static final int MIN_BATCH_LIMIT = 5;
    private static final int MAX_BATCH_LIMIT = 40;

    /** 单批描述锁：必须大于一次 LLM 调用的预期耗时 */
    private static final long DESCRIBE_LOCK_TTL_SECONDS = 120L;
    private static final long FAILURE_COOLDOWN_SECONDS = 30L;
    private static final long BUSY_RETRY_MS = 1_000L;
    private static final long ANALYSIS_RUNNING_RETRY_MS = 5_000L;

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

    /**
     * 顶层类判定：AST 里内部类的 uid 是「外层类名 + '.' + 简单类名」，
     * 只有顶层类满足 uid == 包名 + 简单类名。不能靠 '$' 判断 —— 这里的命名用点号。
     */
    /** 与索引分页共用同一口径，见 {@link GraphPredicates#TOP_LEVEL}。 */
    private static final String TOP_LEVEL = GraphPredicates.TOP_LEVEL;

    /**
     * 选批查询。{@code WITH c ORDER BY c.uid LIMIT $limit} 必须在展开关系之前，
     * 否则 LIMIT 作用在方法行上，一批可能只有几个类。
     * 条件必须与 {@link #COUNTS} 的 pending 完全一致，否则 remaining 落不到 0。
     * 在同一个项目pid下，查询节点class，过滤描述为空的类，支持额外拼接条件%s，分页取limit条；
     * 同时关联它的父类/接口和自身防范，最后聚合返回
     */
    private static final String PENDING_SKELETON = """
            MATCH (c:Class {projectId: $pid})
            WHERE coalesce(c.description, '') = ''
              AND %s
            WITH c ORDER BY c.uid LIMIT $limit
            OPTIONAL MATCH (c)-[:EXTENDS|IMPLEMENTS]->(p:Class {projectId: $pid})
            OPTIONAL MATCH (m:Method {projectId: $pid, owner: c.uid})
            RETURN c.uid AS uid, c.name AS name, c.packageName AS pkg, c.kind AS kind,
                   coalesce(c.isAbstract, false) AS isAbstract,
                   collect(DISTINCT p.name) AS parents,
                   collect(DISTINCT m.signature) AS methods
            """.formatted(TOP_LEVEL);
    /**
     * 找到这个项目下哟有的类：返回总共有多少个类，满足条件的，需要写说明的类有多少个，满足条件的并且还没写说明的类有多少个。
     */
    private static final String COUNTS = """
            MATCH (c:Class {projectId: $pid})
            RETURN count(c) AS total,
                   sum(CASE WHEN %s THEN 1 ELSE 0 END) AS describable,
                   sum(CASE WHEN %s AND coalesce(c.description, '') = ''
                            THEN 1 ELSE 0 END) AS pending
            """.formatted(TOP_LEVEL, TOP_LEVEL);

    private static final String UPDATE_DESCRIPTION = """
            UNWIND $rows AS row
            MATCH (c:Class {projectId: row.projectId, uid: row.uid})
            SET c.description = row.description
            """;

    private final Driver driver;
    private final ChatModel chatModel;
    private final CodeGraphQueryService codeGraphQueryService;
    private final RedisUtil redisUtil;
    private final ProjectMapper projectMapper;
    private final GraphRevService graphRevService;

    @Override
    public void describeCoreClasses(Long projectId, ProgressReporter reporter) {
//        调用queryCoreClasses,拿到排好序的Top核心类完整信息(uid,类名,包,父类,方法等)
        List<CoreClass> cores = queryCoreClasses(projectId, DEFAULT_TOP_N);
        reporter.report(20);
        if (cores.isEmpty()) {
            log.info("无核心类可描述 projectId={}", projectId);
            reporter.report(100);
            return;
        }

//        把这些核心信息瓶装成给大模型的提示词prompt;
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

        if (isAnalyzing(projectId)) {
            markBusy(resp, "项目正在分析中，稍后再补描述", ANALYSIS_RUNNING_RETRY_MS);
            return resp;
        }

        long cooldownMs = cooldownRemainingMs(projectId);
        if (cooldownMs > 0) {
            markBusy(resp, "描述服务刚刚失败，请稍后重试", cooldownMs);
            return resp;
        }

        String lockKey = RedisKeys.DESCRIBE_LOCK + projectId;
        if (!Boolean.TRUE.equals(redisUtil.setIfAbsent(lockKey, "1", DESCRIBE_LOCK_TTL_SECONDS, TimeUnit.SECONDS))) {
            markBusy(resp, "描述批次处理中，稍后重试", BUSY_RETRY_MS);
            return resp;
        }
        try {
            if (isAnalyzing(projectId)) {
                markBusy(resp, "项目正在分析中，稍后再补描述", ANALYSIS_RUNNING_RETRY_MS);
                return resp;
            }
            return describeLocked(projectId, requested, force, resp);
        } finally {
            redisUtil.delete(lockKey);
        }
    }
    /**
     * 对外提供的批量补类描述的接口方法
     * 具有:状态校验,冷却,Redis分布式锁,过期校验,统计返回
     */

    @Override
    public DescribeBatchResult describePending(Long projectId, int limit) {
//        接收项目id,本次最多处理多少,ClampBatchLimit是把limit限制在一个上下限之间,初始化返回对象.
        int batchSize = clampBatchLimit(limit);
        DescribeBatchResult resp = new DescribeBatchResult();
        resp.setLimit(batchSize);
//        进行状态校验: 对project校验,分析状态.
        Project project = projectMapper.selectById(projectId);
        if (project == null) {
            return fail(resp, "项目不存在", 0);
        }
        if (!ProjectAnalysisStatus.COMPLETED.equals(project.getAnalysisStatus())) {
            return blocked(resp, DescribeBatchResult.STATUS_ANALYSIS_NOT_COMPLETED,
                    "分析尚未成功完成，暂不补齐描述", 0);
        }
        if (isAnalyzing(projectId)) {
            return blocked(resp, DescribeBatchResult.STATUS_ANALYSIS_RUNNING,
                    "项目正在分析中", ANALYSIS_RUNNING_RETRY_MS);
        }
//冷却控制:上一次LLM调用失败后会设置冷却时间,冷却期内不允许在调用,防止放开刷LLM
        long cooldownMs = cooldownRemainingMs(projectId);
        if (cooldownMs > 0) {
            return fail(resp, "描述服务刚失败，冷却中", cooldownMs);
        }
//分布式锁：redis分布式锁同一个项目统一时间只能够跑一批描述任务，避免并发多次调用LLM，重复写数据。抢不到锁就返回busy。并带有过期时间
        String lockKey = RedisKeys.DESCRIBE_LOCK + projectId;
        if (!Boolean.TRUE.equals(redisUtil.setIfAbsent(lockKey, "1", DESCRIBE_LOCK_TTL_SECONDS, TimeUnit.SECONDS))) {
            return blocked(resp, DescribeBatchResult.STATUS_BUSY, "已有描述批次在处理", BUSY_RETRY_MS);
        }
        try {
            // 抢到锁后再校验一次，关闭「检查闸门 → 抢锁」之间的窗口
            if (isAnalyzing(projectId)) {
                return blocked(resp, DescribeBatchResult.STATUS_ANALYSIS_RUNNING,
                        "项目正在分析中", ANALYSIS_RUNNING_RETRY_MS);
            }
            /*
            * 拿当前的版本号
            * */
            long rev = graphRevService.currentRev(projectId);
            /*
            * 获得一批描述为空的列表，并存入pending列表中
            * */
            List<Skeleton> pending = loadPendingClasses(projectId, batchSize);
//            需要写描述的数量
            Counts counts = countClasses(projectId);
            resp.setTotal(counts.total());
            resp.setDescribable(counts.describable());
            if (pending.isEmpty()) {
                resp.setStatus(DescribeBatchResult.STATUS_OK);
                resp.setRemaining(counts.pending());
                return resp;
            }

            ClassDescriptionBatch raw;
            try {
                raw = callLlm(buildIndexedPrompt(pending));
            } catch (Exception e) {
                setCooldown(projectId);
                log.error("批量生成类描述失败 projectId={}, count={}", projectId, pending.size(), e);
                return fail(resp, "描述生成失败，请稍后重试", FAILURE_COOLDOWN_SECONDS * 1000);
            }

            // 写回前复验：图被重建或分析已经开始，这一批的骨架可能已经过期
            if (graphRevService.currentRev(projectId) != rev || isAnalyzing(projectId)) {
                resp.setStatus(DescribeBatchResult.STATUS_STALE);
                resp.setNote("图已重建，本批结果丢弃");
                return resp;
            }

            Set<String> allowed = new LinkedHashSet<>();
            for (Skeleton s : pending) {
                allowed.add(s.uid());
            }
            Set<String> coreUids = Set.copyOf(codeGraphQueryService.coreClassUids(projectId));
//            在这里已经完成了对有描述的类的写入，然后返回一个列表
            List<ClassDescriptionsResponse.Item> items =
                    writeBackOnDemand(projectId, mapByIndex(raw, pending), allowed, coreUids);

            Set<String> written = new HashSet<>();
            for (ClassDescriptionsResponse.Item item : items) {
                written.add(item.getUid());
            }
            List<String> failed = new ArrayList<>();
            for (String uid : allowed) {
                if (!written.contains(uid)) {
                    failed.add(uid);
                }
            }

            Counts after = countClasses(projectId);
            resp.setItems(items);
            resp.setFailed(failed);
            resp.setTotal(after.total());
            resp.setDescribable(after.describable());
            resp.setRemaining(after.pending());
            resp.setStatus(DescribeBatchResult.STATUS_OK);
            log.info("批量类描述完成 projectId={}, 候选={}, 写入={}, 漏答={}, 剩余={}",
                    projectId, pending.size(), items.size(), failed.size(), after.pending());
            return resp;
        } catch (Exception e) {
            log.error("批量类描述异常 projectId={}", projectId, e);
            return fail(resp, "批量描述异常: " + e.getMessage(), 0);
        } finally {
            redisUtil.delete(lockKey);
        }
    }

    @Override
    public DescriptionStatusResponse descriptionStatus(Long projectId) {
        Project project = projectMapper.selectById(projectId);
        Counts counts = countClasses(projectId);

        DescriptionStatusResponse resp = new DescriptionStatusResponse();
        resp.setProjectId(projectId);
        resp.setAnalysisStatus(project == null ? null : project.getAnalysisStatus());
        resp.setTotal(counts.total());
        resp.setDescribable(counts.describable());
        resp.setPending(counts.pending());
        resp.setDescribed(Math.max(0, counts.describable() - counts.pending()));
        resp.setRev(graphRevService.currentRev(projectId));
        return resp;
    }

    // ==================== 查询 ====================
//这个项目ID，返回一个限制数量列表
    List<CoreClass> queryCoreClasses(Long projectId, int limit) {
//拿到这个核心类的uid结合，最多拿limit个uid
        List<String> uids = codeGraphQueryService.coreClassUids(projectId, limit);
//        如果uid是空，直接返回空集合，不用查数据库
        if (uids.isEmpty()) {
            return List.of();
        }
//创建一个byUid核心类对象，LinkedHashMap能够记住放进去的顺序
        Map<String, CoreClass> byUid = new LinkedHashMap<>();
//        打开Neo4j会话，执行Cypher语句SKELETON，参数传项目id和uid列表，但是只是有uid列表而已，没有其他内容所以需要根据该uid列表去拿到
//        每一uid所对应的详细内容
        try (Session session = driver.session()) {
            Result skel = session.run(SKELETON, Map.of("pid", projectId, "uids", uids));
//            遍历查到的每一条结果记录，uid，拼装成CoreClass对象，放进map里
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
//按最一开始拿到的uids顺序重新组装结果，循环uid列表，去map里找对应的CoreClass，能找到就假如最终返回列表里；数据库查不到这个uid的就直接不要
        List<CoreClass> filled = new ArrayList<>();
        for (String uid : uids) {
            CoreClass c = byUid.get(uid);
            if (c != null) {
                filled.add(c);
            }
        }
        return filled;
    }

    /** 按需路径：读请求里这些类的骨架与已有描述，由调用方决定哪些进入 LLM。 */
    private List<Candidate> loadCandidates(Long projectId, List<String> uids) {
        Map<String, Candidate> byUid = new LinkedHashMap<>();
        try (Session session = driver.session()) {
            Result result = session.run(SKELETON, Map.of("pid", projectId, "uids", uids));
            while (result.hasNext()) {
                Record r = result.next();
                String uid = r.get("uid").asString();
                byUid.put(uid, new Candidate(
                        new Skeleton(uid,
                                strOrNull(r.get("name")),
                                strOrNull(r.get("kind")),
                                strOrNull(r.get("pkg")),
                                strings(r.get("parents")),
                                strings(r.get("methods"))),
                        strOrNull(r.get("description")),
                        r.get("isAbstract").asBoolean(false)));
            }
        }

        List<Candidate> ordered = new ArrayList<>();
        for (String uid : uids) {
            Candidate c = byUid.get(uid);
            if (c != null) {
                ordered.add(c);
            }
        }
        return ordered;
    }

    /** 全量路径：服务端自己挑一批「描述为空」的类。 */
    private List<Skeleton> loadPendingClasses(Long projectId, int limit) {
        List<Skeleton> out = new ArrayList<>();
        try (Session session = driver.session()) {
            Result result = session.run(PENDING_SKELETON, Map.of("pid", projectId, "limit", limit));
            while (result.hasNext()) {
                Record r = result.next();
                out.add(new Skeleton(
                        r.get("uid").asString(),
                        strOrNull(r.get("name")),
                        strOrNull(r.get("kind")),
                        strOrNull(r.get("pkg")),
                        strings(r.get("parents")),
                        strings(r.get("methods"))));
            }
        }
        return out;
    }
/*
* 统计类的数量。拿到项目 id，开一个 Neo4j 会话，执行上面那条统计 Cypher 语句，传入项目 id。如果查不到数据，直接返回三个数都是 0 的统计对象；有结果就取出 total、describable、pending 这三个数字，封装成 Counts 对象返回。用完会话自动关闭。
* */
    private Counts countClasses(Long projectId) {
        try (Session session = driver.session()) {
            Result result = session.run(COUNTS, Map.of("pid", projectId));
            if (!result.hasNext()) {
                return new Counts(0, 0, 0);
            }
            Record r = result.next();
            return new Counts(
                    r.get("total").asInt(0),
                    r.get("describable").asInt(0),
                    r.get("pending").asInt(0));
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
//        加一个分隔标题,然后循环所有核心类,
        for (int i = 0; i < cores.size(); i++) {
            CoreClass c = cores.get(i);
//            把每一个类的全限定名,类型,包,父类,方法签名拼到字符串
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

    /**
     * 批量补齐的 prompt：用序号回填，而不是让模型复述几万个全限定名。
     * 模型少写错一个字符，按名字匹配就会丢一条；按序号映射不会。
     */
    String buildIndexedPrompt(List<Skeleton> items) {
        StringBuilder sb = new StringBuilder();
        sb.append("你是代码理解助手。以下是某个 Java 项目中若干类的骨架（序号、类名、包、继承/实现、方法签名）。\n");
        sb.append("请为每个类生成一句中文简介（不超过 30 字），说明它在项目中承担的业务职责，不要复述技术细节。\n");
        sb.append("index 必须原样使用我给出的序号（从 1 开始），不要漏项、不要重复。\n");
        sb.append("只输出 JSON，不要输出解释或代码块标记，格式：");
        sb.append("{\"items\":[{\"index\":1,\"description\":\"...\"}]}\n\n");
        sb.append("=== 类骨架 ===\n");
        for (int i = 0; i < items.size(); i++) {
            Skeleton s = items.get(i);
            appendSkeleton(sb, i + 1, s.uid(), s.kind(), s.pkg(), s.parents(), s.methods());
        }
        return sb.toString();
    }

//循环每一个核心类,压缩骨架信息,拼进prompt
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
//            预先配置号的大模型实例,创建一个对话客户端
            ChatClient chatClient = ChatClient.create(chatModel);
//            让框架自动解析LLM返回的JSON字符串,反射转换成ClassDescription这个java实体对象
            return chatClient.prompt(prompt).call().entity(ClassDescriptionBatch.class);
        } catch (Exception e) {
            log.error("调用 LLM 生成类描述失败", e);
            throw new BusinessException("调用 LLM 失败: " + e.getMessage());
        }
    }

    /** 把模型返回的序号映射回 uid；兼容模型忽略序号直接回全限定名的情况。 */
    ClassDescriptionBatch mapByIndex(ClassDescriptionBatch raw, List<Skeleton> pending) {
        ClassDescriptionBatch out = new ClassDescriptionBatch();
        out.setItems(new ArrayList<>());
        if (raw == null || raw.getItems() == null) {
            return out;
        }

        Map<String, String> byUid = new LinkedHashMap<>();
        for (ClassDescriptionBatch.Item item : raw.getItems()) {
            if (item == null || item.getDescription() == null || item.getDescription().isBlank()) {
                continue;
            }
            String uid = null;
            Integer idx = item.getIndex();
            if (idx != null && idx >= 1 && idx <= pending.size()) {
                uid = pending.get(idx - 1).uid();
            } else if (item.getQualifiedName() != null) {
                for (Skeleton s : pending) {
                    if (s.uid().equals(item.getQualifiedName())) {
                        uid = s.uid();
                        break;
                    }
                }
            }
            if (uid != null) {
                byUid.putIfAbsent(uid, item.getDescription().trim());
            }
        }

        for (Map.Entry<String, String> e : byUid.entrySet()) {
            ClassDescriptionBatch.Item item = new ClassDescriptionBatch.Item();
            item.setQualifiedName(e.getKey());
            item.setDescription(e.getValue());
            out.getItems().add(item);
        }
        return out;
    }

    // ==================== 写回 ====================

    private void writeBack(Long projectId, ClassDescriptionBatch batch) {
//        逐行拆解:先兜底判断,如果LLM为空那么直接结束
        if (batch == null || batch.getItems() == null || batch.getItems().isEmpty()) {
            return;
        }
//        准备一个集合用来存放记录,循环遍历AI返回的每一条全限类名+描述
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
//        先准备返回列表：如果批量数据为空，就直接返回空值
        List<ClassDescriptionsResponse.Item> items = new ArrayList<>();
        if (batch == null || batch.getItems() == null) {
            return items;
        }
//        准备一个row数组
        List<Map<String, Object>> rows = new ArrayList<>();
        Set<String> seen = new HashSet<>();
//        遍历每一条传入的描述
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
//        这里已经完成了对Neo4j的写入
        writeRows(rows);
//        回传一个列表包含了uid,描述,和是否需要分析
        return items;
    }
//将传入的参数整理成一块完成的信息
    private Map<String, Object> row(Long projectId, String uid, String description) {
        Map<String, Object> row = new HashMap<>();
        row.put("projectId", projectId);
        row.put("uid", uid);
        row.put("description", description);
        return row;
    }
//写进Neo4j中去
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

    // ==================== 按需路径（持锁后） ====================

    private ClassDescriptionsResponse describeLocked(Long projectId, List<String> requested,
                                                     boolean force, ClassDescriptionsResponse resp) {
        List<Candidate> candidates;
        try {
            candidates = loadCandidates(projectId, requested);
        } catch (Exception e) {
            log.error("读取类骨架失败 projectId={}", projectId, e);
            resp.setSkipped(requested);
            resp.setNote("读取类信息失败");
            return resp;
        }

        Map<String, Candidate> byUid = new HashMap<>();
        for (Candidate c : candidates) {
            byUid.put(c.skeleton().uid(), c);
        }
        Set<String> coreUids = Set.copyOf(codeGraphQueryService.coreClassUids(projectId));

        List<Skeleton> pending = new ArrayList<>();
        for (String uid : requested) {
            Candidate c = byUid.get(uid);
            if (c == null) {
                resp.getSkipped().add(uid);
                continue;
            }
            // 已有描述的也回给前端：批次可能刚写完，画布重试时要能拿到值
            if (c.description() != null && !c.description().isBlank()) {
                resp.getItems().add(item(uid, c.description(), coreUids.contains(uid)));
                continue;
            }
            if (!force && (!"CLASS".equals(c.skeleton().kind()) || c.isAbstract())) {
                resp.getSkipped().add(uid);
                continue;
            }
            pending.add(c.skeleton());
        }
        if (pending.isEmpty()) {
            return resp;
        }

        long rev = graphRevService.currentRev(projectId);
        ClassDescriptionBatch batch;
        try {
            batch = callLlm(buildOnDemandPrompt(pending));
        } catch (Exception e) {
            log.error("按需生成类描述失败 projectId={}, count={}", projectId, pending.size(), e);
            setCooldown(projectId);
            for (Skeleton s : pending) {
                resp.getSkipped().add(s.uid());
            }
            resp.setNote("描述生成失败，请稍后重试");
            resp.setBusy(true);
            resp.setRetryAfterMs(FAILURE_COOLDOWN_SECONDS * 1000);
            return resp;
        }

        if (graphRevService.currentRev(projectId) != rev || isAnalyzing(projectId)) {
            markBusy(resp, "图已更新，稍后重试", 2_000L);
            return resp;
        }

        Set<String> pendingUids = new LinkedHashSet<>();
        for (Skeleton s : pending) {
            pendingUids.add(s.uid());
        }
        List<ClassDescriptionsResponse.Item> written =
                writeBackOnDemand(projectId, batch, pendingUids, coreUids);
        resp.getItems().addAll(written);

        Set<String> writtenUids = new HashSet<>();
        for (ClassDescriptionsResponse.Item item : written) {
            writtenUids.add(item.getUid());
        }
        for (String uid : pendingUids) {
            if (!writtenUids.contains(uid)) {
                resp.getSkipped().add(uid);
            }
        }
        return resp;
    }

    private ClassDescriptionsResponse.Item item(String uid, String description, boolean core) {
        ClassDescriptionsResponse.Item item = new ClassDescriptionsResponse.Item();
        item.setUid(uid);
        item.setDescription(description);
        item.setCore(core);
        return item;
    }

    // ==================== 闸门 / 锁 / 统计 ====================

    private boolean isAnalyzing(Long projectId) {
        return Boolean.TRUE.equals(redisUtil.hasKey(RedisKeys.ANALYZE_LOCK + projectId));
    }
    /*
    * 用项目Id拼接上redis的Key,通过redis查这个key还有多久过期
    * 如果查到的过期时间部署null,而且大于0,就把秒转成毫秒返回
    * */
    private long cooldownRemainingMs(Long projectId) {
        Long ttl = redisUtil.getExpire(RedisKeys.DESCRIBE_COOLDOWN + projectId);
        return ttl != null && ttl > 0 ? ttl * 1000 : 0L;
    }

    private void setCooldown(Long projectId) {
        redisUtil.set(RedisKeys.DESCRIBE_COOLDOWN + projectId, "1",
                FAILURE_COOLDOWN_SECONDS, TimeUnit.SECONDS);
    }

    private int clampBatchLimit(int limit) {
        int value = limit <= 0 ? DEFAULT_BATCH_LIMIT : limit;
        return Math.max(MIN_BATCH_LIMIT, Math.min(value, MAX_BATCH_LIMIT));
    }

    private DescribeBatchResult blocked(DescribeBatchResult resp, String status,
                                        String note, long retryAfterMs) {
        resp.setStatus(status);
        resp.setNote(note);
        if (retryAfterMs > 0) {
            resp.setRetryAfterMs(retryAfterMs);
        }
        return resp;
    }

    private DescribeBatchResult fail(DescribeBatchResult resp, String note, long retryAfterMs) {
        resp.setStatus(DescribeBatchResult.STATUS_FAILED);
        resp.setNote(note);
        resp.setRetryAfterMs(retryAfterMs);
        return resp;
    }

    private void markBusy(ClassDescriptionsResponse resp, String note, long retryAfterMs) {
        resp.setBusy(true);
        resp.setRetryAfterMs(retryAfterMs);
        resp.setNote(note);
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

    private record Candidate(Skeleton skeleton, String description, boolean isAbstract) {
    }

    private record Counts(int total, int describable, int pending) {
    }
}
