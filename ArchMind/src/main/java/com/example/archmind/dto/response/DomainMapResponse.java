package com.example.archmind.dto.response;

import java.util.List;

/**
 * 领域地图：全部业务领域及其下的能力。基础设施领域没有能力，会以空列表出现。
 */
public record DomainMapResponse(List<AreaDto> areas) {
}
