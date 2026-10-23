package com.example.archmind.dao;

import lombok.Data;

/**
 * file 表按语言聚合的投影行。
 */
@Data
public class LanguageCount {
    private String language;
    private Long cnt;
}
