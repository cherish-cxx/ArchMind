package com.example.archmind.service.domain;

import com.example.archmind.dto.response.CapabilityDetailResponse;
import com.example.archmind.dto.response.DomainMapResponse;

/**
 * 领域与能力查询：读 DOMAIN 阶段落图的 BusinessArea / Capability。
 */
public interface DomainQueryService {

    /** 项目领域地图：全部业务领域，能力按 uid 排序；无能力的领域保留为空列表。 */
    DomainMapResponse domainMap(Long projectId);

    /** 单个能力详情：能力摘要 + 成员 + 跨领域调用。 */
    CapabilityDetailResponse capability(Long projectId, String uid);
}
