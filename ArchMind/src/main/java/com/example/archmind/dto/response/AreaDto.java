package com.example.archmind.dto.response;

import java.util.List;

/**
 * 领域地图中的一个业务领域。
 */
public record AreaDto(String uid, String name, String kind, String description,
                      List<CapabilityDto> capabilities) {
}
