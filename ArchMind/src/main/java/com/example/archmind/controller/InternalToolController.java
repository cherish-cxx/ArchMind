package com.example.archmind.controller;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.request.InternalToolRequest;
import com.example.archmind.dto.response.CallChainResponse;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;
import com.example.archmind.dto.response.Evidence;
import com.example.archmind.dto.response.MethodBodyResponse;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.dto.response.RelatedClassDTO;
import com.example.archmind.dto.response.ToolEnvelope;
import com.example.archmind.service.ProjectOverviewService;
import com.example.archmind.service.ast.CallChainService;
import com.example.archmind.service.ast.ChainDirection;
import com.example.archmind.service.ast.CodeGraphQueryService;
import com.example.archmind.service.ast.GraphRevService;
import com.example.archmind.service.ast.MethodBodyService;
import com.example.archmind.service.ast.RelationDirection;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Agent 专用内部取数接口（{@code /internal/tools/*}）。
 *
 * <p>这些接口对「Agent」一无所知 —— 它们只是普通的内部数据接口，因此前端的图谱页
 * 将来可以复用同一批。鉴权由 {@code InternalTokenFilter} 统一处理，这里不校验用户身份。</p>
 *
 * <p>工具「答不了」是业务结果不是异常，统一返回 {@code ok:false} 的信封，不走 500。</p>
 */
@RestController
@RequestMapping("/internal/tools")
@RequiredArgsConstructor
@Slf4j
public class InternalToolController {

    /** 单次返回节点数上限：默认值与硬上限。调用方给什么都不能突破 MAX_LIMIT */
    private static final int DEFAULT_LIMIT = 40;
    private static final int MAX_LIMIT = 200;
    /** 调用链默认往下走 2 层 —— 正好覆盖「接口 → 实现 → 它调了谁」这条主链路 */
    private static final int DEFAULT_CHAIN_DEPTH = 2;
    /** 代码切片：默认最多 3 个方法、120 行 —— 再往上会把 prompt 撑爆 */
    private static final int DEFAULT_MAX_METHODS = 3;
    private static final int DEFAULT_MAX_LINES = 120;

    private final ProjectOverviewService projectOverviewService;
    private final CodeGraphQueryService codeGraphQueryService;
    private final CallChainService callChainService;
    private final MethodBodyService methodBodyService;
    private final GraphRevService graphRevService;
    private final CheckProjectUtil checkProjectUtil;

    /** 这个项目是做什么的。数据源是 project_overview 表，不碰图 —— 未落图的项目也能答 */
    @PostMapping("/overview")
    public ToolEnvelope<ProjectOverviewResponse> overview(@RequestBody InternalToolRequest request) {
        Long projectId = request.getProjectId();
        try {
            checkProjectUtil.checkProjectExists(projectId);
            ProjectOverviewResponse data = projectOverviewService.tryGetExistingOverview(projectId);
            if (data == null) {
                return stamp(ToolEnvelope.failure("该项目尚未生成项目概况，请先完成分析"), projectId);
            }
            return stamp(ToolEnvelope.success(data), projectId);
        } catch (BusinessException e) {
            log.warn("overview 工具返回失败 projectId={}: {}", projectId, e.getMessage());
            return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
        }
    }

    /** 项目里有哪些类（兜底用：用户没给锚点时，让 LLM 在清单里选而不是硬猜） */
    @PostMapping("/classes")
    public ToolEnvelope<List<ClassNodeDTO>> classes(@RequestBody InternalToolRequest request) {
        Long projectId = request.getProjectId();
        try {
            checkProjectUtil.checkProjectExists(projectId);
            int limit = clampLimit(request.getArgs());

            // 多取一条用于判断「是否还有更多」，省掉一次 count
            List<ClassNodeDTO> rows = codeGraphQueryService.listClasses(projectId, limit + 1);
            boolean truncated = rows.size() > limit;
            List<ClassNodeDTO> data =
                    truncated ? new ArrayList<>(rows.subList(0, limit)) : rows;

            List<Evidence> evidence = data.stream()
                    .map(c -> classEvidence(c.getQualifiedName()))
                    .toList();
            return stamp(ToolEnvelope.success(data, evidence, truncated), projectId);
        } catch (BusinessException e) {
            log.warn("classes 工具返回失败 projectId={}: {}", projectId, e.getMessage());
            return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
        }
    }

    /** 谁依赖它 / 它继承谁。direction 不给默认 BOTH，与前端「点类看关联」一致 */
    @PostMapping("/class-relations")
    public ToolEnvelope<ClassRelationsResponse> classRelations(@RequestBody InternalToolRequest request) {
        Long projectId = request.getProjectId();
        try {
            checkProjectUtil.checkProjectExists(projectId);
            String uid = strArg(request.getArgs(), "uid");
            if (uid == null || uid.isBlank()) {
                return stamp(ToolEnvelope.failure("缺少参数 uid（类的全限定名）"), projectId);
            }
            RelationDirection direction = parseDirection(strArg(request.getArgs(), "direction"));
            if (direction == null) {
                return stamp(ToolEnvelope.failure("direction 取值必须是 UP / DOWN / BOTH"), projectId);
            }
            int limit = clampLimit(request.getArgs());

            ClassRelationsResponse data =
                    codeGraphQueryService.classRelations(projectId, uid, direction);
            List<Evidence> evidence = new ArrayList<>();
            boolean truncated = capRelations(data, limit, evidence);
            return stamp(ToolEnvelope.success(data, evidence, truncated), projectId);
        } catch (BusinessException e) {
            log.warn("class-relations 工具返回失败 projectId={}: {}", projectId, e.getMessage());
            return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
        }
    }

    /**
     * 谁调用了谁（业务流程）。**Agent 的核心场景。**
     * DOWN 多层往下走；UP 只查一层（再往上会迅速发散到全项目）。
     */
    @PostMapping("/call-chain")
    public ToolEnvelope<CallChainResponse> callChain(@RequestBody InternalToolRequest request) {
        Long projectId = request.getProjectId();
        try {
            checkProjectUtil.checkProjectExists(projectId);
            String uid = strArg(request.getArgs(), "uid");
            if (uid == null || uid.isBlank()) {
                return stamp(ToolEnvelope.failure("缺少参数 uid（类的全限定名）"), projectId);
            }
            ChainDirection direction = parseChainDirection(strArg(request.getArgs(), "direction"));
            if (direction == null) {
                return stamp(ToolEnvelope.failure("direction 取值必须是 DOWN / UP"), projectId);
            }

            CallChainResponse data = callChainService.callChain(
                    projectId,
                    uid,
                    strArg(request.getArgs(), "methodUid"),
                    direction,
                    intArg(request.getArgs(), "maxDepth", DEFAULT_CHAIN_DEPTH),
                    intArg(request.getArgs(), "maxNodes", clampLimit(request.getArgs())));

            return stamp(ToolEnvelope.success(data, chainEvidence(data), data.isTruncated()), projectId);
        } catch (BusinessException e) {
            log.warn("call-chain 工具返回失败 projectId={}: {}", projectId, e.getMessage());
            return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
        }
    }

    /**
     * 这个方法具体怎么实现的 —— **「讲解深度」的唯一来源**。
     * 只给 uid（类）时按 public 优先挑最多 3 个方法；给了 methodUid 就只切那一个。
     */
    @PostMapping("/method-body")
    public ToolEnvelope<MethodBodyResponse> methodBody(@RequestBody InternalToolRequest request) {
        Long projectId = request.getProjectId();
        try {
            checkProjectUtil.checkProjectExists(projectId);
            String uid = strArg(request.getArgs(), "uid");
            String methodUid = strArg(request.getArgs(), "methodUid");
            if (isBlank(uid) && isBlank(methodUid)) {
                return stamp(ToolEnvelope.failure("需要 uid（类的全限定名）或 methodUid（方法 uid）"), projectId);
            }

            MethodBodyResponse data = methodBodyService.methodBody(
                    projectId, uid, methodUid,
                    intArg(request.getArgs(), "maxMethods", DEFAULT_MAX_METHODS),
                    intArg(request.getArgs(), "maxLines", DEFAULT_MAX_LINES));

            return stamp(ToolEnvelope.success(data, methodEvidence(data), data.isTruncated()), projectId);
        } catch (BusinessException e) {
            log.warn("method-body 工具返回失败 projectId={}: {}", projectId, e.getMessage());
            return stamp(ToolEnvelope.failure(e.getMessage()), projectId);
        }
    }

    /**
     * 方法级 evidence。
     * 自动跳到实现类时，**接口方法 uid 和实现方法 uid 都要收** —— 调用方 citation 可能引的是前者。
     */
    private List<Evidence> methodEvidence(MethodBodyResponse data) {
        List<Evidence> evidence = new ArrayList<>();
        for (MethodBodyResponse.MethodCode m : data.getMethods()) {
            Evidence ev = new Evidence();
            ev.setKind("METHOD");
            ev.setFrom(m.getUid());
            evidence.add(ev);
            if (m.getResolvedFrom() != null) {
                Evidence origin = new Evidence();
                origin.setKind("METHOD");
                origin.setFrom(m.getResolvedFrom());
                evidence.add(origin);
            }
        }
        return evidence;
    }

    /**
     * 类级 evidence：节点 + 边。
     * 必须是**类 uid 级别**而不是方法级别 —— 后续要用它校验 LLM 生成的
     * {@code uiCommands[].target.uid}，而那些 uid 指向的是类。
     */
    private List<Evidence> chainEvidence(CallChainResponse data) {
        List<Evidence> evidence = new ArrayList<>();
        for (CallChainResponse.ChainNode n : data.getNodes()) {
            evidence.add(classEvidence(n.getUid()));
        }
        for (CallChainResponse.ChainEdge e : data.getEdges()) {
            Evidence ev = new Evidence();
            ev.setKind("EDGE");
            ev.setFrom(e.getFrom());
            ev.setTo(e.getTo());
            ev.setRel(e.getRel());
            ev.setCount(e.getCallCount());
            evidence.add(ev);
        }
        return evidence;
    }

    /**
     * 给信封盖上图版本号。**失败响应也要盖** —— 「类不存在」很可能是图被重建过导致的，
     * 调用方正是需要 rev 才能判断自己的 Context 是否已过期。
     */
    private <T> ToolEnvelope<T> stamp(ToolEnvelope<T> envelope, Long projectId) {
        return envelope.withGraphRev(graphRevService.currentRev(projectId));
    }

    // ==================== 上限与截断 ====================

    /**
     * 关联结果按 limit 截断，并把类级边收进 evidence。
     *
     * <p>截断只能在**聚合之后**做：一个类可能对应几十条方法级边，若在 Cypher 层按行
     * {@code LIMIT}，本该合并成一个类的行会被切掉一半，聚合结果就歪了。</p>
     */
    private boolean capRelations(ClassRelationsResponse data, int limit, List<Evidence> evidence) {
        boolean truncated = false;
        String center = data.getCenter().getQualifiedName();

        ClassRelationsResponse.Upstream up = data.getUpstream();
        if (up != null) {
            truncated |= capList(up.getCallers(), limit, evidence, center, true);
            truncated |= capList(up.getDependents(), limit, evidence, center, true);
            truncated |= capList(up.getSubclasses(), limit, evidence, center, true);
        }
        ClassRelationsResponse.Downstream down = data.getDownstream();
        if (down != null) {
            truncated |= capList(down.getCallees(), limit, evidence, center, false);
            truncated |= capList(down.getDependencies(), limit, evidence, center, false);
            truncated |= capList(down.getParents(), limit, evidence, center, false);
        }
        return truncated;
    }

    /**
     * 就地截断一个关联列表，并逐条收集类级 evidence。
     *
     * @param upstream true 表示这条边指向 center（related → center），false 表示由 center 指出
     */
    private boolean capList(List<RelatedClassDTO> list, int limit, List<Evidence> evidence,
                            String centerUid, boolean upstream) {
        if (list == null || list.isEmpty()) {
            return false;
        }
        boolean truncated = list.size() > limit;
        if (truncated) {
            List<RelatedClassDTO> kept = new ArrayList<>(list.subList(0, limit));
            list.clear();
            list.addAll(kept);
        }
        for (RelatedClassDTO rel : list) {
            Evidence e = new Evidence();
            e.setKind("EDGE");
            e.setRel(rel.getRelationType());
            e.setCount(rel.getEvidence() == null ? 0 : rel.getEvidence().size());
            String other = rel.getClazz().getQualifiedName();
            e.setFrom(upstream ? other : centerUid);
            e.setTo(upstream ? centerUid : other);
            evidence.add(e);
        }
        return truncated;
    }

    private Evidence classEvidence(String uid) {
        Evidence e = new Evidence();
        e.setKind("CLASS");
        e.setFrom(uid);
        return e;
    }

    private int clampLimit(Map<String, Object> args) {
        int limit = DEFAULT_LIMIT;
        Object raw = args == null ? null : args.get("limit");
        if (raw instanceof Number n) {
            limit = n.intValue();
        }
        return Math.max(1, Math.min(limit, MAX_LIMIT));
    }

    private int intArg(Map<String, Object> args, String key, int fallback) {
        Object raw = args == null ? null : args.get(key);
        return raw instanceof Number n ? n.intValue() : fallback;
    }

    private String strArg(Map<String, Object> args, String key) {
        Object raw = args == null ? null : args.get(key);
        return raw == null ? null : String.valueOf(raw).trim();
    }

    private boolean isBlank(String s) {
        return s == null || s.isBlank();
    }

    /** 不给 direction 时默认 BOTH；给了非法值返回 null，由调用方拒绝而不是静默兜底 */
    private RelationDirection parseDirection(String raw) {
        if (raw == null || raw.isBlank()) {
            return RelationDirection.BOTH;
        }
        try {
            return RelationDirection.valueOf(raw.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    /** 不给 direction 时默认 DOWN —— 业务流程是主场景；给了非法值返回 null 让调用方拒绝 */
    private ChainDirection parseChainDirection(String raw) {
        if (raw == null || raw.isBlank()) {
            return ChainDirection.DOWN;
        }
        try {
            return ChainDirection.valueOf(raw.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            return null;
        }
    }
}
