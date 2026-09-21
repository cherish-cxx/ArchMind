package com.example.archmind.dto.response;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/**
 * 调用链查询结果。**类级折叠后的视图** —— 方法级边可能有几百条，喂给 LLM 是纯噪声。
 */
@Data
public class CallChainResponse {

    private ClassNodeDTO start;

    private List<ChainNode> nodes = new ArrayList<>();

    private List<ChainEdge> edges = new ArrayList<>();

    private List<Gap> gaps = new ArrayList<>();

    /** 单层节点数超过上限时为 true —— 必须让调用方知道，否则 LLM 会把「只看到这些」讲成「只有这些」 */
    private boolean truncated;

    @Data
    public static class ChainNode {
        private String uid;
        private String name;
        /** CLASS / INTERFACE / ENUM */
        private String kind;
        /** 所在模块（从 filePath 推导）。让 LLM 能看出调用是否跨了模块／跨了服务 */
        private String module;
        /** 距起点的跳数。起点为 0；扇出得到的实现类与它替代的接口**同深度** */
        private int depth;
        /** true 表示这个类是通过「接口 → 实现类」扇出得到的 */
        private boolean viaFanout;
        /**
         * 与该类相关的方法级边条数（自环不计）。
         * 注意它按**方法级行**累计，所以 `ItemClient` 被同一方法调两次会记 2 —— 与
         * {@link ChainEdge#callCount} 一致，不重复计算「类到类的边数」。
         */
        private int edgeCount;
    }

    @Data
    public static class ChainEdge {
        private String from;
        private String to;
        /** CALLS（普通调用）或 IMPLEMENTS（扇出时替换了接口） */
        private String rel;
        /**
         * 这条边的证据条数，随 {@code rel} 含义不同：
         * CALLS 是底层**方法级调用边**条数；IMPLEMENTS 是**配对成功的接口方法数**。
         * 后者是诊断信号 —— 接口有 3 个方法而这里只有 2，说明有一个没扇出去。
         */
        private int callCount;
        private boolean viaFanout;
    }

    /** 断点：链路**意外**终止的位置与原因 */
    @Data
    public static class Gap {
        /** 断在哪个类（或方法）上 */
        private String at;
        /** 见 {@code GapReason} */
        private String reason;
    }
}
