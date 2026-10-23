package com.example.archmind.dto.response;

import lombok.Data;

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
}
