package com.example.archmind.service.domain;

import java.util.List;

/**
 * 领域建模所需的只读图快照：类、调用边、入口。
 * 一次读出，后续 role / 领域 / 能力计算全部在内存里做，不再回库。
 */
public record GraphSnapshot(List<ClassSnapshot> classes,
                            List<CallEdgeSnapshot> calls,
                            List<EndpointSnapshot> endpoints) {

    /** 类节点快照；annotationNames / interfaces 缺失或类型异常时为默认值，见 GraphSnapshotDao。 */
    public record ClassSnapshot(String uid, String name, String packageName,
                                List<String> annotationNames, List<String> interfaces,
                                String superclass) {
    }

    /** 方法级调用边；uid 形如 owner#signature。 */
    public record CallEdgeSnapshot(String fromMethodUid, String toMethodUid) {
    }

    /** 入口快照；kind 为 http / scheduled / mq / event / manual。 */
    public record EndpointSnapshot(String uid, String kind, String handlerUid) {
    }
}
