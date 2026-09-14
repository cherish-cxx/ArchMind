package com.example.archmind.model.ast;

/**
 * 边类型：CALLS 连接方法（源/目标是方法唯一键），其余三种连接类（全限定名）。
 */
public enum EdgeType {
    /** 方法调用（含构造器调用） */
    CALLS,
    /** 继承：class A extends B */
    EXTENDS,
    /** 实现：class A implements B */
    IMPLEMENTS,
    /** 依赖：字段类型 / 方法签名类型用到目标类 */
    DEPENDS
}
