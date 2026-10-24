package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.FileEntityMapper;
import com.example.archmind.dto.response.MethodBodyResponse;
import com.example.archmind.entity.FileEntity;
import com.example.archmind.service.SourceSliceService;
import com.example.archmind.service.ast.MethodBodyService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.stereotype.Service;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 方法源码切片。
 *
 * <p>两边都有坑要防：**行号**从第一个注解算起（`@GlobalTransactional` 这类语义会被包含，
 * javadoc 不会）；**接口方法**没有方法体，直接切只有一行声明，所以要顺 {@code IMPLEMENTS +
 * signature 相等} 自动跳到实现类。</p>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class MethodBodyServiceImpl implements MethodBodyService {

    /** 硬上限：防止 prompt 被代码撑爆 */
    private static final int MAX_METHODS_HARD = 3;
    /**
     * 单次切片的总行数上限。
     *
     * <p>方法级锁定（给了 methodUid）只切 1 个方法，120 行很容易把带事务的长方法切成半截，
     * 所以上限放到 300。类级调用方仍显式传 120（{@code java_client.DEFAULT_MAX_LINES}），
     * 老行为不变。</p>
     */
    private static final int MAX_LINES_HARD = 300;

    /** public 优先，再按行号 —— 用户问「这个类怎么实现的」想看的是对外方法，不是私有辅助 */
    private static final String METHODS_OF_CLASS = """
            MATCH (m:Method {projectId: $pid, owner: $uid})
            MATCH (c:Class {projectId: $pid, uid: m.owner})
            RETURN m.uid AS uid, m.owner AS owner, m.signature AS signature,
                   m.fileId AS fileId, m.startLine AS startLine, m.endLine AS endLine,
                   m.visibility AS visibility, c.kind AS ownerKind
            ORDER BY CASE WHEN m.visibility = 'public' THEN 0 ELSE 1 END, m.startLine
            LIMIT $limit
            """;

    private static final String METHOD_BY_UID = """
            MATCH (m:Method {projectId: $pid, uid: $methodUid})
            MATCH (c:Class {projectId: $pid, uid: m.owner})
            RETURN m.uid AS uid, m.owner AS owner, m.signature AS signature,
                   m.fileId AS fileId, m.startLine AS startLine, m.endLine AS endLine,
                   m.visibility AS visibility, c.kind AS ownerKind
            """;

    /** 接口方法 → 实现类的同签名方法。签名用的是参数类型源码字面量，接口与实现必然相同 */
    private static final String IFACE_TO_IMPL = """
            MATCH (impl:Class {projectId: $pid})-[:IMPLEMENTS]->(:Class {projectId: $pid, uid: $ifaceUid})
            MATCH (im:Method {projectId: $pid, owner: impl.uid, signature: $signature})
            RETURN im.uid AS uid, im.owner AS owner, im.signature AS signature,
                   im.fileId AS fileId, im.startLine AS startLine, im.endLine AS endLine
            LIMIT 1
            """;

    private final Driver driver;
    private final FileEntityMapper fileEntityMapper;
    private final SourceSliceService sourceSliceService;

    @Override
    public MethodBodyResponse methodBody(Long projectId, String uid, String methodUid,
                                         int maxMethods, int maxLines) {
//        校验参数是否为空
        if (projectId == null || (uid == null || uid.isBlank()) && (methodUid == null || methodUid.isBlank())) {
            throw new BusinessException("缺少 projectId 或 uid");
        }
//        确定最大的输出范围
        int maxM = Math.max(1, Math.min(maxMethods, MAX_METHODS_HARD));
        int maxL = Math.max(1, Math.min(maxLines, MAX_LINES_HARD));
//创建一个列表，并获取uid所映射的限制范围内的方法列表
        List<MInfo> selected = selectMethods(projectId, uid, methodUid, maxM);
        if (selected.isEmpty()) {
            throw new BusinessException("没有找到可切片的方法: " + (methodUid == null ? uid : methodUid));
        }

        MethodBodyResponse resp = new MethodBodyResponse();
        List<String> notes = new ArrayList<>();

        Path extractDir = sourceSliceService.resolveExtractDir(projectId);
        Map<Long, String> pathByFileId = new HashMap<>();
        Map<Long, String[]> linesByFileId = new HashMap<>();

        int budget = maxL;
        for (MInfo m : selected) {
            // 接口方法没有方法体，跳到实现类再去切
            String resolvedFrom = null;
//            如果这个方法属于接口类型
            if ("INTERFACE".equals(m.ownerKind())) {
//                根据项目ID，接口，方法签名找到对饮过的实现类里的方法
                MInfo impl = ifaceToImpl(projectId, m.owner(), m.signature());
                if (impl != null) {
                    resolvedFrom = m.uid();
                    m = impl;
                } else {
                    notes.add("接口方法无实现类，只能给出声明: " + m.uid());
                }
            }

            /**
             *  新建一个结果里的方法信息对象
             */
            MethodBodyResponse.MethodCode item = new MethodBodyResponse.MethodCode();
            item.setUid(m.uid());
            item.setOwner(m.owner());
            item.setSignature(m.signature());
            item.setStartLine(m.startLine());
            item.setEndLine(m.endLine());
            item.setResolvedFrom(resolvedFrom);

            String[] lines;
            try {
//                根据文件路径，id读取这个源码文件的全部行，返回字符串数组，一行一条
                lines = readLines(extractDir, m.fileId(), pathByFileId, linesByFileId);
            } catch (BusinessException e) {
                item.setError(e.getMessage());
                notes.add("源码读不到: " + m.uid() + "（" + e.getMessage() + "）");
                resp.getMethods().add(item);
                break;
            }

            /*
              保护边界，行号最小是第1行，结束行不能超过文件总行数量
             */
            int from = Math.max(1, m.startLine());
            int to = Math.min(lines.length, m.endLine());
            if (from > to) {
                item.setError("行号越界: " + m.startLine() + "-" + m.endLine() + "，文件共 " + lines.length + " 行");
                notes.add(item.getError() + "（" + m.uid() + "）");
                resp.getMethods().add(item);
                continue;
            }
//计算当单个方法所占的行数，是否会超出预算
            int len = to - from + 1;
            if (len > budget) {
                // 单个方法就超预算：切到剩余额度为止
                to = from + budget - 1;
                item.setEndLine(to);
                item.setCode(join(lines, from, to) + "\n// …（已截断）");
                resp.getMethods().add(item);
                resp.setTruncated(true);
                break;
            }

            item.setCode(join(lines, from, to));
            resp.getMethods().add(item);
            budget -= len;//总预算扣减当前方法消耗的行数
            if (budget <= 0) {
                resp.setTruncated(true);
                break;//预算用完，结束
            }
        }

        if (!notes.isEmpty()) {
            resp.setNote(String.join("；", notes));
        }
        return resp;
    }

    // ==================== 选方法 ====================

    private List<MInfo> selectMethods(Long pid, String uid, String methodUid, int maxMethods) {
//        创建列表
        List<MInfo> list = new ArrayList<>();
        try (Session session = driver.session()) {
//            校验的是methodUid参数存在，能够精确找到所需method
            if (methodUid != null && !methodUid.isBlank()) {
                Result r = session.run(METHOD_BY_UID, Map.of("pid", pid, "methodUid", methodUid));
                if (r.hasNext()) {
                    list.add(toMInfo(r.next()));
                }
                return list;
            }
//            如果methodUid参数不存在则根据uid，pid，limit来获取method
            Result r = session.run(METHODS_OF_CLASS,
                    Map.of("pid", pid, "uid", uid, "limit", maxMethods));
            while (r.hasNext()) {
                list.add(toMInfo(r.next()));
            }
        }
        return list;
    }

    private MInfo ifaceToImpl(Long pid, String ifaceUid, String signature) {
        if (signature == null) {
            return null;
        }
        try (Session session = driver.session()) {
//            根据参数去查实现类
            Result r = session.run(IFACE_TO_IMPL,
                    Map.of("pid", pid, "ifaceUid", ifaceUid, "signature", signature));
            if (!r.hasNext()) {
                return null;
            }
            Record rec = r.next();
            return new MInfo(
                    rec.get("uid").asString(),
                    rec.get("owner").asString(),
                    rec.get("signature").asString(),
                    longOrNull(rec.get("fileId")),
                    intOrZero(rec.get("startLine")),
                    intOrZero(rec.get("endLine")),
                    "CLASS");
        }
    }

    private MInfo toMInfo(Record rec) {
        return new MInfo(
                rec.get("uid").asString(),
                rec.get("owner").asString(),
                rec.get("signature").asString(),
                longOrNull(rec.get("fileId")),
                intOrZero(rec.get("startLine")),
                intOrZero(rec.get("endLine")),
                str(rec.get("ownerKind")));
    }

    // ==================== 读文件并切片 ====================

    private String[] readLines(Path extractDir, Long fileId,
                               Map<Long, String> pathByFileId, Map<Long, String[]> linesByFileId) {
        String[] cached = linesByFileId.get(fileId);
        if (cached != null) {
            return cached;
        }
        if (fileId == null) {
            throw new BusinessException("图上没有 fileId，无法定位源码");
        }
        String relative = pathByFileId.computeIfAbsent(fileId, id -> {
            FileEntity f = fileEntityMapper.selectById(id);
            return f == null ? null : f.getFilePath();
        });
        if (relative == null) {
            throw new BusinessException("file 表里找不到 fileId=" + fileId);
        }

        String[] lines = sourceSliceService.readLines(extractDir, relative);
        linesByFileId.put(fileId, lines);
        return lines;
    }

    private String join(String[] lines, int from, int to) {
        StringBuilder sb = new StringBuilder();
        for (int i = from; i <= to && i <= lines.length; i++) {
            if (i > from) {
                sb.append('\n');
            }
            sb.append(lines[i - 1]);
        }
        return sb.toString();
    }

    // ==================== 小工具 ====================

    private static String str(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private static Long longOrNull(Value v) {
        return v == null || v.isNull() ? null : v.asLong();
    }

    private static int intOrZero(Value v) {
        return v == null || v.isNull() ? 0 : (int) v.asLong();
    }

    private record MInfo(String uid, String owner, String signature, Long fileId,
                         int startLine, int endLine, String ownerKind) {
    }
}
