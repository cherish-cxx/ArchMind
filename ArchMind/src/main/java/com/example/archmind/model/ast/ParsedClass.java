package com.example.archmind.model.ast;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 一个类/接口/枚举的声明。
 * extendsTypes / implementsTypes 存原始短名（如 "BaseController"），消解留给 C 阶段。
 */
@Data
public class ParsedClass {

    /** 全限定名，如 "com.x.UserController"，全项目唯一，当主键 */
    private String qualifiedName;

    /** 简单类名，如 "UserController"，给类名别名索引 */
    private String simpleName;

    private String packageName;

    private ClassKind kind;

    /** 来自 file 表，最终写入 Neo4j Class 节点的 fileId 属性 */
    private Long fileId;

    /** 相对源码根路径，如 "com/x/UserController.java" */
    private String filePath;

    private int startLine;
    private int endLine;

    private String visibility;

    private boolean isAbstract;

    /** 原始短名，如 ["BaseController"]，C 阶段消解成全限定名后产 EXTENDS 边 */
    private List<String> extendsTypes = new ArrayList<>();

    /** 原始短名，如 ["Serializable"]，C 阶段消解后产 IMPLEMENTS 边 */
    private List<String> implementsTypes = new ArrayList<>();

    /** 类上声明的注解，源码顺序 */
    private List<ParsedAnnotation> annotations = new ArrayList<>();

    private List<ParsedField> fields = new ArrayList<>();

    private List<ParsedMethod> methods = new ArrayList<>();
}
