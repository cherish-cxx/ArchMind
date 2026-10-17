package com.example.archmind.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * 调用 Python agent-service 的连接配置（{@code /api/agent/*} 网关用）。
 *
 * <p>对应 {@code application.yml} 的 {@code agent} 段。没有 {@code @EnableConfigurationProperties}，
 * 靠 {@code @Component} 被扫描生效 —— 与 {@link UploadProperties} 一致。</p>
 */
@Data
@ConfigurationProperties(prefix = "agent")
@Component
public class AgentProperties {

    /** agent-service 的地址。容器化后必须用环境变量覆盖 —— 容器里的 localhost 指向容器自己 */
    private String baseUrl = "http://localhost:8000";

    /**
     * 转发 {@code /ask} 等长耗时接口的超时（毫秒）。
     *
     * <p><b>70s 不是随手取的，它是级联超时链里的一环</b>，必须严格递减：</p>
     * <pre>
     *   nginx(90s) &gt; Java 这个值(70s) &gt; Python 一轮最坏(58s) &gt; 单次 LLM(25s) &gt; 工具(8s)
     * </pre>
     *
     * <p>Python 一轮最坏 = Intent LLM 25s（超时后降级为规则）+ 工具 8s + 讲解 LLM 25s = 58s。
     * 设计文档 §3.2 原文写「Java 代理 Python 45–60s」，<b>下界 45s 是错的</b> ——
     * 那会让网关在 Python 还没答完时就断开，用户看到的是网关超时而不是一个慢答案。</p>
     */
    private int timeoutMs = 70_000;

    /**
     * 清理类接口（evict）的超时（毫秒）。
     *
     * <p>单独给一个短值：删项目时顺带清理 Agent 会话，<b>不能因为 Python 挂了就卡 70 秒</b>。
     * 这类调用失败只是留下一点 Redis 里的孤儿数据，不影响删除结果。</p>
     */
    private int evictTimeoutMs = 3_000;
}
