package com.example.archmind.service.ast;

/**
 * 关联查询方向。UP = 谁指向我，DOWN = 我指向谁。
 *
 * <p>由调用方指定而非全查，是因为原实现固定跑 6 条 Cypher（上游 3 + 下游 3），
 * 只要一个方向时会浪费 5 次查询。</p>
 */
public enum RelationDirection {
    UP,
    DOWN,
    BOTH
}
