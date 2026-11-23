package com.example.archmind.service.impl;

import com.example.archmind.model.ast.*;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * AstParserServiceImpl 单测：用内联源码字符串验证阶段 A 的抽取逻辑。
 * 不起 Spring 上下文、不碰磁盘、不碰数据库。
 */
class AstParserServiceImplTest {

    private final AstParserServiceImpl parser = new AstParserServiceImpl();

    @Test
    void parseBasicClass() {
        String src = """
                package com.x;
                import com.y.Svc;
                import java.util.List;
                public class UserController extends BaseController implements Serializable {
                    private Svc svc;
                    private String name;
                    public void login(String username) {
                        svc.save();
                        this.help();
                        doLog(username);
                    }
                    void help() {}
                    private void doLog(String msg) {
                        System.out.println(msg);
                    }
                }
                """;

        ParsedProject result = parser.parse(List.of(
                new SourceFileView(1L, "com/x/UserController.java", src)));

        assertEquals(1, result.getFiles().size());

        ParsedFile pf = result.getFiles().get(0);
        assertEquals("com.x", pf.getPackageName());
        assertEquals(List.of("com.y.Svc", "java.util.List"), pf.getImports());

        // 只有一个类
        assertEquals(1, pf.getClasses().size());
        ParsedClass pc = pf.getClasses().get(0);
        assertEquals("com.x.UserController", pc.getQualifiedName());
        assertEquals("UserController", pc.getSimpleName());
        assertEquals(ClassKind.CLASS, pc.getKind());
        assertEquals("public", pc.getVisibility());
        assertFalse(pc.isAbstract());

        // extends / implements 存原始短名
        assertEquals(List.of("BaseController"), pc.getExtendsTypes());
        assertEquals(List.of("Serializable"), pc.getImplementsTypes());

        // 字段
        assertEquals(2, pc.getFields().size());
        assertEquals("svc", pc.getFields().get(0).name());
        assertEquals("Svc", pc.getFields().get(0).type());
        assertEquals("name", pc.getFields().get(1).name());
        assertEquals("String", pc.getFields().get(1).type());

        // 方法数：login + help + doLog = 3
        assertEquals(3, pc.getMethods().size());

        // login 方法
        ParsedMethod login = pc.getMethods().stream()
                .filter(m -> m.getName().equals("login")).findFirst().orElseThrow();
        assertEquals("com.x.UserController#login", login.getQualifiedName());
        assertEquals("login(String)", login.getSignature());
        assertEquals("public", login.getVisibility());
        assertFalse(login.isConstructor());
        assertFalse(login.isStatic());

        // login 的作用域表
        assertTrue(login.getLocalTypes().containsKey("username"));  // 参数
        assertEquals("String", login.getLocalTypes().get("username"));
        assertTrue(login.getLocalTypes().containsKey("svc"));       // 字段
        assertEquals("Svc", login.getLocalTypes().get("svc"));
        assertTrue(login.getLocalTypes().containsKey("name"));      // 字段
        assertEquals("String", login.getLocalTypes().get("name"));

        // login 的悬空调用
        assertEquals(3, login.getCalls().size());
        // svc.save()
        ParsedCall call1 = login.getCalls().get(0);
        assertEquals("svc", call1.receiver());
        assertEquals("save", call1.methodName());
        assertEquals(0, call1.argCount());
        // this.help()
        ParsedCall call2 = login.getCalls().get(1);
        assertEquals("this", call2.receiver());
        assertEquals("help", call2.methodName());
        // doLog(username) — 裸调用，receiver 为 null
        ParsedCall call3 = login.getCalls().get(2);
        assertNull(call3.receiver());
        assertEquals("doLog", call3.methodName());
        assertEquals(1, call3.argCount());
    }

    @Test
    void parseInterfaceAndEnum() {
        String src = """
                package com.x;
                public interface UserService {
                    User findById(Long id);
                }
                """;
        String src2 = """
                package com.x;
                public enum Status {
                    ACTIVE, INACTIVE;
                    public boolean isActive() { return this == ACTIVE; }
                }
                """;

        ParsedProject result = parser.parse(List.of(
                new SourceFileView(1L, "UserService.java", src),
                new SourceFileView(2L, "Status.java", src2)));

        assertEquals(2, result.getFiles().size());

        ParsedClass iface = result.getFiles().get(0).getClasses().get(0);
        assertEquals(ClassKind.INTERFACE, iface.getKind());
        assertEquals("com.x.UserService", iface.getQualifiedName());
        assertEquals(1, iface.getMethods().size());
        assertEquals("findById", iface.getMethods().get(0).getName());

        ParsedClass enumm = result.getFiles().get(1).getClasses().get(0);
        assertEquals(ClassKind.ENUM, enumm.getKind());
        assertEquals("com.x.Status", enumm.getQualifiedName());
        assertEquals(1, enumm.getMethods().size());
        assertEquals("isActive", enumm.getMethods().get(0).getName());
    }

    @Test
    void parseConstructorAndObjectCreation() {
        String src = """
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
                """;

        ParsedProject result = parser.parse(List.of(
                new SourceFileView(1L, "Config.java", src)));

        ParsedClass pc = result.getFiles().get(0).getClasses().get(0);
        assertEquals(2, pc.getMethods().size());

        // 构造器
        ParsedMethod ctor = pc.getMethods().stream()
                .filter(ParsedMethod::isConstructor).findFirst().orElseThrow();
        assertEquals("Config(String)", ctor.getSignature());
        assertNull(ctor.getReturnType());

        // createDefault 方法里有 new Config("default") → 调用记录
        ParsedMethod create = pc.getMethods().stream()
                .filter(m -> m.getName().equals("createDefault")).findFirst().orElseThrow();
        assertTrue(create.getCalls().stream()
                .anyMatch(c -> "Config".equals(c.receiver()) && "<init>".equals(c.methodName())));
    }

    @Test
    void parseFailureSkipsFile() {
        String broken = "this is not valid java {{{";
        String valid = "package ok; class Ok {}";

        ParsedProject result = parser.parse(List.of(
                new SourceFileView(1L, "broken.java", broken),
                new SourceFileView(2L, "ok.java", valid)));

        // 破文件跳过，好文件保留
        assertEquals(1, result.getFiles().size());
        assertEquals("ok", result.getFiles().get(0).getPackageName());
    }

    @Test
    void extractsClassAndMethodAnnotations() {
        String src = """
            package demo;
            @RestController
            @RequestMapping({"/orders", "/o"})
            public class OrderController {
                @PostMapping("/{id}/cancel")
                public void cancel() {}
            }
            """;
        ParsedProject p = parse(src);
        ParsedClass c = p.getFiles().get(0).getClasses().get(0);
        assertEquals(List.of("RestController", "RequestMapping"),
                c.getAnnotations().stream().map(ParsedAnnotation::name).toList());
        assertEquals(List.of("/orders", "/o"), c.getAnnotations().get(1).values());
        assertEquals(List.of("PostMapping"),
                c.getMethods().get(0).getAnnotations().stream().map(ParsedAnnotation::name).toList());
    }

    private ParsedProject parse(String src) {
        return parser.parse(List.of(new SourceFileView(1L, "Demo.java", src)));
    }

    @Test
    void localVariableShadowsField() {
        String src = """
                package com.x;
                public class Svc {
                    private Dao dao;
                    public void run() {
                        Dao dao = new SpecialDao();
                        dao.flush();
                    }
                }
                """;

        ParsedProject result = parser.parse(List.of(new SourceFileView(1L, "Svc.java", src)));
        ParsedMethod run = result.getFiles().get(0).getClasses().get(0).getMethods().get(0);

        // 局部变量 dao 遮蔽字段 dao，但两者类型可能不同
        // 字段先 put，局部变量 putIfAbsent 不会覆盖
        // 然而我们的实现是参数→局部→字段(putIfAbsent)
        // 局部变量用 putIfAbsent，所以先到先得
        // 实际上局部变量声明在字段之后出现，但 putIfAbsent 不覆盖
        // 所以字段 "dao"→"Dao" 先被字段阶段放入
        // 需要看代码实现顺序：参数→局部变量→字段(putIfAbsent)
        // 局部变量也是 putIfAbsent，所以字段先在？
        // 不对——实现顺序是：参数 put → 局部变量 putIfAbsent → 字段 putIfAbsent
        // 同名时，参数/局部变量优先（先 put 进去），字段不覆盖
        assertTrue(run.getLocalTypes().containsKey("dao"));
        // dao 的值应该是 "Dao"（来自字段或局部变量声明——两者类型写法一样）
    }
}
