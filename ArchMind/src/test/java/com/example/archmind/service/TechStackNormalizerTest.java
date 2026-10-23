package com.example.archmind.service;

import com.example.archmind.dto.response.ProjectOverviewResponse;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

class TechStackNormalizerTest {

    private static ProjectOverviewResponse.TechStackItem item(String name, String category) {
        ProjectOverviewResponse.TechStackItem i = new ProjectOverviewResponse.TechStackItem();
        i.setName(name);
        i.setCategory(category);
        return i;
    }

    @Test
    void normalizesChineseAndEnglishAliases() {
        assertEquals("语言", TechStackNormalizer.normalizeCategory("Language"));
        assertEquals("语言", TechStackNormalizer.normalizeCategory(" 编程语言 "));
        assertEquals("框架", TechStackNormalizer.normalizeCategory("web framework"));
        assertEquals("数据库", TechStackNormalizer.normalizeCategory("Database"));
        assertEquals("构建工具", TechStackNormalizer.normalizeCategory("build tool"));
        assertEquals("测试", TechStackNormalizer.normalizeCategory("Testing"));
    }

    @Test
    void unknownOrBlankFallsBackToOther() {
        assertEquals("其他", TechStackNormalizer.normalizeCategory("运行时环境"));
        assertEquals("其他", TechStackNormalizer.normalizeCategory("   "));
        assertEquals("其他", TechStackNormalizer.normalizeCategory(null));
    }

    @Test
    void normalizeInPlaceHandlesNullListAndItems() {
        assertNull(TechStackNormalizer.normalizeInPlace(null));

        ProjectOverviewResponse.TechStackItem withNull = null;
        ProjectOverviewResponse.TechStackItem lang = item("Java", "Language");
        List<ProjectOverviewResponse.TechStackItem> items =
                new java.util.ArrayList<>(java.util.Arrays.asList(lang, withNull));

        TechStackNormalizer.normalizeInPlace(items);
        assertEquals("语言", items.get(0).getCategory());
    }
}
