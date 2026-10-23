package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectSourceMapper;
import com.example.archmind.entity.ProjectSource;
import com.example.archmind.service.FileContentService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.nio.file.Path;
import java.nio.file.Paths;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class SourceSliceServiceImplTest {

    @Mock
    private ProjectSourceMapper projectSourceMapper;
    @Mock
    private FileContentService fileContentService;

    private SourceSliceServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new SourceSliceServiceImpl(projectSourceMapper, fileContentService);
    }

    private static Path workDir() {
        return Paths.get("build", "tmp", "workspace", "1", "2", "source")
                .toAbsolutePath().normalize();
    }

    @Test
    void resolveExtractDirUsesProjectSourceContent() {
        ProjectSource source = new ProjectSource();
        source.setContent(workDir().toString());
        when(projectSourceMapper.selectOne(any())).thenReturn(source);

        assertEquals(workDir(), service.resolveExtractDir(1L));
    }

    @Test
    void resolveExtractDirThrowsWhenSourceMissing() {
        when(projectSourceMapper.selectOne(any())).thenReturn(null);
        assertThrows(BusinessException.class, () -> service.resolveExtractDir(1L));
    }

    @Test
    void readLinesRejectsPathEscape() {
        assertThrows(BusinessException.class, () -> service.readLines(workDir(), "../secret.txt"));
        verifyNoInteractions(fileContentService);
    }

    @Test
    void readLinesReadsTextInsideWorkspace() {
        when(fileContentService.readTextContent(any())).thenReturn("class A {\n}");

        String[] lines = service.readLines(workDir(), "src/A.java");

        assertArrayEquals(new String[]{"class A {", "}"}, lines);
    }
}
