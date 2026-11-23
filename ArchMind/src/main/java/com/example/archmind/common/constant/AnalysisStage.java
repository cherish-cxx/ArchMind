package com.example.archmind.common.constant;

public enum AnalysisStage {
    SCAN("文件扫描", 0, 5),
    AST("AST 提取", 5, 45),
    OVERVIEW("概况生成", 45, 58),
    DESCRIBE("核心类描述", 58, 75),
    DOMAIN("业务建模", 75, 100);

    private final String label;
    private final int start;
    private final int end;

    AnalysisStage(String label, int start, int end) {
        this.label = label; this.start = start; this.end = end;
    }

    /** 阶段内 0~100 → 全局 0~100 */
    public int progressAt(int percentWithinStage) {
        int p = Math.max(0, Math.min(100, percentWithinStage));
        return start + (end - start) * p / 100;
    }

    public String getLabel() { return label; }
    public int getStart() { return start; }
    public int getEnd()   { return end; }
}
