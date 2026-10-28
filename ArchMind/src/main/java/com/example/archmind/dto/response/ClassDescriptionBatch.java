package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 核心类描述批量生成的 LLM 输出：一次调用返回多个 {qualifiedName, description}。
 */
@Data
public class ClassDescriptionBatch {

    private List<Item> items;

    @Data
    public static class Item {
        /** 在本次 prompt 中的序号（1 起），批量补齐路径按它回填 uid */
        private Integer index;
        private String qualifiedName;
        private String description;
    }
}
