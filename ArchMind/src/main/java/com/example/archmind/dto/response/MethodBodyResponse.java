package com.example.archmind.dto.response;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 方法源码切片。**「讲解深度」的唯一来源** —— 没有它，Agent 讲业务流程只能念签名。
 */
@Data
public class MethodBodyResponse {

    private List<MethodCode> methods = new ArrayList<>();

    /** 触到 maxLines / maxMethods 上限时为 true */
    private boolean truncated;

    /** 降级说明：例如某个方法的源码读不到 */
    private String note;

    @Data
    public static class MethodCode {
        private String uid;
        private String signature;
        /** 所属类全限定名 */
        private String owner;
        /** 行号**从第一个注解算起**（`@Override` / `@Transactional` 会被包含，javadoc 不会） */
        private int startLine;
        private int endLine;
        private String code;
        /** 源码读不到时为 null，原因见顶层 note */
        private String error;
        /**
         * 请求的是接口方法、实际切的是实现类代码时，这里填**原来的接口方法 uid**。
         * 接口方法没有方法体，直接切只能得到一行声明。
         */
        private String resolvedFrom;
    }
}
