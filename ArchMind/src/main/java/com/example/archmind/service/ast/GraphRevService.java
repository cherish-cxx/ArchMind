package com.example.archmind.service.ast;

/**
 * 图版本号读取。**写入在 {@code CodeGraphNeo4jWriter} 的写图事务里**，由它负责递增 ——
 * rev 的职责是回答「图变了没」，必须和图的变更原子绑定，所以不在这里写。
 */
public interface GraphRevService {

    /**
     * 当前图版本。
     *
     * <p>返回 0 表示「还没有过带版本号的写图」—— 包括两种情况：项目从未落图，
     * 或图是本次改动之前建的（存量图没有 {@code GraphMeta} 节点）。
     * 重建一次之后就会变成 1、2、3……</p>
     *
     * <p>取值失败（如 Neo4j 不可用）也返回 0 而不抛异常 —— 否则会把本来不依赖图的
     * {@code overview} 工具一起拖挂。</p>
     */
    long currentRev(Long projectId);
}
