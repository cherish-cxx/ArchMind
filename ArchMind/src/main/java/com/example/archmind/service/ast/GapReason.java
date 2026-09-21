package com.example.archmind.service.ast;

/**
 * 链路断点的原因。
 *
 * <p>必须分类而不是笼统说「没找到」—— 三种断法对用户是完全不同的信息，
 * 混成一句等于什么都没说（也是「LLM 用常识补全断链」的防线）。</p>
 */
public enum GapReason {

    /**
     * 目标是 API 契约模块里的接口，项目内没有实现类 —— **调用跨到了另一个服务**。
     * 对微服务项目这是最有价值的信息：它讲的是服务间调用关系。
     */
    REMOTE_SERVICE,

    /** 目标接口在项目内没有任何实现类（典型：MyBatis Mapper，实现是运行时代理） */
    INTERFACE_NO_IMPLEMENTATION,

    /**
     * 保留值，**V1 不产出**：普通类方法没有下游，说明它是个叶子节点，
     * 从 edges 里本来就能看出来，报成「断链」只会制造噪声。
     */
    NO_DOWNSTREAM_EDGE,

    /**
     * 保留值，**V1 不产出**：调项目外的 jar 时 {@code usable()} 解析失败、边压根不会建，
     * 图上无从检出，因此无法作为 gap 上报。
     */
    EXTERNAL_DEPENDENCY
}
