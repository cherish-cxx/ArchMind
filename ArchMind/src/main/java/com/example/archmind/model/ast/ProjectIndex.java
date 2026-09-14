package com.example.archmind.model.ast;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * B 阶段产物：全项目符号索引（"字典"）。
 * 把 A 阶段散落在 files → classes → methods 里的声明，整理成几张 O(1) 可查的表，
 * 供 C 阶段给悬空调用定名。
 *
 * 纯内存结构：parse() 里建、C 阶段用完即随 GC 回收——
 * 不进 Spring 容器、不落库（可随时从 ParsedProject 重建，只在消解期间被需要）。
 */
public class ProjectIndex {

    /** 全限定名 → 类声明。全限定名全项目唯一，是主索引 */
    private final Map<String, ParsedClass> byQualifiedName = new HashMap<>();

    /**
     * 简单名 → 全限定名列表。
     * 长度 1 = 唯一命中；>1 = 撞名歧义，C 阶段查到歧义必须丢弃（宁可漏不要错）。
     */
    private final Map<String, List<String>> bySimpleName = new HashMap<>();

    /** (所属类, 方法名, 参数个数) → 方法声明。同键重载后写覆盖先写，P1 接受 */
    private final Map<MethodKey, ParsedMethod> byMethod = new HashMap<>();

    /** 类全限定名 → 声明它的文件。C 阶段消解短类名（import/同包）时要用到该文件的上下文 */
    private final Map<String, ParsedFile> fileByClass = new HashMap<>();

    /**
     * 静态工厂：双重循环遍历 A 阶段产出，一次建满全部表。
     * B 阶段的全部工作就在这一个方法里——没有算法，只有"整理入册"。
     */
    public static ProjectIndex build(ParsedProject project) {
        ProjectIndex index = new ProjectIndex();
        for (ParsedFile pf : project.getFiles()) {
            for (ParsedClass pc : pf.getClasses()) {
                index.byQualifiedName.put(pc.getQualifiedName(), pc);
                index.bySimpleName
                        .computeIfAbsent(pc.getSimpleName(), k -> new ArrayList<>())
                        .add(pc.getQualifiedName());
                index.fileByClass.put(pc.getQualifiedName(), pf);
                for (ParsedMethod pm : pc.getMethods()) {
                    index.byMethod.put(
                            new MethodKey(pc.getQualifiedName(), pm.getName(), pm.getParams().size()),
                            pm);
                }
            }
        }
        return index;
    }

    // ==================== 查询 ====================

    /** 按全限定名找类 */
    public Optional<ParsedClass> findClass(String qualifiedName) {
        return Optional.ofNullable(byQualifiedName.get(qualifiedName));
    }

    /**
     * 按简单名找类。
     * 返回 null = 项目内没有这个短名（项目外）；长度 1 = 唯一命中；>1 = 歧义。
     */
    public List<String> findBySimpleName(String simpleName) {
        return bySimpleName.get(simpleName);
    }

    /** 按 (所属类, 方法名, 参数个数) 找方法 */
    public Optional<ParsedMethod> findMethod(String ownerQualifiedName, String name, int argCount) {
        return Optional.ofNullable(byMethod.get(new MethodKey(ownerQualifiedName, name, argCount)));
    }

    /** 找声明某个类的文件（取该文件的 import 列表和包名） */
    public Optional<ParsedFile> fileOfClass(String classQualifiedName) {
        return Optional.ofNullable(fileByClass.get(classQualifiedName));
    }

    // ==================== 规模（日志/调试用） ====================

    public int classCount() {
        return byQualifiedName.size();
    }

    public int methodCount() {
        return byMethod.size();
    }
}
