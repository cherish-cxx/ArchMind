package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 代码图中的一个类节点（含接口/枚举），用于「代码探索」的类清单与关联项。
 */
@Data
public class ClassNodeDTO {

    /** 全限定名，即图中节点的 uid */
    private String qualifiedName;

    private String simpleName;

    private String packageName;

    /** CLASS / INTERFACE / ENUM */
    private String kind;

    /** 一句话简介。核心类由分析阶段写入，其余类由按需描述接口写入；未生成为 null */
    private String description;

    /** 是否抽象类。按需描述只覆盖实现类，用它过滤 */
    private Boolean isAbstract;

    /** 是否分析阶段选出的核心类。读取时按关联度排名推导，不落库 */
    private Boolean core;

    private String filePath;

    private Integer startLine;

    private Integer endLine;
}
