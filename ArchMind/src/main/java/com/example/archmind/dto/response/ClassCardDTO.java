package com.example.archmind.dto.response;

import lombok.Data;
import lombok.EqualsAndHashCode;

import java.util.List;

/**
 * 索引专用的类卡片：在 {@link ClassNodeDTO} 之上补方法摘要与上下游摘要。
 *
 * <p>和 {@code classes/page} 分开是刻意的：那个接口是通用分页，
 * 加字段会改变所有调用方的响应形状；卡片只有索引服务消费。</p>
 */
@Data
@EqualsAndHashCode(callSuper = true)
public class ClassCardDTO extends ClassNodeDTO {

    /** 方法总数（不只是 methods 里返回的截断部分） */
    private int methodCount;

    /** 方法摘要，最多 cap 个；按方法名排序 */
    private List<MethodBriefDTO> methods = List.of();

    private UpstreamDTO upstream = new UpstreamDTO();

    private DownstreamDTO downstream = new DownstreamDTO();

    private boolean methodsTruncated;

    private boolean upstreamTruncated;

    private boolean downstreamTruncated;
}
