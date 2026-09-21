package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 工具返回的「真实来源凭据」。
 *
 * <p>它是「禁止编造」从 prompt 口号变成可校验机制的关键：Agent 返回的
 * {@code citations[].uid} / {@code uiCommands[].target.uid} 必须出现在本轮 evidence 里，
 * 未命中即可丢弃，不必依赖 LLM 自觉。</p>
 */
@Data
public class Evidence {

    /** EDGE / CLASS / METHOD / FILE */
    private String kind;

    /** 起点：类全限定名，或方法 uid（owner#signature） */
    private String from;

    /** 终点；kind 为 CLASS / FILE 时可为空 */
    private String to;

    /** 关系类型，如 CALLS / EXTENDS / IMPLEMENTS / DEPENDS */
    private String rel;

    /** 同一对端点之间的聚合条数 */
    private Integer count;
}
