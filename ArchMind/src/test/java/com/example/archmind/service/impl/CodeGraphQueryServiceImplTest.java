package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;
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

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * CodeGraphQueryServiceImpl 单测：mock 掉 Driver/Session/Result/Record 链，
 * 只验证「结果行 → DTO」的映射与类级聚合。不起 Spring、不连真 Neo4j。
 * Cypher 本身是否命中正确数据，需另靠真库冒烟。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class CodeGraphQueryServiceImplTest {

    @Mock
    private Driver driver;
    @Mock
    private Session session;

    private CodeGraphQueryServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new CodeGraphQueryServiceImpl(driver);
        when(driver.session()).thenReturn(session);
    }

    // ==================== 假数据构造 ====================

    private static Value strVal(String s) {
        Value v = mock(Value.class);
        when(v.isNull()).thenReturn(s == null);
        when(v.asString()).thenReturn(s);
        return v;
    }

    private static Value intVal(Integer i) {
        Value v = mock(Value.class);
        when(v.isNull()).thenReturn(i == null);
        if (i != null) {
            when(v.asLong()).thenReturn((long) i);
        }
        return v;
    }

    private static Value nodeVal(Map<String, Value> props) {
        Value v = mock(Value.class);
        when(v.isNull()).thenReturn(false);
        for (Map.Entry<String, Value> e : props.entrySet()) {
            when(v.get(e.getKey())).thenReturn(e.getValue());
        }
        return v;
    }

    private static Value classNode(String uid, String simple, String pkg, String kind) {
        Map<String, Value> props = new LinkedHashMap<>();
        props.put("uid", strVal(uid));
        props.put("name", strVal(simple));
        props.put("packageName", strVal(pkg));
        props.put("kind", strVal(kind));
        props.put("filePath", strVal("src/" + simple + ".java"));
        props.put("startLine", intVal(10));
        props.put("endLine", intVal(20));
        return nodeVal(props);
    }

    /** nodeKey 一列放类节点，其余列按 key=value 依次填（fromSig/toSig/via/rel 等） */
    private static Record record(String nodeKey, Value node, String... cols) {
        Record r = mock(Record.class);
        when(r.get(nodeKey)).thenReturn(node);
        for (int i = 0; i < cols.length; i += 2) {
            Value v = strVal(cols[i + 1]);
            when(r.get(cols[i])).thenReturn(v);
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
    void listClassesMapsNodes() {
        Value a = classNode("com.x.UserController", "UserController", "com.x", "CLASS");
        Value b = classNode("com.x.IUserApi", "IUserApi", "com.x", "INTERFACE");
        Result rows = resultOf(List.of(record("c", a), record("c", b)));
        when(session.run(anyString(), anyMap())).thenReturn(rows);

        List<ClassNodeDTO> list = service.listClasses(1L);

        assertEquals(2, list.size());
        assertEquals("com.x.UserController", list.get(0).getQualifiedName());
        assertEquals("UserController", list.get(0).getSimpleName());
        assertEquals("IUserApi", list.get(1).getSimpleName());
        assertEquals("INTERFACE", list.get(1).getKind());
    }

    @Test
    void classRelationsGroupsUpAndDownstream() {
        Value center = classNode("com.x.UserController", "UserController", "com.x", "CLASS");

        when(session.run(anyString(), anyMap())).thenAnswer(inv -> {
            String c = inv.getArgument(0, String.class);
            if (c.contains("uid: $uid}) RETURN c")) {
                return resultOf(List.of(record("c", center)));
            }
            if (c.contains("AS fromSig")) {
                // CALLEES 的 owner: $uid 在 m 上（紧贴箭头），CALLERS 的在 t 上
                if (c.contains("owner: $uid})-[r:CALLS]")) {
                    return resultOf(List.of(record("cc",
                            classNode("com.x.UserService", "UserService", "com.x", "CLASS"),
                            "fromSig", "login(String)", "toSig", "findByName(String)")));
                }
                return resultOf(List.of(record("cc",
                        classNode("com.x.LoginService", "LoginService", "com.x", "CLASS"),
                        "fromSig", "handleLogin(String)", "toSig", "login(String)")));
            }
            if (c.contains("AS via")) {
                if (c.contains("uid: $uid})-[r:DEPENDS]")) {
                    return resultOf(List.of(record("cc",
                            classNode("com.x.UserMapper", "UserMapper", "com.x", "INTERFACE"), "via", "field")));
                }
                return resultOf(List.of(record("cc",
                        classNode("com.x.AuthService", "AuthService", "com.x", "CLASS"), "via", "field")));
            }
            if (c.contains("AS rel")) {
                if (c.contains("uid: $uid})-[r:EXTENDS")) {
                    return resultOf(List.of(record("cc",
                            classNode("com.x.BaseController", "BaseController", "com.x", "CLASS"), "rel", "EXTENDS")));
                }
                return resultOf(List.of(record("cc",
                        classNode("com.x.AdminController", "AdminController", "com.x", "CLASS"), "rel", "EXTENDS")));
            }
            return resultOf(List.of());
        });

        ClassRelationsResponse resp = service.classRelations(1L, "com.x.UserController");

        assertEquals("UserController", resp.getCenter().getSimpleName());
        assertEquals(1, resp.getUpstream().getCallers().size());
        assertEquals("handleLogin(String) → login(String)",
                resp.getUpstream().getCallers().get(0).getEvidence().get(0));
        assertEquals(1, resp.getUpstream().getDependents().size());
        assertEquals(1, resp.getUpstream().getSubclasses().size());
        assertEquals("EXTENDS", resp.getUpstream().getSubclasses().get(0).getRelationType());
        assertEquals(1, resp.getDownstream().getCallees().size());
        assertEquals(1, resp.getDownstream().getDependencies().size());
        assertEquals(1, resp.getDownstream().getParents().size());
    }

    @Test
    void classRelationsThrowsWhenClassMissing() {
        Result empty = resultOf(List.of());
        when(session.run(anyString(), anyMap())).thenReturn(empty);

        assertThrows(BusinessException.class, () -> service.classRelations(1L, "com.x.Nope"));
    }
}
