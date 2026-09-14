package com.example.archmind.model.ast;

/**
 * C 阶段产出的一条边。
 * source/target：CALLS 用方法唯一键（类全限定名 + "#" + 签名），其余用类全限定名。
 * count 仅 CALLS 有意义（不同调用表达式合并计数）；via 仅 DEPENDS 有意义（field/signature，取首个来源）。
 */
public record ParsedEdge(EdgeType type, String source, String target, int count, String via) {
}
