package com.example.archmind.service.domain;

import com.example.archmind.dto.response.CapabilityCardPageResponse;

/**
 * 索引专用读接口：按 uid 游标分页拉取带描述的能力卡片。
 */
public interface CapabilityCardQueryService {

    /**
     * 按能力 uid 游标分页；每页带 rev，调用方发现 rev 变化要中止重来。
     *
     * @param afterUid 上一页最后一个能力 uid；null 表示第一页
     * @param limit    本页最多返回多少个能力（服务端钳在 1~200）
     */
    CapabilityCardPageResponse listCapabilityCardsPage(Long projectId, String afterUid, int limit);
}
