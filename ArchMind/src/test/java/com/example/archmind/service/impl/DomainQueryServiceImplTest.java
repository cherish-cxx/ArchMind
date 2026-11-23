package com.example.archmind.service.impl;

import com.example.archmind.dto.response.MemberDto;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * DomainQueryServiceImpl 纯函数单测：行 → DTO 的分组与排序，不连库。
 * Cypher 本身是否命中正确数据，需另靠真库冒烟。
 */
class DomainQueryServiceImplTest {

    @Test
    void assembleMapGroupsCapabilitiesUnderArea() {
        List<Map<String, Object>> rows = List.of(
                Map.of("areaUid", "item", "areaName", "item", "kind", "business", "description", "商品管理",
                        "capUid", "GET /items", "capName", "GET /items", "entryUid", "a.C#l()", "memberCount", 2L),
                Map.of("areaUid", "item", "areaName", "item", "kind", "business", "description", "商品管理",
                        "capUid", "POST /items", "capName", "POST /items", "entryUid", "a.C#c()", "memberCount", 3L));
        var resp = DomainQueryServiceImpl.assembleMap(rows);
        assertEquals(1, resp.areas().size());
        assertEquals(2, resp.areas().get(0).capabilities().size());
        assertEquals("GET /items", resp.areas().get(0).capabilities().get(0).uid());
    }

    @Test
    void assembleMapKeepsAreaWithoutCapability() {
        List<Map<String, Object>> rows = List.of(
                Map.of("areaUid", "infrastructure", "areaName", "infrastructure",
                        "kind", "infrastructure", "description", ""));
        var resp = DomainQueryServiceImpl.assembleMap(rows);
        assertEquals(1, resp.areas().size());
        assertTrue(resp.areas().get(0).capabilities().isEmpty());
    }

    @Test
    void assembleDetailSortsMembersByDepth() {
        Map<String, Object> cap = Map.of("uid", "POST /orders", "name", "POST /orders", "entryUid", "o.C#c()");
        List<Map<String, Object>> members = List.of(
                Map.of("uid", "o.S", "name", "S", "depth", 1L, "role", "business"),
                Map.of("uid", "o.C", "name", "C", "depth", 0L, "role", "entry"));
        List<Map<String, Object>> areaCalls = List.of();
        var resp = DomainQueryServiceImpl.assembleDetail(cap, members, areaCalls);
        assertEquals(List.of("o.C", "o.S"), resp.members().stream().map(MemberDto::uid).toList());
    }
}
