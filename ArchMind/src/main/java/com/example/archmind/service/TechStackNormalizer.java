package com.example.archmind.service;

import com.example.archmind.dto.response.ProjectOverviewResponse;

import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 技术栈 category 归一化：LLM 会自由生成 "Language / 编程语言 / framework" 等写法，
 * 前端只认封闭枚举。写入与读取两侧都走这里，兼容历史数据。
 */
public final class TechStackNormalizer {

    public static final String LANGUAGE = "语言";
    public static final String FRAMEWORK = "框架";
    public static final String MIDDLEWARE = "中间件";
    public static final String DATABASE = "数据库";
    public static final String BUILD_TOOL = "构建工具";
    public static final String TESTING = "测试";
    public static final String TOOL = "工具";
    public static final String OTHER = "其他";

    /** 全部小写；中文与英文别名都收敛到同一个封闭枚举 */
    private static final Map<String, String> ALIASES = Map.ofEntries(
            Map.entry("语言", LANGUAGE),
            Map.entry("language", LANGUAGE),
            Map.entry("languages", LANGUAGE),
            Map.entry("programming language", LANGUAGE),
            Map.entry("programminglanguage", LANGUAGE),
            Map.entry("编程语言", LANGUAGE),
            Map.entry("开发语言", LANGUAGE),
            Map.entry("lang", LANGUAGE),

            Map.entry("框架", FRAMEWORK),
            Map.entry("framework", FRAMEWORK),
            Map.entry("frameworks", FRAMEWORK),
            Map.entry("web framework", FRAMEWORK),
            Map.entry("开发框架", FRAMEWORK),
            Map.entry("应用框架", FRAMEWORK),
            Map.entry("orm", FRAMEWORK),

            Map.entry("中间件", MIDDLEWARE),
            Map.entry("middleware", MIDDLEWARE),
            Map.entry("消息队列", MIDDLEWARE),
            Map.entry("message queue", MIDDLEWARE),
            Map.entry("mq", MIDDLEWARE),
            Map.entry("缓存", MIDDLEWARE),
            Map.entry("cache", MIDDLEWARE),

            Map.entry("数据库", DATABASE),
            Map.entry("database", DATABASE),
            Map.entry("databases", DATABASE),
            Map.entry("db", DATABASE),
            Map.entry("存储", DATABASE),
            Map.entry("storage", DATABASE),

            Map.entry("构建工具", BUILD_TOOL),
            Map.entry("构建", BUILD_TOOL),
            Map.entry("build", BUILD_TOOL),
            Map.entry("build tool", BUILD_TOOL),
            Map.entry("buildtool", BUILD_TOOL),
            Map.entry("包管理", BUILD_TOOL),
            Map.entry("package manager", BUILD_TOOL),
            Map.entry("依赖管理", BUILD_TOOL),
            Map.entry("dependency management", BUILD_TOOL),

            Map.entry("测试", TESTING),
            Map.entry("test", TESTING),
            Map.entry("tests", TESTING),
            Map.entry("testing", TESTING),
            Map.entry("测试框架", TESTING),
            Map.entry("test framework", TESTING),

            Map.entry("工具", TOOL),
            Map.entry("tool", TOOL),
            Map.entry("tools", TOOL),
            Map.entry("utility", TOOL),
            Map.entry("开发工具", TOOL),

            Map.entry("其他", OTHER),
            Map.entry("other", OTHER),
            Map.entry("others", OTHER),
            Map.entry("misc", OTHER),
            Map.entry("miscellaneous", OTHER)
    );

    private TechStackNormalizer() {
    }

    public static String normalizeCategory(String raw) {
        if (raw == null || raw.isBlank()) {
            return OTHER;
        }
        String key = raw.trim().toLowerCase(Locale.ROOT).replaceAll("\\s+", " ");
        return ALIASES.getOrDefault(key, OTHER);
    }

    /** 原地归一化，null 安全；返回值便于链式调用 */
    public static List<ProjectOverviewResponse.TechStackItem> normalizeInPlace(
            List<ProjectOverviewResponse.TechStackItem> items) {
        if (items == null) {
            return null;
        }
        for (ProjectOverviewResponse.TechStackItem item : items) {
            if (item != null) {
                item.setCategory(normalizeCategory(item.getCategory()));
            }
        }
        return items;
    }
}
