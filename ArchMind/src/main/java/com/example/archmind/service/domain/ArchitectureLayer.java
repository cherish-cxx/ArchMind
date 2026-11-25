package com.example.archmind.service.domain;

/**
 * 系统架构分层：六层固定顺序、总在前端可见（空层也输出）。
 * key 是稳定标识，可直接作为 CSS/DOM 与前端常量使用；label 是展示名。
 */
public enum ArchitectureLayer {
    ENTRY("entry", "用户入口层"),
    BUSINESS("business", "业务层"),
    DOMAIN_MODEL("domain-model", "领域模块层"),
    INFRASTRUCTURE("infrastructure", "基础设施"),
    EXTERNAL("external", "外部依赖"),
    OTHER("other", "未归类");

    private final String key;
    private final String label;

    ArchitectureLayer(String key, String label) {
        this.key = key;
        this.label = label;
    }

    public String key() {
        return key;
    }

    public String label() {
        return label;
    }

    /**
     * 类角色 → 层。大小写、连字符/下划线均可；null、空白与未知值都归入 {@link #OTHER}。
     */
    public static ArchitectureLayer fromRole(String role) {
        if (role == null || role.isBlank()) {
            return OTHER;
        }
        return switch (role.trim().toUpperCase(java.util.Locale.ROOT).replace('-', '_')) {
            case "ENTRY" -> ENTRY;
            case "BUSINESS" -> BUSINESS;
            case "DOMAIN_MODEL" -> DOMAIN_MODEL;
            case "INFRASTRUCTURE" -> INFRASTRUCTURE;
            case "EXTERNAL" -> EXTERNAL;
            default -> OTHER;
        };
    }
}
