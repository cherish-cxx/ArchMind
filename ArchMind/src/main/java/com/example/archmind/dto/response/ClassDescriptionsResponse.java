package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 按需描述的返回体。
 *
 * <p>{@code skipped} 不是错误：不存在、类型不合适（接口/抽象/枚举）、已有描述、
 * LLM 漏答，都会进这里。前端据此避免反复请求同一批节点。</p>
 */
@Data
public class ClassDescriptionsResponse {

    private List<Item> items = List.of();

    private List<String> skipped = List.of();

    /** 本次超出批量上限、尚未处理的数量；前端可继续分批请求 */
    private int remaining;

    /** 降级说明（例如 LLM 暂时不可用）。前端只展示，不当作错误 */
    private String note;

    @Data
    public static class Item {
        private String uid;
        private String description;
        /** 是否分析阶段选出的核心类 */
        private Boolean core;
    }
}
