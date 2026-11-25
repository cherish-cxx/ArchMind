package com.example.archmind.service.impl;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * ArchitectureQueryServiceImpl 纯函数单测：Cypher 行 → resolver 输入的映射，不连库。
 * Cypher 本身是否命中正确数据，需另靠真库冒烟。
 */
class ArchitectureQueryServiceImplTest {

    @Test
    void assembleMapsRowsAndCallsIntoResolver() {
        var classes = List.of(
                Map.of("uid", "a.C", "name", "C", "role", "ENTRY", "packageName", "a.web"),
                Map.of("uid", "a.R", "name", "R", "role", "INFRASTRUCTURE", "packageName", "a.mapper"));
        var calls = List.of(Map.of("fromUid", "a.C", "toUid", "a.R", "count", 4));

        var resp = ArchitectureQueryServiceImpl.assemble(classes, calls);

        assertEquals(2, resp.classCount());
        assertEquals(1, resp.edges().size());
        assertEquals(4, resp.edges().get(0).count());
    }
}
