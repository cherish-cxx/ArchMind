package com.example.archmind.common.constant;

/**
 * 跨查询复用的 Cypher 片段。
 *
 * <p>顶层类判定：AST 里内部类的 uid 是「外层类名 + '.' + 简单类名」，
 * 只有顶层类满足 uid == 包名 + 简单类名。描述补齐与索引分页**必须用同一口径**，
 * 否则 remaining 和数量校验永远对不上。</p>
 */
public final class GraphPredicates {

    public static final String TOP_LEVEL =
            "c.uid = (CASE WHEN coalesce(c.packageName, '') = '' THEN c.name "
                    + "ELSE c.packageName + '.' + c.name END)";

    private GraphPredicates() {
    }
}
