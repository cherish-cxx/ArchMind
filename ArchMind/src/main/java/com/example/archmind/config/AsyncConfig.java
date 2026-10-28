package com.example.archmind.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/** 异步线程池配置
 * @EnableAsync利用反射机制，开前Spring异步注解能力，配合Async使用
 * 参数说明：
 * corePoolSize：常驻核心线程
 * maxPoolSize：最大线程数量
 * queueCapacity：等待队列，任务来了核心线程满了就进入队列
 * CallerRunsPolicy：拒绝策略，不抛异常，由调用者线程自己执行任务
 */
@Configuration
@EnableAsync
public class AsyncConfig {

    @Bean("analysisExecutor")
//   把当前线程注入到Spring容器中，bean对象的名称就是analysisExecutor
    public ThreadPoolTaskExecutor analysisExecutor(){
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
            executor.setCorePoolSize(4);
            executor.setMaxPoolSize(8);
            executor.setQueueCapacity(100);
            executor.setThreadNamePrefix("analysis-");
            executor.setRejectedExecutionHandler(
                    new ThreadPoolExecutor.CallerRunsPolicy());
//            手动创建线程池
            executor.initialize();
            return executor;
    }

    /**
     * 描述补齐专用池：LLM 调用是长 IO，且一个项目一次只跑一批，
     * 和 AST 解析分池，避免补齐把分析线程占满。
     */
    @Bean("descriptionExecutor")
    public ThreadPoolTaskExecutor descriptionExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(1);
        executor.setMaxPoolSize(2);
        executor.setQueueCapacity(20);
        executor.setThreadNamePrefix("describe-");
        // 队列满时直接拒绝，由投递方记录；不能用 CallerRuns 把补齐跑进分析线程
        executor.setRejectedExecutionHandler(
                new ThreadPoolExecutor.AbortPolicy());
        executor.initialize();
        return executor;
    }

}
