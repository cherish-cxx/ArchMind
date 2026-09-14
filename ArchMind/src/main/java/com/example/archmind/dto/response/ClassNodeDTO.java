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

    /** 核心类的一句话简介（由核心类描述阶段生成，非核心类为 null） */
    private String description;

    private String filePath;

    private Integer startLine;

    private Integer endLine;
}
