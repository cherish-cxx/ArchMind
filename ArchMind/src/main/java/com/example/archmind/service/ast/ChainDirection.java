package com.example.archmind.service.ast;

/**
 * 调用链方向。
 *
 * <p>与 {@link RelationDirection} 分开是因为语义不同：call-chain 的 DOWN 是**多层 BFS**，
 * UP 是**单层反查**，没有 BOTH 这种组合。</p>
 */
public enum ChainDirection {
    /** 谁调用了谁 —— 多层往下走，业务流程的主场景 */
    DOWN,
    /** 谁调用了它 —— 只做 1 层，再往上会迅速发散到全项目 */
    UP
}
