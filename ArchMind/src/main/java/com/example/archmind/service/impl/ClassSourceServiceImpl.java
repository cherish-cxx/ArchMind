package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassSourceResponse;
import com.example.archmind.service.ClassSourceService;
import com.example.archmind.service.SourceSliceService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.stereotype.Service;

import java.nio.file.Path;
import java.util.Map;

/**
 * 类源码切片：Neo4j 上的 Class 节点已经带 filePath/startLine/endLine，
 * 直接定位磁盘文件切片，不做任何语法分析。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class ClassSourceServiceImpl implements ClassSourceService {

    /** 单次返回的默认行数与硬上限，避免把超大文件整体塞进响应 */
    private static final int DEFAULT_MAX_LINES = 2000;
    private static final int MAX_LINES_HARD = 4000;

    private static final String FIND_CLASS_SOURCE = """
            MATCH (c:Class {projectId: $pid, uid: $uid})
            RETURN c.uid AS uid, c.name AS name, c.packageName AS packageName,
                   c.filePath AS filePath, c.startLine AS startLine, c.endLine AS endLine
            """;

    private final Driver driver;
    private final SourceSliceService sourceSliceService;

    @Override
    public ClassSourceResponse classSource(Long projectId, String uid, int maxLines) {
        if (projectId == null || uid == null || uid.isBlank()) {
            throw new BusinessException("缺少 projectId 或 uid");
        }
        int cap = Math.max(1, Math.min(maxLines, MAX_LINES_HARD));

        ClassMeta meta = findClass(projectId, uid);
        if (meta == null) {
            throw new BusinessException("类不存在: " + uid);
        }
        if (meta.filePath() == null || meta.filePath().isBlank()) {
            throw new BusinessException("图上没有该类的文件路径: " + uid);
        }

        Path extractDir = sourceSliceService.resolveExtractDir(projectId);
        String[] lines = sourceSliceService.readLines(extractDir, meta.filePath());

        int start = meta.startLine() == null ? 1 : Math.max(1, meta.startLine());
        if (start > lines.length) {
            throw new BusinessException("行号越界: " + start + "，文件共 " + lines.length + " 行");
        }
        int end = meta.endLine() == null ? lines.length : Math.min(meta.endLine(), lines.length);
        if (end < start) {
            throw new BusinessException("行号区间非法: " + start + "-" + end);
        }

        boolean truncated = false;
        if (end - start + 1 > cap) {
            end = start + cap - 1;
            truncated = true;
        }

        ClassSourceResponse resp = new ClassSourceResponse();
        resp.setUid(meta.uid());
        resp.setSimpleName(meta.name());
        resp.setPackageName(meta.packageName());
        resp.setFilePath(meta.filePath());
        resp.setStartLine(start);
        resp.setEndLine(end);
        resp.setFileLineCount(lines.length);
        resp.setContent(join(lines, start, end));
        resp.setTruncated(truncated);
        return resp;
    }

    private ClassMeta findClass(Long projectId, String uid) {
        try (Session session = driver.session()) {
            Result result = session.run(FIND_CLASS_SOURCE, Map.of("pid", projectId, "uid", uid));
            if (!result.hasNext()) {
                return null;
            }
            Record rec = result.next();
            return new ClassMeta(
                    rec.get("uid").asString(),
                    str(rec.get("name")),
                    str(rec.get("packageName")),
                    str(rec.get("filePath")),
                    intOrNull(rec.get("startLine")),
                    intOrNull(rec.get("endLine")));
        }
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

    private static String str(Value v) {
        return v == null || v.isNull() ? null : v.asString();
    }

    private static Integer intOrNull(Value v) {
        return v == null || v.isNull() ? null : (int) v.asLong();
    }

    private record ClassMeta(String uid, String name, String packageName, String filePath,
                             Integer startLine, Integer endLine) {
    }
}
