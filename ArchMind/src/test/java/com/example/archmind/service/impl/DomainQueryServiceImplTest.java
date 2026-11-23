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
                        "classCount", 4L,
                        "capUid", "GET /items", "capName", "GET /items", "capDescription", "按分类查商品。",
                        "entryUid", "a.C#l()", "memberCount", 2L),
                Map.of("areaUid", "item", "areaName", "item", "kind", "business", "description", "商品管理",
                        "classCount", 4L,
                        "capUid", "POST /items", "capName", "POST /items", "capDescription", "上架新商品。",
                        "entryUid", "a.C#c()", "memberCount", 3L));
        var resp = DomainQueryServiceImpl.assembleMap(rows);
        assertEquals(1, resp.areas().size());
        assertEquals(4, resp.areas().get(0).classCount());
        assertEquals(2, resp.areas().get(0).capabilities().size());
        assertEquals("GET /items", resp.areas().get(0).capabilities().get(0).uid());
        assertEquals("按分类查商品。", resp.areas().get(0).capabilities().get(0).description());
    }

    @Test
    void assembleMapKeepsAreaWithoutCapability() {
        List<Map<String, Object>> rows = List.of(
                Map.of("areaUid", "infrastructure", "areaName", "infrastructure",
                        "kind", "infrastructure", "description", "", "classCount", 7L));
        var resp = DomainQueryServiceImpl.assembleMap(rows);
        assertEquals(1, resp.areas().size());
        assertEquals(7, resp.areas().get(0).classCount());
        assertTrue(resp.areas().get(0).capabilities().isEmpty());
    }

    @Test
    void assembleDetailSortsMembersByDepth() {
        Map<String, Object> cap = Map.of("uid", "POST /orders", "name", "POST /orders",
                "description", "创建订单。", "entryUid", "o.C#c()");
        List<Map<String, Object>> members = List.of(
                Map.of("uid", "o.S", "name", "S", "depth", 1L, "role", "business"),
                Map.of("uid", "o.C", "name", "C", "depth", 0L, "role", "entry"));
        List<Map<String, Object>> edges = List.of(
                Map.of("fromUid", "o.S", "toUid", "o.C"),
                Map.of("fromUid", "o.C", "toUid", "o.S"));
        List<Map<String, Object>> areaCalls = List.of(
                Map.of("toAreaUid", "item", "toAreaName", "商品", "count", 3L,
                        "fromClassUids", List.of("o.S")));
        var resp = DomainQueryServiceImpl.assembleDetail(cap, members, edges, areaCalls);
        assertEquals("创建订单。", resp.capability().description());
        assertEquals(List.of("o.C", "o.S"), resp.members().stream().map(MemberDto::uid).toList());
        assertEquals("o.C->o.S", resp.edges().get(0).fromUid() + "->" + resp.edges().get(0).toUid());
        assertEquals(List.of("o.S"), resp.areaCalls().get(0).fromClassUids());
    }
}
