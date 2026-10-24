package com.example.archmind.dto.response;

import com.example.archmind.dto.AgentNodeRef;
import lombok.Data;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Python {@code /internal/agent/ask}（及 {@code /navigate}）的响应 —— 网关**原样透传**的形状。
 *
 * <p>逐字段对齐 Python 侧 {@code app/models/response.py} 的 {@code AgentResponse}。
 * <b>多一个少一个都会静默出问题</b>：Spring Boot 默认
 * {@code FAIL_ON_UNKNOWN_PROPERTIES=false}，Java 这边少写一个字段，
 * Python 发过来的那个字段就**不报错地消失**（P4.2 刚加的 {@code sessionExpired}
 * 就是这种容易被漏掉的字段）。改这里务必同步改 {@code response.py}。</p>
 */
@Data
public class AgentResponse {

    /** 会话 id。没带 cid 的首次问答，由 Python 生成并在这里回带。 */
    private String conversationId;

    private String message;

    /** 一句话结论。和 {@code sections} 一起给前端做逐段渲染；降级时为 null。 */
    private String headline;

    /**
     * 结构化讲解：**唯一真相**。{@code message} 由它拼出来（降级时反过来，sections 为空）。
     *
     * <p>段落是引用的最小单位：一段话共享一组出处。顶层 {@code citations} 是各段
     * {@code citedUids} 的并集，旧前端读它不受影响。</p>
     */
    private List<Section> sections = new ArrayList<>();

    /**
     * 请求带了 cid 但 Redis 里的 Context 已过期（TTL 2h）。
     *
     * <p>给前端一个**能直接分支的布尔**，而不是让它去匹配文案。
     * 此时 {@code message} 是一句「对话已过期」，并且**不会**有正常回答 ——
     * Python 绝不会静默重开一个空会话。</p>
     */
    private Boolean sessionExpired;

    private List<Citation> citations = new ArrayList<>();

    private List<UiCommand> uiCommands = new ArrayList<>();

    /**
     * P5：本轮用于渲染的证据子图。
     *
     * <p>命令（{@code uiCommands}）只说「看哪里」，这个字段说「那里有什么、怎么连」。
     * {@code null} 表示这轮没有图数据（不是「图是空的」）。</p>
     */
    private Graph graph;

    private List<Suggestion> suggestions = new ArrayList<>();

    /** 本轮执行了哪些步骤，给用户回放、给自己审计。 */
    private List<Step> steps = new ArrayList<>();

    private Meta meta;

    // ==================== 零件 ====================

    /** 讲解里引用的出处。必须来自本轮工具结果，否则就是编造。 */
    @Data
    public static class Citation {
        private String uid;
        private String kind;
    }

    /**
     * 讲解的一段。{@code body} 是 Markdown。
     *
     * <p>{@code citedUids} **可以为空** —— 空表示这段在讲图的边界或做负向断言，
     * 图上没有对应的边可引。别把它当成漏填。</p>
     */
    @Data
    public static class Section {
        private String title;
        private String body;
        private List<String> citedUids = new ArrayList<>();
    }

    /**
     * 一条给前端的可视化命令。**LLM 只声明「要看什么」，怎么画是前端的事。**
     */
    @Data
    public static class UiCommand {
        private String op;
        private AgentNodeRef target;
        private Map<String, Object> params = new HashMap<>();
        /** 这条命令基于哪一版屏幕；前端对比自己的 rev，旧的就丢。 */
        private Integer rev;
    }

    /** 「下一步可以问什么」。前端点一个建议就是发一轮 {@code /ask}。 */
    @Data
    public static class Suggestion {
        private String label;
        private String intent;
        private Map<String, Object> slots = new HashMap<>();
    }

    /** 一轮里的一步。{@code ms} 让「慢在哪」一眼可见。 */
    @Data
    public static class Step {
        private Integer n;
        /** INTENT / TOOL / LLM —— 用 String 接，理由同 {@link Meta#intent}。 */
        private String type;
        private String detail;
        private Integer ms;
    }

    /** 这一轮的元信息。 */
    @Data
    public static class Meta {
        private String intent;
        private Double confidence;
        /** 让上层能发现「我讲的是旧图」。 */
        private Integer graphRev;
        private Integer latencyMs;
    }

    // ==================== P5：图数据 ====================

    /**
     * 本轮回答对应的证据子图。前端 upsert 进现有画布。
     *
     * <p>字段名逐一对齐 Python {@code GraphPatch}；list 必须初始化成空集合，
     * 不能留 null（Python 侧 Pydantic 对 {@code null} 不会回落默认值）。</p>
     */
    @Data
    public static class Graph {
        private List<GraphNode> nodes = new ArrayList<>();
        private List<GraphEdge> edges = new ArrayList<>();
        private List<GraphGap> gaps = new ArrayList<>();
        private boolean truncated;
    }

    /** 一个可渲染的类节点。 */
    @Data
    public static class GraphNode {
        private String uid;
        private String name;
        /** CLASS / INTERFACE / ENUM —— 用 String 接，理由同 {@link Meta#intent}。 */
        private String kind;
        private String packageName;
        private String module;
        private Integer depth;
        private boolean viaFanout;
        private Integer edgeCount;
    }

    /** 一条可渲染的边。{@code from} 在 Java 里是合法标识符。 */
    @Data
    public static class GraphEdge {
        private String from;
        private String to;
        /** CALLS / EXTENDS / IMPLEMENTS / DEPENDS */
        private String rel;
        private Integer callCount;
        private boolean viaFanout;
    }

    /** 链路断点：前端要把它画成「未解析」，不能当成不存在。 */
    @Data
    public static class GraphGap {
        private String at;
        private String reason;
    }
}
