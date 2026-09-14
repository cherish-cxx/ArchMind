package com.example.archmind.service.ast;

import com.example.archmind.model.ast.ParsedFile;
import com.example.archmind.model.ast.ProjectIndex;

import java.util.List;

/**
 * 短类名 → 全限定名的消解器（C 阶段的基石）。
 * 消解顺序模拟 javac 的查找思路（实用近似，非 JLS 精确语义）：
 * 全限定名直查 → 单类型 import → 同包 → 通配 import → 全项目短名唯一（>1 视为歧义）。
 * 每次 parse 新建一个实例，非 Spring bean。
 */
public class TypeResolver {

    /** 消解结果三态 */
    public enum Kind {
        /** 唯一命中，qualifiedName 可用 */
        UNIQUE,
        /** 短名撞车，无法确定，丢弃 */
        AMBIGUOUS,
        /** 项目外（JDK/三方库），丢弃 */
        EXTERNAL
    }

    public record Resolution(Kind kind, String qualifiedName) {

        public static final Resolution AMBIGUOUS = new Resolution(Kind.AMBIGUOUS, null);
        public static final Resolution EXTERNAL = new Resolution(Kind.EXTERNAL, null);

        public static Resolution unique(String qualifiedName) {
            return new Resolution(Kind.UNIQUE, qualifiedName);
        }

        public boolean isUsable() {
            return kind == Kind.UNIQUE;
        }
    }

    private final ProjectIndex index;

    public TypeResolver(ProjectIndex index) {
        this.index = index;
    }

    /**
     * 消解一个类型名（可以是短名、全限定名、带泛型/数组后缀的原始写法）。
     *
     * @param rawTypeName A 阶段抽出的原始类型文本，如 "UserService"、"List<User>"、"com.x.Foo[]"
     * @param context     引用方所在的文件（import 与包名是文件级的，没有上下文就无法消解）
     */
    public Resolution resolve(String rawTypeName, ParsedFile context) {
        String name = clean(rawTypeName);
        if (name.isEmpty()) {
            return Resolution.EXTERNAL;
        }

        // ① 已含点号：当全限定名直查
        if (name.contains(".")) {
            return index.findClass(name).isPresent()
                    ? Resolution.unique(name) : Resolution.EXTERNAL;
        }

        if (context != null) {
            // ② 单类型 import 精确命中。Java 语义：import 即绑定——
            //    命中项目内则用之；指向项目外则这个短名就是外部类型，不再往同包/全局找
            for (String imp : context.getImports()) {
                if (imp.endsWith("." + name)) {
                    return index.findClass(imp).isPresent()
                            ? Resolution.unique(imp) : Resolution.EXTERNAL;
                }
            }

            // ③ 同包
            if (!context.getPackageName().isEmpty()) {
                String qn = context.getPackageName() + "." + name;
                if (index.findClass(qn).isPresent()) {
                    return Resolution.unique(qn);
                }
            }

            // ④ 通配 import："com.y.*" + name → "com.y.name"
            for (String imp : context.getImports()) {
                if (imp.endsWith(".*")) {
                    String qn = imp.substring(0, imp.length() - 1) + name;
                    if (index.findClass(qn).isPresent()) {
                        return Resolution.unique(qn);
                    }
                }
            }
        }

        // ⑤ 全项目短名：唯一才用（覆盖继承来的嵌套类型等无 import 引用），多个 = 歧义
        List<String> hits = index.findBySimpleName(name);
        if (hits == null) {
            return Resolution.EXTERNAL;
        }
        return hits.size() == 1 ? Resolution.unique(hits.get(0)) : Resolution.AMBIGUOUS;
    }

    /** 剥掉泛型实参 <...>、数组 [] 和可变参数 ... 后缀，拿裸类型名 */
    private String clean(String raw) {
        String t = raw == null ? "" : raw.trim();
        int lt = t.indexOf('<');
        if (lt >= 0) {
            t = t.substring(0, lt);
        }
        while (t.endsWith("[]")) {
            t = t.substring(0, t.length() - 2);
        }
        while (t.endsWith("...")) {
            t = t.substring(0, t.length() - 3);
        }
        return t.trim();
    }
}
