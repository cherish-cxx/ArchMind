package com.example.archmind.dto.response;

import lombok.Getter;

import java.util.ArrayList;
import java.util.List;

/**
 * {@code /internal/tools/*} 的统一返回信封，5 个工具共用。
 *
 * <p>两个字段是硬要求，不是可选项：</p>
 * <ul>
 *   <li>{@code evidence} —— 让「禁止编造」可校验（见 {@link Evidence}）</li>
 *   <li>{@code truncated} —— 结果被截断时必须为 true。不说的话，LLM 会把
 *       「只看到 3 个」讲成「这个类只调用 3 个」，这是最难发现的错误</li>
 * </ul>
 *
 * <p>{@code graphRev} 是图版本号（当前未实现，P1 第 3 步补）：图变了 rev 变，
 * 用于缓存失效与 Context 一致性判断。</p>
 */
@Getter
public class ToolEnvelope<T> {

    private final boolean ok;
    private final T data;
    private final List<Evidence> evidence;
    private final boolean truncated;
    private final String note;
    private final Long graphRev;

    private ToolEnvelope(boolean ok, T data, List<Evidence> evidence,
                         boolean truncated, String note, Long graphRev) {
        this.ok = ok;
        this.data = data;
        this.evidence = evidence;
        this.truncated = truncated;
        this.note = note;
        this.graphRev = graphRev;
    }

    public static <T> ToolEnvelope<T> success(T data) {
        return new ToolEnvelope<>(true, data, new ArrayList<>(), false, null, null);
    }

    public static <T> ToolEnvelope<T> success(T data, List<Evidence> evidence, boolean truncated) {
        return new ToolEnvelope<>(true, data, evidence, truncated, null, null);
    }

    /**
     * 返回带上图版本号的副本。
     * 调用方拿它跟上一轮比对：rev 变了说明图被重建过，Context 里存的 uid 可能已失效。
     */
    public ToolEnvelope<T> withGraphRev(long rev) {
        return new ToolEnvelope<>(ok, data, evidence, truncated, note, rev);
    }

    /**
     * 工具「答不了」时的正常返回 —— 例如项目尚未分析、类不存在。
     * 这是业务结果，不是异常，因此仍走 200，由调用方按 {@code ok} 判断。
     */
    public static <T> ToolEnvelope<T> failure(String note) {
        return new ToolEnvelope<>(false, null, List.of(), false, note, null);
    }
}
