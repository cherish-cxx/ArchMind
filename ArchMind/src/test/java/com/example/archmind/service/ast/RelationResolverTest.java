package com.example.archmind.service.ast;

import com.example.archmind.model.ast.EdgeType;
import com.example.archmind.model.ast.ParsedEdge;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.impl.AstParserServiceImpl;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * C 阶段（关系解析）单测：走完整 parse()（A→B→C），断言产出的边。
 * 覆盖：变量/裸/静态/构造器调用、继承层级查找、外部与歧义丢弃、DEPENDS、count 合并。
 */
class RelationResolverTest {

    private final AstParserServiceImpl parser = new AstParserServiceImpl();

    private static List<ParsedEdge> edgesOf(ParsedProject p, EdgeType t) {
        return p.getEdges().stream().filter(e -> e.type() == t).toList();
    }

    @Test
    void variableReceiverCallProducesCallsEdge() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/UserController.java", """
                        package com.x;
                        public class UserController {
                            private UserService userService;
                            public void login() {
                                userService.save();
                            }
                        }
                        """),
                new SourceFileView(2L, "com/x/UserService.java", """
                        package com.x;
                        public class UserService {
                            public void save() {}
                        }
                        """)));

        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        assertEquals(1, calls.size());
        assertEquals("com.x.UserController#login()", calls.get(0).source());
        assertEquals("com.x.UserService#save()", calls.get(0).target());
        assertEquals(1, calls.get(0).count());

        // 字段类型是项目内类 → 顺带一条 DEPENDS
        List<ParsedEdge> deps = edgesOf(p, EdgeType.DEPENDS);
        assertEquals(1, deps.size());
        assertEquals("com.x.UserController", deps.get(0).source());
        assertEquals("com.x.UserService", deps.get(0).target());
        assertEquals("field", deps.get(0).via());
    }

    @Test
    void bareCallFindsOwnAndInheritedMethod() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/Base.java", """
                        package com.x;
                        public class Base {
                            public void setup() {}
                        }
                        """),
                new SourceFileView(2L, "com/x/Derived.java", """
                        package com.x;
                        public class Derived extends Base {
                            public void run() {
                                prepare();
                                setup();
                            }
                            private void prepare() {}
                        }
                        """)));

        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        // prepare() 命中本类；setup() 沿继承层级命中 Base
        assertEquals(2, calls.size());
        assertTrue(calls.stream().anyMatch(e ->
                "com.x.Derived#run()".equals(e.source()) && "com.x.Derived#prepare()".equals(e.target())));
        assertTrue(calls.stream().anyMatch(e ->
                "com.x.Derived#run()".equals(e.source()) && "com.x.Base#setup()".equals(e.target())));
    }

    @Test
    void externalAndComplexReceiversDropped() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/Holder.java", """
                        package com.x;
                        import java.util.ArrayList;
                        import java.util.List;
                        public class Holder {
                            private List<String> names;
                            public void go() {
                                names.add("x");
                                this.makeIt().save();
                            }
                            public Holder makeIt() { return this; }
                        }
                        """)));

        // names.add：import 指向项目外 → 无边
        // this.makeIt().save()：外层 save 的 receiver 是方法链，推不出类型 → 丢弃；
        // 但内层 this.makeIt() 本身是合法的本类调用，会被 findAll 抓到并正常命中
        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        assertEquals(1, calls.size());
        assertEquals("com.x.Holder#makeIt()", calls.get(0).target());
        // 字段 List<String> 也是项目外 → 无 DEPENDS
        assertTrue(edgesOf(p, EdgeType.DEPENDS).isEmpty());
    }

    @Test
    void ambiguousSimpleNameDropped() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/UserService.java", """
                        package com.x;
                        public class UserService { public void save() {} }
                        """),
                new SourceFileView(2L, "com/y/UserService.java", """
                        package com.y;
                        public class UserService { public void save() {} }
                        """),
                new SourceFileView(3L, "com/z/Client.java", """
                        package com.z;
                        public class Client {
                            private UserService userService;
                            public void run() {
                                userService.save();
                            }
                        }
                        """)));

        // com.z 里引用 "UserService"：无 import、不同包 → 全项目短名命中 2 个 = 歧义 → 丢弃
        assertTrue(edgesOf(p, EdgeType.CALLS).isEmpty());
        assertTrue(edgesOf(p, EdgeType.DEPENDS).isEmpty());
    }

    @Test
    void constructorCallResolvesToConstructor() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/Config.java", """
                        package com.x;
                        public class Config {
                            private String name;
                            public Config(String name) {
                                this.name = name;
                            }
                            public Config createDefault() {
                                return new Config("default");
                            }
                        }
                        """)));

        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        assertEquals(1, calls.size());
        assertEquals("com.x.Config#createDefault()", calls.get(0).source());
        // <init> 特判：目标定位到构造器（签名 = 类名 + 参数类型）
        assertEquals("com.x.Config#Config(String)", calls.get(0).target());
    }

    @Test
    void extendsAndImplementsEdges() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/BaseController.java", """
                        package com.x;
                        public class BaseController {}
                        """),
                new SourceFileView(2L, "com/x/UserService.java", """
                        package com.x;
                        public interface UserService { void save(); }
                        """),
                new SourceFileView(3L, "com/x/UserController.java", """
                        package com.x;
                        public class UserController extends BaseController implements UserService, java.io.Serializable {
                            public void save() {}
                        }
                        """)));

        List<ParsedEdge> ext = edgesOf(p, EdgeType.EXTENDS);
        assertEquals(1, ext.size());
        assertEquals("com.x.UserController", ext.get(0).source());
        assertEquals("com.x.BaseController", ext.get(0).target());

        // Serializable 是项目外 → 只有项目内接口留下 IMPLEMENTS
        List<ParsedEdge> impl = edgesOf(p, EdgeType.IMPLEMENTS);
        assertEquals(1, impl.size());
        assertEquals("com.x.UserService", impl.get(0).target());
    }

    @Test
    void sameCallPairAccumulatesCount() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/x/Client.java", """
                        package com.x;
                        public class Client {
                            private Svc svc;
                            public void run() {
                                svc.save();
                                this.svc.save();
                            }
                        }
                        """),
                new SourceFileView(2L, "com/x/Svc.java", """
                        package com.x;
                        public class Svc {
                            public void save() {}
                        }
                        """)));

        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        // svc.save() 与 this.svc.save() 是两个调用点，合并为一条边，count=2
        assertEquals(1, calls.size());
        assertEquals(2, calls.get(0).count());
        assertEquals("com.x.Svc#save()", calls.get(0).target());
    }

    @Test
    void staticCallOnClassName() {
        ParsedProject p = parser.parse(List.of(
                new SourceFileView(1L, "com/util/MathUtil.java", """
                        package com.util;
                        public class MathUtil {
                            public static int twice(int x) { return x * 2; }
                        }
                        """),
                new SourceFileView(2L, "com/app/Main.java", """
                        package com.app;
                        import com.util.MathUtil;
                        public class Main {
                            public int go() {
                                return MathUtil.twice(21);
                            }
                        }
                        """)));

        List<ParsedEdge> calls = edgesOf(p, EdgeType.CALLS);
        assertEquals(1, calls.size());
        assertEquals("com.app.Main#go()", calls.get(0).source());
        assertEquals("com.util.MathUtil#twice(int)", calls.get(0).target());
    }
}
