package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 批量补齐类描述的单批结果。
 *
 * <p>{@code status} 是驱动侧的分支依据，不要只看 remaining —— blocked/busy 时
 * remaining 为 null，表示「不知道还剩多少」而不是「已补完」。</p>
 */
@Data
public class DescribeBatchResult {

    /** 正常执行了一批（可能一条都没写成） */
    public static final String STATUS_OK = "ok";
    /** 另一批正在处理，稍后重试 */
    public static final String STATUS_BUSY = "busy";
    /** 项目正在分析，重试即可 */
    public static final String STATUS_ANALYSIS_RUNNING = "analysis_running";
    /** 项目从未成功分析，不应补齐，直接停止 */
    public static final String STATUS_ANALYSIS_NOT_COMPLETED = "analysis_not_completed";
    /** 调用期间图被重建，本批结果已丢弃 */
    public static final String STATUS_STALE = "stale";
    /** LLM 调用失败或处于冷却 */
    public static final String STATUS_FAILED = "failed";

    private String status;

    /** 本批实际使用的批量大小 */
    private Integer limit;

    private List<ClassDescriptionsResponse.Item> items = List.of();

    /** 选中但没写成的类（模型漏答） */
    private List<String> failed = List.of();

    /** 本次写回后仍待描述的类数；blocked/busy 时为 null */
    private Integer remaining;

    private Integer total;

    private Integer describable;

    private String note;

    /** 建议重试间隔（毫秒），仅 busy/failed 有值 */
    private Long retryAfterMs;
}
