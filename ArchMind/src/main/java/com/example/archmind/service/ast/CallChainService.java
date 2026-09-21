package com.example.archmind.service.ast;

import com.example.archmind.dto.response.CallChainResponse;

/**
 * 调用链查询：谁调用了谁（DOWN）／谁调用了它（UP）。
 *
 * <p>刻意**不用变长 Cypher**（{@code CALLS*1..N}）：走到接口断点时要换成 {@code IMPLEMENTS}
 * 继续往下，变长模式表达不了；且变长路径在稠密图上是引擎内部枚举所有路径，外层 LIMIT
 * 拦不住。逐层 BFS 每层一条 1-hop，每层都能限流。</p>
 */
public interface CallChainService {

    /**
     * @param uid       起点类全限定名
     * @param methodUid 可选，方法级起点（{@code owner#signature}）。为 null 时从整个类出发
     * @param direction DOWN 多层 / UP 单层
     * @param maxDepth  DOWN 的最大层数，服务端会再 clamp
     * @param maxNodes  单层节点上限，超出即截断
     */
    CallChainResponse callChain(Long projectId, String uid, String methodUid,
                                ChainDirection direction, int maxDepth, int maxNodes);
}
