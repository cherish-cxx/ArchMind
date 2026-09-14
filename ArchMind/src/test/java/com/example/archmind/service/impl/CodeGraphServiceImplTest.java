package com.example.archmind.service.impl;

import com.example.archmind.dao.FileEntityMapper;
import com.example.archmind.dao.ProjectSourceMapper;
import com.example.archmind.dao.neo4j.CodeGraphNeo4jWriter;
import com.example.archmind.entity.FileEntity;
import com.example.archmind.entity.ProjectSource;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.FileContentService;
import com.example.archmind.service.ast.AstParserService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Captor;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.*;

/**
 * CodeGraphServiceImpl 单测：mock 掉全部 Mapper/解析器/写图器，
 * 只验证"文件筛选"与"进度上报"两件事。不起 Spring、不连真库。
 */
@ExtendWith(MockitoExtension.class)
class CodeGraphServiceImplTest {

    @Mock
    private ProjectSourceMapper projectSourceMapper;
    @Mock
    private FileEntityMapper fileEntityMapper;
    @Mock
    private AstParserService astParserService;
    @Mock
    private CodeGraphNeo4jWriter graphWriter;
    @Mock
    private FileContentService fileContentService;

    @InjectMocks
    private CodeGraphServiceImpl service;

    @Captor
    private ArgumentCaptor<List<SourceFileView>> viewsCaptor;

    private static FileEntity javaFile(Long id, String path, long size, String fileName) {
        FileEntity f = new FileEntity();
        f.setId(id);
        f.setProjectId(1L);
        f.setFileName(fileName);
        f.setFilePath(path);
        f.setFileSize(size);
        f.setLanguage("Java");
        return f;
    }

    @Test
    void filtersSkippedFilesAndParsesRest() {
        ProjectSource source = new ProjectSource();
        source.setContent("D:/code-workspace/1/1/source");
        when(projectSourceMapper.selectOne(any())).thenReturn(source);

        List<FileEntity> all = List.of(
                javaFile(1L, "src/main/java/com/x/A.java", 100, "A.java"),
                javaFile(2L, "target/classes/com/x/B.java", 100, "B.java"),   // target 跳过
                javaFile(3L, "src/main/java/module-info.java", 100, "module-info.java"), // 跳过
                javaFile(4L, "src/main/java/com/x/Big.java", 2_000_000, "Big.java"),     // 超大跳过
                javaFile(5L, "src/main/java/com/x/C.java", 100, "C.java"));
        when(fileEntityMapper.selectList(any())).thenReturn(all);
        when(astParserService.parse(any())).thenReturn(new ParsedProject());
        when(fileContentService.readTextContent(any())).thenReturn("package com.x; class X {}");

        service.rebuild(1L, pct -> {});

        verify(astParserService).parse(viewsCaptor.capture());
        List<SourceFileView> views = viewsCaptor.getValue();
        // 只留 A.java 与 C.java
        assertEquals(2, views.size());
        assertEquals(1L, views.get(0).fileId());
        assertEquals(5L, views.get(1).fileId());
        verify(graphWriter).replaceProjectGraph(eq(1L), any());
    }

    @Test
    void reportsProgressLinearlyAndEndsAt100() {
        ProjectSource source = new ProjectSource();
        source.setContent("D:/code-workspace/1/1/source");
        when(projectSourceMapper.selectOne(any())).thenReturn(source);

        when(fileEntityMapper.selectList(any())).thenReturn(List.of(
                javaFile(1L, "a/A.java", 100, "A.java"),
                javaFile(2L, "b/B.java", 100, "B.java")));
        when(astParserService.parse(any())).thenReturn(new ParsedProject());
        when(fileContentService.readTextContent(any())).thenReturn("class X {}");

        List<Integer> reported = new ArrayList<>();
        service.rebuild(1L, reported::add);

        // 2 个文件：i=0→0, i=1→50，最后 100
        assertEquals(List.of(0, 50, 100), reported);
    }

    @Test
    void missingSourceThrows() {
        when(projectSourceMapper.selectOne(any())).thenReturn(null);

        assertThrows(RuntimeException.class, () -> service.rebuild(1L, pct -> {}));
        verifyNoInteractions(astParserService, graphWriter);
    }

    @Test
    void readFailureSkipsFileButContinues() {
        ProjectSource source = new ProjectSource();
        source.setContent("D:/code-workspace/1/1/source");
        when(projectSourceMapper.selectOne(any())).thenReturn(source);
        when(fileEntityMapper.selectList(any())).thenReturn(List.of(
                javaFile(1L, "a/A.java", 100, "A.java"),
                javaFile(2L, "b/Broken.java", 100, "Broken.java")));
        when(fileContentService.readTextContent(any()))
                .thenReturn("class A {}")
                .thenThrow(new RuntimeException("not a text file"));
        when(astParserService.parse(any())).thenReturn(new ParsedProject());

        service.rebuild(1L, pct -> {});

        // 坏文件读失败被跳过，只剩 A.java
        verify(astParserService).parse(viewsCaptor.capture());
        assertEquals(1, viewsCaptor.getValue().size());
    }
}
