package com.example.archmind.service.impl;

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
import org.springframework.ai.chat.model.ChatModel;

import java.util.Iterator;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * ClassDescriptionServiceImpl 单测：只测不依赖真实 LLM 的部分——
 * buildPrompt 的格式与截断、queryCoreClasses 的结果映射与骨架填充。不起 Spring、不连真库。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ClassDescriptionServiceImplTest {

    @Mock
    private Driver driver;
    @Mock
    private Session session;
    @Mock
    private ChatModel chatModel;

    private ClassDescriptionServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new ClassDescriptionServiceImpl(driver, chatModel);
        when(driver.session()).thenReturn(session);
    }

    // ==================== 假数据构造 ====================

    private static Value strVal(String s) {
        Value v = mock(Value.class);
        when(v.isNull()).thenReturn(s == null);
        when(v.asString()).thenReturn(s);
        return v;
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static Value listVal(List<String> items) {
        Value v = mock(Value.class);
        when(v.isNull()).thenReturn(false);
        doReturn(items).when(v).asList(any(java.util.function.Function.class));
        return v;
    }

    private static Record record(Map<String, Value> cols) {
        Record r = mock(Record.class);
        for (Map.Entry<String, Value> e : cols.entrySet()) {
            when(r.get(e.getKey())).thenReturn(e.getValue());
        }
        return r;
    }

    private static Result resultOf(List<Record> records) {
        Result res = mock(Result.class);
        Iterator<Record> it = records.iterator();
        when(res.hasNext()).thenAnswer(inv -> it.hasNext());
        when(res.next()).thenAnswer(inv -> it.next());
        return res;
    }

    // ==================== 测试 ====================

    @Test
    void buildPromptIncludesSkeletonAndTruncatesMethods() {
        List<String> many = java.util.stream.IntStream.rangeClosed(1, 12)
                .mapToObj(i -> "m" + i + "()").toList();
        List<ClassDescriptionServiceImpl.CoreClass> cores = List.of(
                new ClassDescriptionServiceImpl.CoreClass("com.x.UserService", "UserService", "com.x", "CLASS",
                        List.of("BaseService"), List.of("login(String)", "logout()")),
                new ClassDescriptionServiceImpl.CoreClass("com.x.Util", "Util", "com.x", "CLASS",
                        List.of(), many));

        String prompt = service.buildPrompt(cores);

        assertTrue(prompt.contains("com.x.UserService"));
        assertTrue(prompt.contains("BaseService"));
        assertTrue(prompt.contains("login(String)"));
        assertTrue(prompt.contains("\"items\""));
        // 方法截断到 8 个
        assertTrue(prompt.contains("m8()"));
        assertFalse(prompt.contains("m9()"));
    }

    @Test
    void queryCoreClassesReturnsEmptyWhenNoClasses() {
        Result empty = resultOf(List.of());
        when(session.run(anyString(), anyMap())).thenReturn(empty);

        assertTrue(service.queryCoreClasses(1L, 20).isEmpty());
    }

    @Test
    void queryCoreClassesMapsAndFillsSkeleton() {
        Result top = resultOf(List.of(
                record(Map.of("uid", strVal("com.x.UserService"), "name", strVal("UserService"),
                        "pkg", strVal("com.x"), "kind", strVal("CLASS"))),
                record(Map.of("uid", strVal("com.x.UserMapper"), "name", strVal("UserMapper"),
                        "pkg", strVal("com.x"), "kind", strVal("INTERFACE")))));
        Result skel = resultOf(List.of(
                record(Map.of("uid", strVal("com.x.UserService"),
                        "parents", listVal(List.of("BaseService")),
                        "methods", listVal(List.of("login(String)")))),
                record(Map.of("uid", strVal("com.x.UserMapper"),
                        "parents", listVal(List.of()),
                        "methods", listVal(List.of("findByName(String)"))))));

        when(session.run(contains("ORDER BY score"), anyMap())).thenReturn(top);
        when(session.run(contains("collect"), anyMap())).thenReturn(skel);

        List<ClassDescriptionServiceImpl.CoreClass> cores = service.queryCoreClasses(1L, 20);

        assertEquals(2, cores.size());
        assertEquals("com.x.UserService", cores.get(0).qualifiedName());
        assertEquals(List.of("BaseService"), cores.get(0).parents());
        assertEquals(List.of("login(String)"), cores.get(0).methods());
        assertEquals("INTERFACE", cores.get(1).kind());
        assertEquals(List.of("findByName(String)"), cores.get(1).methods());
    }
}
