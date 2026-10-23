package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectSourceMapper;
import com.example.archmind.entity.ProjectSource;
import com.example.archmind.service.FileContentService;
import com.example.archmind.service.SourceSliceService;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 源码读取实现：解压根目录来自 project_source.content —— 与概况/方法切片用的是同一套约定。
 */
@Service
@RequiredArgsConstructor
public class SourceSliceServiceImpl implements SourceSliceService {

    private final ProjectSourceMapper projectSourceMapper;
    private final FileContentService fileContentService;

    @Override
    public Path resolveExtractDir(Long projectId) {
        ProjectSource source = projectSourceMapper.selectOne(
                new LambdaQueryWrapper<ProjectSource>()
                        .eq(ProjectSource::getProjectId, projectId)
                        .isNotNull(ProjectSource::getFileId)
                        .orderByDesc(ProjectSource::getId)
                        .last("limit 1"));
        if (source == null || source.getContent() == null) {
            throw new BusinessException("该项目尚未上传源码或未完成扫描");
        }
        return Paths.get(source.getContent()).toAbsolutePath().normalize();
    }

    @Override
    public String[] readLines(Path extractDir, String relativePath) {
        if (relativePath == null || relativePath.isBlank()) {
            throw new BusinessException("源码路径为空，无法定位文件");
        }
        // filePath 来自自家库，但 normalize 之后仍须落在项目解压目录内
        Path absolute = extractDir.resolve(relativePath).normalize();
        if (!absolute.startsWith(extractDir)) {
            throw new BusinessException("路径越出项目工作区: " + relativePath);
        }
        // readTextContent 自带 NOFOLLOW_LINKS 与 10MB 上限，超限/二进制会抛 BusinessException
        return fileContentService.readTextContent(absolute).split("\\R", -1);
    }
}
