package com.example.archmind.model.ast;

import com.example.archmind.service.impl.AstParserServiceImpl;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * ProjectIndex 单测：先跑 A 阶段解析内联源码，再建索引验证三张表的查询语义。
 * 不起 Spring、不碰磁盘和数据库。
 */
class ProjectIndexTest {

    private final AstParserServiceImpl parser = new AstParserServiceImpl();

    @Test
    void buildAndQueryBasicIndex() {
        String controller = """
                package com.x;
                public class UserController {
                    private UserService userService;
                    public void login() {
                        userService.save();
                    }
                }
                """;
        String service = """
                package com.x;
                public class UserService {
                    public void save() {}
                    public void save(String name) {}
                }
                """;

        ParsedProject project = parser.parse(List.of(
                new SourceFileView(1L, "com/x/UserController.java", controller),
                new SourceFileView(2L, "com/x/UserService.java", service)));
        ProjectIndex index = ProjectIndex.build(project);

        // 类索引：唯一主键
        assertEquals(2, index.classCount());
        assertTrue(index.findClass("com.x.UserService").isPresent());
        assertTrue(index.findClass("com.x.NoSuchService").isEmpty());

        // 简单名：唯一命中
        List<String> hits = index.findBySimpleName("UserService");
        assertNotNull(hits);
        assertEquals(1, hits.size());
        assertEquals("com.x.UserService", hits.get(0));

        // 简单名：项目外 → null
        assertNull(index.findBySimpleName("ArrayList"));

        // 方法索引：靠参数个数区分重载
        assertTrue(index.findMethod("com.x.UserService", "save", 0).isPresent());
        assertTrue(index.findMethod("com.x.UserService", "save", 1).isPresent());
        assertTrue(index.findMethod("com.x.UserService", "save", 2).isEmpty());

        // 方法总数：login + save×2 = 3
        assertEquals(3, index.methodCount());

        // 类 → 文件：能取回 import 上下文
        ParsedFile file = index.fileOfClass("com.x.UserService").orElseThrow();
        assertEquals("com/x/UserService.java", file.getRelativePath());
    }

    @Test
    void simpleNameCollisionMarksAmbiguity() {
        String a = """
                package com.x;
                public class UserService { public void save() {} }
                """;
        String b = """
                package com.y;
                public class UserService { public void save() {} }
                """;

        ParsedProject project = parser.parse(List.of(
                new SourceFileView(1L, "com/x/UserService.java", a),
                new SourceFileView(2L, "com/y/UserService.java", b)));
        ProjectIndex index = ProjectIndex.build(project);

        // 撞名：列表长度 2 = 歧义，C 阶段查到应丢弃
        List<String> hits = index.findBySimpleName("UserService");
        assertEquals(2, hits.size());
        assertTrue(hits.contains("com.x.UserService"));
        assertTrue(hits.contains("com.y.UserService"));

        // 但全限定名各自仍能查到（歧义只影响"从短名出发"，不影响"从全名出发"）
        assertTrue(index.findClass("com.x.UserService").isPresent());
        assertTrue(index.findClass("com.y.UserService").isPresent());

        // 两个同名方法分属不同类，方法键不冲突
        assertTrue(index.findMethod("com.x.UserService", "save", 0).isPresent());
        assertTrue(index.findMethod("com.y.UserService", "save", 0).isPresent());
    }

    @Test
    void nestedClassGetsDistinctQualifiedNames() {
        String src = """
                package com.x;
                public class Outer {
                    public class Inner {
                        public void go() {}
                    }
                    public void run() {}
                }
                """;

        ParsedProject project = parser.parse(List.of(
                new SourceFileView(1L, "com/x/Outer.java", src)));
        ProjectIndex index = ProjectIndex.build(project);

        // 嵌套类 A 阶段已打平，qualifiedName 各不相同
        assertEquals(2, index.classCount());
        assertTrue(index.findClass("com.x.Outer").isPresent());
        assertTrue(index.findClass("com.x.Outer.Inner").isPresent());
        assertTrue(index.findMethod("com.x.Outer.Inner", "go", 0).isPresent());
        assertTrue(index.findMethod("com.x.Outer", "run", 0).isPresent());

        // 两个类都来自同一个文件
        assertEquals("com/x/Outer.java",
                index.fileOfClass("com.x.Outer.Inner").orElseThrow().getRelativePath());
    }
}
