package com.example.archmind.service.ast;

import com.example.archmind.model.ast.EdgeType;
import com.example.archmind.model.ast.ParsedCall;
import com.example.archmind.model.ast.ParsedClass;
import com.example.archmind.model.ast.ParsedEdge;
import com.example.archmind.model.ast.ParsedField;
import com.example.archmind.model.ast.ParsedFile;
import com.example.archmind.model.ast.ParsedMethod;
import com.example.archmind.model.ast.ParsedParam;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.ProjectIndex;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * C 阶段实现：拿 B 的索引给 A 的悬空调用/短名定名，产出边列表。
 * 纯内存计算、无 Spring、无 IO，每次 parse 新建一个实例。
 *
 * 原则：宁可漏不要错——消解失败、歧义、项目外目标一律丢弃，不猜测。
 */
public class RelationResolver {

    /** Object 的方法名：屏蔽，几乎全是噪声 */
    private static final Set<String> BUILTIN_METHOD_NAMES = Set.of(
            "equals", "hashCode", "toString", "getClass", "clone",
            "wait", "notify", "notifyAll", "finalize");

    private final ProjectIndex index;
    private final TypeResolver typeResolver;

    public RelationResolver(ProjectIndex index) {
        this.index = index;
        this.typeResolver = new TypeResolver(index);
    }

    /** 遍历全部类，产出去重后的边列表（顺序稳定，便于测试与排查） */
    public List<ParsedEdge> resolve(ParsedProject project) {
        Map<String, ParsedEdge> edges = new LinkedHashMap<>();
        for (ParsedFile pf : project.getFiles()) {
            for (ParsedClass pc : pf.getClasses()) {
                resolveClass(pc, pf, edges);
            }
        }
        return new ArrayList<>(edges.values());
    }

    // ==================== 类级边：EXTENDS / IMPLEMENTS / DEPENDS ====================

    private void resolveClass(ParsedClass pc, ParsedFile pf, Map<String, ParsedEdge> edges) {
        for (String raw : pc.getExtendsTypes()) {
            String target = usable(raw, pf);
            if (target != null) {
                addEdge(edges, new ParsedEdge(EdgeType.EXTENDS, pc.getQualifiedName(), target, 1, null));
            }
        }
        for (String raw : pc.getImplementsTypes()) {
            String target = usable(raw, pf);
            if (target != null) {
                addEdge(edges, new ParsedEdge(EdgeType.IMPLEMENTS, pc.getQualifiedName(), target, 1, null));
            }
        }

        // DEPENDS：字段类型 + 方法签名类型（返回/参数）。同一对类只留一条边，via 取首个来源
        for (ParsedField f : pc.getFields()) {
            String target = usable(f.type(), pf);
            if (target != null && !target.equals(pc.getQualifiedName())) {
                addEdge(edges, new ParsedEdge(EdgeType.DEPENDS, pc.getQualifiedName(), target, 1, "field"));
            }
        }
        for (ParsedMethod m : pc.getMethods()) {
            if (m.getReturnType() != null) {
                String target = usable(m.getReturnType(), pf);
                if (target != null && !target.equals(pc.getQualifiedName())) {
                    addEdge(edges, new ParsedEdge(EdgeType.DEPENDS, pc.getQualifiedName(), target, 1, "signature"));
                }
            }
            for (ParsedParam p : m.getParams()) {
                String target = usable(p.type(), pf);
                if (target != null && !target.equals(pc.getQualifiedName())) {
                    addEdge(edges, new ParsedEdge(EdgeType.DEPENDS, pc.getQualifiedName(), target, 1, "signature"));
                }
            }
        }

        // CALLS
        for (ParsedMethod m : pc.getMethods()) {
            resolveMethodCalls(pc, m, pf, edges);
        }
    }

    // ==================== CALLS：按 receiver 形态分派 ====================

    private void resolveMethodCalls(ParsedClass pc, ParsedMethod pm, ParsedFile pf,
                                    Map<String, ParsedEdge> edges) {
        String source = methodUid(pm);
        for (ParsedCall call : pm.getCalls()) {
            resolveCall(pc, pm, call, pf, source, edges);
        }
    }

    private void resolveCall(ParsedClass pc, ParsedMethod pm, ParsedCall call,
                             ParsedFile pf, String source, Map<String, ParsedEdge> edges) {
        String methodName = call.methodName();
        if (methodName == null || BUILTIN_METHOD_NAMES.contains(methodName)) {
            return;
        }

        String receiver = call.receiver();

        // 裸调用 / this：本类 + 继承层级。Java 作用域规则决定裸调用只可能到
        // 本类/继承方法/静态导入——绝不全项目乱匹配（静态导入 P1 不支持）
        if (receiver == null || "this".equals(receiver)) {
            findInHierarchy(pc.getQualifiedName(), methodName, call.argCount())
                    .ifPresent(t -> emitCall(edges, source, t));
            return;
        }

        // super：从各父类型开始找（不含本类），命中即停
        if ("super".equals(receiver)) {
            for (String parent : supertypes(pc, pf)) {
                Optional<ParsedMethod> hit = findInHierarchy(parent, methodName, call.argCount());
                if (hit.isPresent()) {
                    emitCall(edges, source, hit.get());
                    break;
                }
            }
            return;
        }

        // this.xxx / super.xxx：剥前缀后当变量查
        if (receiver.startsWith("this.")) {
            receiver = receiver.substring("this.".length());
        } else if (receiver.startsWith("super.")) {
            receiver = receiver.substring("super.".length());
        }

        // 复杂表达式（方法链、数组访问等）推不出类型
        if (receiver.contains("(") || receiver.contains("[")) {
            return;
        }

        // new Foo(...)：A 阶段存为 receiver=类名、method="<init>"；
        // 方法索引里构造器名是类名不是 "<init>"，直接查会落空，特判
        if ("<init>".equals(methodName)) {
            resolveConstructorCall(receiver, call, pf, source, edges);
            return;
        }

        // 变量 receiver：localTypes 定类型 → 在该类型的继承层级里找方法
        String varType = pm.getLocalTypes().get(receiver);
        if (varType != null) {
            String targetClass = usable(varType, pf);
            if (targetClass != null) {
                findInHierarchy(targetClass, methodName, call.argCount())
                        .ifPresent(t -> emitCall(edges, source, t));
            }
            return;
        }

        // 非变量 → 当类名（静态调用 UserService.create() / 全限定名）
        String targetClass = usable(receiver, pf);
        if (targetClass != null) {
            findInHierarchy(targetClass, methodName, call.argCount())
                    .ifPresent(t -> emitCall(edges, source, t));
        }
    }

    private void resolveConstructorCall(String receiver, ParsedCall call, ParsedFile pf,
                                        String source, Map<String, ParsedEdge> edges) {
        String targetClass = usable(receiver, pf);
        if (targetClass == null) {
            return;
        }
        ParsedClass target = index.findClass(targetClass).orElse(null);
        if (target == null) {
            return;
        }
        target.getMethods().stream()
                .filter(ParsedMethod::isConstructor)
                .filter(m -> m.getParams().size() == call.argCount())
                .findFirst()
                .ifPresent(ctor -> emitCall(edges, source, ctor));
    }

    private void emitCall(Map<String, ParsedEdge> edges, String source, ParsedMethod target) {
        addEdge(edges, new ParsedEdge(EdgeType.CALLS, source, methodUid(target), 1, null));
    }

    /**
     * 从 startClass 开始，BFS 其项目内超类型（extends + implements——接口方法也算继承来的；
     * 枚举的 implements 在 A 阶段存于 extendsTypes，天然覆盖），找 (methodName, argCount) 的声明。
     * visited 防脏数据成环。
     */
    private Optional<ParsedMethod> findInHierarchy(String startClass, String methodName, int argCount) {
        Deque<String> queue = new ArrayDeque<>();
        Set<String> visited = new HashSet<>();
        queue.add(startClass);
        while (!queue.isEmpty()) {
            String cur = queue.poll();
            if (!visited.add(cur)) {
                continue;
            }
            Optional<ParsedMethod> hit = index.findMethod(cur, methodName, argCount);
            if (hit.isPresent()) {
                return hit;
            }
            ParsedClass pc = index.findClass(cur).orElse(null);
            if (pc == null) {
                continue;
            }
            ParsedFile pf = index.fileOfClass(cur).orElse(null);
            for (String parent : supertypes(pc, pf)) {
                queue.add(parent);
            }
        }
        return Optional.empty();
    }

    /** 收集一个类的项目内超类型全限定名 */
    private List<String> supertypes(ParsedClass pc, ParsedFile pf) {
        List<String> result = new ArrayList<>();
        for (String raw : pc.getExtendsTypes()) {
            String qn = usable(raw, pf);
            if (qn != null) {
                result.add(qn);
            }
        }
        for (String raw : pc.getImplementsTypes()) {
            String qn = usable(raw, pf);
            if (qn != null) {
                result.add(qn);
            }
        }
        return result;
    }

    // ==================== 工具 ====================

    /** 消解成功返回全限定名，否则 null（项目外/歧义/空名） */
    private String usable(String rawType, ParsedFile pf) {
        TypeResolver.Resolution r = typeResolver.resolve(rawType, pf);
        return r.isUsable() ? r.qualifiedName() : null;
    }

    /** 方法唯一键：类全限定名 + "#" + 签名。签名含参数类型，重载不撞键 */
    private String methodUid(ParsedMethod m) {
        return m.getOwnerQualifiedName() + "#" + m.getSignature();
    }

    private void addEdge(Map<String, ParsedEdge> edges, ParsedEdge edge) {
        String key = edge.type() + "|" + edge.source() + "|" + edge.target();
        ParsedEdge existing = edges.get(key);
        if (existing == null) {
            edges.put(key, edge);
        } else if (edge.type() == EdgeType.CALLS) {
            // 同一对方法的多个调用点合并成一条边，count 累加
            edges.put(key, new ParsedEdge(edge.type(), edge.source(), edge.target(),
                    existing.count() + 1, edge.via()));
        }
        // 其余类型同键重复：保留首条即可
    }
}
