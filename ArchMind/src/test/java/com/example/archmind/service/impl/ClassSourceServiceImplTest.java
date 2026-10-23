package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassSourceResponse;
import com.example.archmind.service.SourceSliceService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Record;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.neo4j.driver.Values;

import java.nio.file.Path;
import java.nio.file.Paths;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ClassSourceServiceImplTest {

    @Mock
    private Driver driver;
    @Mock
    private Session session;
    @Mock
    private Result result;
    @Mock
    private Record record;
    @Mock
    private SourceSliceService sourceSliceService;

    private ClassSourceServiceImpl service;

    private static final Path DIR = Paths.get("build", "tmp", "source").toAbsolutePath().normalize();

    @BeforeEach
    void setUp() {
        service = new ClassSourceServiceImpl(driver, sourceSliceService);
        when(driver.session()).thenReturn(session);
        when(session.run(anyString(), anyMap())).thenReturn(result);
    }

    private static Value strVal(String s) {
        return Values.value(s);
    }

    private static Value intVal(Integer i) {
        return Values.value(i.longValue());
    }

    private void stubClassRow() {
        when(result.hasNext()).thenReturn(true);
        when(result.next()).thenReturn(record);
        when(record.get("uid")).thenReturn(strVal("com.x.Foo"));
        when(record.get("name")).thenReturn(strVal("Foo"));
        when(record.get("packageName")).thenReturn(strVal("com.x"));
        when(record.get("filePath")).thenReturn(strVal("src/Foo.java"));
        when(record.get("startLine")).thenReturn(intVal(3));
        when(record.get("endLine")).thenReturn(intVal(5));
        when(sourceSliceService.resolveExtractDir(1L)).thenReturn(DIR);
        when(sourceSliceService.readLines(DIR, "src/Foo.java"))
                .thenReturn(new String[]{"l1", "l2", "l3", "l4", "l5", "l6"});
    }

    @Test
    void slicesClassRange() {
        stubClassRow();

        ClassSourceResponse resp = service.classSource(1L, "com.x.Foo", 2000);

        assertEquals("com.x.Foo", resp.getUid());
        assertEquals("Foo", resp.getSimpleName());
        assertEquals("src/Foo.java", resp.getFilePath());
        assertEquals(3, resp.getStartLine());
        assertEquals(5, resp.getEndLine());
        assertEquals(6, resp.getFileLineCount());
        assertEquals("l3\nl4\nl5", resp.getContent());
        assertFalse(resp.getTruncated());
    }

    @Test
    void truncatesWhenClassExceedsMaxLines() {
        stubClassRow();

        ClassSourceResponse resp = service.classSource(1L, "com.x.Foo", 2);

        assertEquals(3, resp.getStartLine());
        assertEquals(4, resp.getEndLine());
        assertEquals("l3\nl4", resp.getContent());
        assertTrue(resp.getTruncated());
    }

    @Test
    void throwsWhenClassNotFound() {
        when(result.hasNext()).thenReturn(false);

        assertThrows(BusinessException.class,
                () -> service.classSource(1L, "com.x.Missing", 2000));
        verifyNoInteractions(sourceSliceService);
    }
}
