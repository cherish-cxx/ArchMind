package com.example.archmind.common.constant;

/**
 * 跨模块共用的 Redis key 前缀。
 *
 * <p>集中放这里是为了让「分析锁」「描述锁」在 TaskService 与 ClassDescriptionService
 * 之间有唯一来源，同时不引入两者之间的 Bean 依赖（否则会成环）。</p>
 */
public final class RedisKeys {

    /** 项目级分析锁：整轮分析期间持有 */
    public static final String ANALYZE_LOCK = "project:analyze:lock:";

    /** 单批描述锁：画布按需与批量补齐共用，按批持有、按批释放 */
    public static final String DESCRIBE_LOCK = "project:describe:lock:";

    /** 描述服务失败冷却：存在期间不再调用 LLM */
    public static final String DESCRIBE_COOLDOWN = "project:describe:cooldown:";

    /** 任务实时进度（高频，只写 Redis） */
    public static final String TASK_PROGRESS = "task:progress:";

    private RedisKeys() {
    }
}
