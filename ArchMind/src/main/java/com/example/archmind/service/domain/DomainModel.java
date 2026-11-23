package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.AreaCall;
import com.example.archmind.service.domain.RoleClassifier.Classified;

import java.util.List;
import java.util.Map;

/**
 * 领域模型：Task 5-7 的产物汇总，交给 {@link DomainModelValidator} 对账、
 * 交给 {@link DomainModelWriter} 落库。
 */
public record DomainModel(List<BusinessArea> areas, List<Capability> capabilities,
                          List<AreaCall> areaCalls, Map<String, String> classToArea,
                          Map<String, Classified> roles) {
}
