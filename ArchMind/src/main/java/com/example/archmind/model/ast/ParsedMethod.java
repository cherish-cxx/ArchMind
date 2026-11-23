package com.example.archmind.model.ast;

import lombok.Data;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 一个方法的声明 + 悬空调用 + 作用域类型表。
 * calls 和 localTypes 在 A 阶段填充；C 阶段读 calls、用 localTypes 去定名。
 */
@Data
public class ParsedMethod {

    /** 所属类的全限定名，用于拼 qualifiedName */
    private String ownerQualifiedName;

    private String name;

    /** 全限定名，如 "com.x.UserController#login" */
    private String qualifiedName;

    /** 签名，如 "login(String,UserService)"，用于区分重载 */
    private String signature;

    /** 返回类型的原始写法；构造器为 null */
    private String returnType;

    private List<ParsedParam> params = new ArrayList<>();

    private int startLine;
    private int endLine;

    private String visibility;

    private boolean isStatic;
    private boolean isConstructor;
    private boolean isAbstract;

    /** 方法上声明的注解，源码顺序 */
    private List<ParsedAnnotation> annotations = new ArrayList<>();

    /** A 阶段抽出的悬空调用，C 阶段的原料 */
    private List<ParsedCall> calls = new ArrayList<>();

    /**
     * 作用域类型表：变量名 → 原始类型名（短名）。
     * 来源：参数 + 局部变量 + 本类字段（父类字段留给 C 阶段沿 extends 链查）。
     * C 阶段拿这张表去消解 "userService.save()" 里的 userService 是什么类型。
     */
    private Map<String, String> localTypes = new HashMap<>();
}
