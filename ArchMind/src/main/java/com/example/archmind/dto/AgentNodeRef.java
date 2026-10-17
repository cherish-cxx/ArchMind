package com.example.archmind.dto;

import lombok.Data;

/**
 * 指向图里一个实体的引用（类的全限定名 / 方法 uid）。
 *
 * <p>请求与响应共用：前端在 {@code /ask} 里用它当锚点（{@code focus}），
 * Python 在 {@code uiCommands[].target} 里用它指出「要聚焦谁」。</p>
 *
 * <p>{@code type} 是字符串而不是枚举 —— Python 侧 {@code NodeRefType} 只有
 * CLASS / METHOD，但**响应里的 {@code meta.intent} 会带枚举外的值**
 * （{@code SESSION_GONE} 等），这个项目已经因为「用枚举接 Python 的字符串」
 * 吃过一次亏，凡是 Python 来的字段一律用 String 接。</p>
 */
@Data
public class AgentNodeRef {
    private String type;
    private String uid;
}
