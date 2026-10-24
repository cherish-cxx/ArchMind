package com.example.archmind.dto.response;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 类的源码视图：代码抽屉用。行号是 1-based，content 为切片后的纯文本。
 */
@Data
public class ClassSourceResponse {

    private String uid;
    private String simpleName;
    private String packageName;
    private String filePath;
    private Integer startLine;
    private Integer endLine;
    private Integer fileLineCount;
    private String content;
    /** 类跨度超过 maxLines 时只返回前 maxLines 行 */
    private Boolean truncated;

    /**
     * 方法大纲：按源码行号排序。
     *
     * <p>纯图数据，不读文件 —— 所以**不套** method-body 的 3 个方法上限。
     * 前端用它渲染可点击的目录，点中后把 {@code uid} 作为提问锚点。</p>
     */
    private List<MethodOutline> methods = new ArrayList<>();

    @Data
    public static class MethodOutline {
        /** {@code owner#signature}，与方法级 evidence / methodUid 是同一个字符串 */
        private String uid;
        private String name;
        private String signature;
        private String returnType;
        private String visibility;
        private Integer startLine;
        private Integer endLine;
        private boolean constructor;
    }
}
