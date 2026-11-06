package com.example.archmind.config;

import lombok.extern.slf4j.Slf4j;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 流式问答的线程池。
 *
 * <p><b>为什么必须单独一个</b>：{@code SseEmitter} 的请求线程要立刻还给容器，
 * 上游读取只能放到别的线程。共用 Tomcat 线程池会在并发多轮时把请求线程全部占住 ——
 * 表现为「站点整体卡死」，而不只是问答变慢。</p>
 *
 * <p>上限刻意很小：一轮问答最长可以占几分钟，队列排太深只会让用户等一个
 * 早就该失败的请求。排不下时直接拒绝，由网关发一帧 {@code error}。</p>
 */
@Configuration
@Slf4j
public class AgentStreamConfig {

    @Bean(name = "agentStreamExecutor", destroyMethod = "shutdown")
    public ExecutorService agentStreamExecutor() {
        AtomicInteger seq = new AtomicInteger();
        return new ThreadPoolExecutor(
                4, 8, 60, TimeUnit.SECONDS,
                new LinkedBlockingQueue<>(32),
                r -> {
                    Thread t = new Thread(r, "agent-stream-" + seq.incrementAndGet());
                    t.setDaemon(true);
                    return t;
                },
                new ThreadPoolExecutor.AbortPolicy());
    }

    /**
     * SSE 心跳。**不是为了保活，是为了发现「人已经走了」**。
     *
     * <p>Tomcat 只有在写响应时才会发现客户端断开。如果模型正安静地想 20 秒，
     * 这期间一个字节都不写，Java 就不知道对面已经关了 —— 上游会继续跑完、
     * 继续烧 token，最后还把这一轮落库。每 2s 写一个注释帧，断开就能及时暴露。</p>
     */
    @Bean(name = "agentStreamHeartbeat", destroyMethod = "shutdown")
    public ScheduledExecutorService agentStreamHeartbeat() {
        AtomicInteger seq = new AtomicInteger();
        return Executors.newScheduledThreadPool(1, r -> {
            Thread t = new Thread(r, "agent-heartbeat-" + seq.incrementAndGet());
            t.setDaemon(true);
            return t;
        });
    }
}
