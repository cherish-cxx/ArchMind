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
        private String qualifiedName;
        private String description;
    }
}
