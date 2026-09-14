package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.FileEntityMapper;
import com.example.archmind.dao.ProjectSourceMapper;
import com.example.archmind.dao.neo4j.CodeGraphNeo4jWriter;
import com.example.archmind.entity.FileEntity;
import com.example.archmind.entity.ProjectSource;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.FileContentService;
import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.ast.AstParserService;
import com.example.archmind.service.ast.CodeGraphService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * 代码图谱装配：定位源码根 → 查 Java 文件 → 过滤 → 读内容 → 解析 → 写图。
 * 单个文件读/解析失败仅告警跳过，不拖垮整批；最后写图前会按 projectId 清旧图，重跑幂等。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class CodeGraphServiceImpl implements CodeGraphService {

    /** 超过该字节数跳过解析（编译产物 / 生成代码的典型特征） */
    private static final long MAX_FILE_BYTES = 1024L * 1024L;

    /** 路径中命中这些目录段则跳过（编译产物与 IDE/构建目录） */
    private static final Set<String> SKIP_DIR_SEGMENTS = Set.of(
            "target", "build", "out", "bin", ".git", ".idea", ".gradle");

    private final ProjectSourceMapper projectSourceMapper;
    private final FileEntityMapper fileEntityMapper;
    private final AstParserService astParserService;
    private final CodeGraphNeo4jWriter graphWriter;
    private final FileContentService fileContentService;

    @Override
    public void rebuild(Long projectId, ProgressReporter reporter) {
        Path extractDir = Paths.get(findSource(projectId).getContent());
        List<FileEntity> candidates = listJavaFiles(projectId);

        List<SourceFileView> views = new ArrayList<>();
        int total = candidates.size();
        for (int i = 0; i < total; i++) {
            FileEntity f = candidates.get(i);
            readFile(extractDir, f).ifPresent(views::add);
            reporter.report(i * 100 / total);
        }

        ParsedProject parsed = astParserService.parse(views);
        graphWriter.replaceProjectGraph(projectId, parsed);
        reporter.report(100);
        log.info("代码图谱重建完成 projectId={}, 解析文件={}, 节点边已写 Neo4j", projectId, views.size());
    }

    private ProjectSource findSource(Long projectId) {
        ProjectSource source = projectSourceMapper.selectOne(
                new LambdaQueryWrapper<ProjectSource>()
                        .eq(ProjectSource::getProjectId, projectId)
                        .isNotNull(ProjectSource::getFileId)
                        .orderByDesc(ProjectSource::getId)
                        .last("limit 1"));
        if (source == null || source.getContent() == null) {
            throw new BusinessException("该项目尚未上传源码或未完成扫描");
        }
        return source;
    }

    private List<FileEntity> listJavaFiles(Long projectId) {
        List<FileEntity> all = fileEntityMapper.selectList(
                new LambdaQueryWrapper<FileEntity>()
                        .eq(FileEntity::getProjectId, projectId)
                        .eq(FileEntity::getLanguage, "Java")
                        .isNotNull(FileEntity::getFilePath));
        return all.stream().filter(f -> !shouldSkip(f)).toList();
    }

    private boolean shouldSkip(FileEntity f) {
        String path = f.getFilePath();
        if (path == null) {
            return true;
        }
        String fileName = f.getFileName() == null ? "" : f.getFileName();
        if ("module-info.java".equals(fileName) || "package-info.java".equals(fileName)) {
            return true;
        }
        for (String segment : path.split("/")) {
            if (SKIP_DIR_SEGMENTS.contains(segment)) {
                return true;
            }
        }
        Long size = f.getFileSize();
        return size != null && size > MAX_FILE_BYTES;
    }

    private Optional<SourceFileView> readFile(Path extractDir, FileEntity f) {
        Path absolute = extractDir.resolve(f.getFilePath()).normalize();
        try {
            String content = fileContentService.readTextContent(absolute);
            return Optional.of(new SourceFileView(f.getId(), f.getFilePath(), content));
        } catch (Exception e) {
            log.warn("读取源码失败，跳过: {}", f.getFilePath(), e);
            return Optional.empty();
        }
    }
}
