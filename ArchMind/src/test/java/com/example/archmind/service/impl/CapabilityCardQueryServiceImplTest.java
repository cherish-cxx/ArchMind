package com.example.archmind.service.impl;

import com.example.archmind.dto.response.CapabilityCardPageResponse;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class CapabilityCardQueryServiceImplTest {

    @Test
    void assembleSortsMembersByDepthAndFlagsTruncation() {
        var caps = List.of(row("POST /orders", 10));
        var members = Map.of("POST /orders", List.of(
                member("a.OrderController", "OrderController", "ENTRY", 0),
                member("a.OrderRepo", "OrderRepo", "INFRASTRUCTURE", 2),
                member("a.OrderService", "OrderService", "BUSINESS", 1)));

        CapabilityCardPageResponse page =
                CapabilityCardQueryServiceImpl.assemble(caps, members, 7L, "next-uid");

        assertEquals(7L, page.getRev());
        assertEquals("next-uid", page.getNextCursor());
        var card = page.getItems().get(0);
        assertEquals(List.of("a.OrderController", "a.OrderService", "a.OrderRepo"),
                card.getMembers().stream().map(m -> m.getUid()).toList());
        assertTrue(card.isMembersTruncated());
        assertEquals(10, card.getMemberCount());
    }

    @Test
    void assembleKeepsOpaqueCapabilityUid() {
        var caps = List.of(row("SCHEDULED a.Job#run()", 1));
        var page = CapabilityCardQueryServiceImpl.assemble(
                caps, Map.of(), 3L, null);
        assertEquals("SCHEDULED a.Job#run()", page.getItems().get(0).getUid());
    }

    private static Map<String, Object> row(String uid, int memberCount) {
        return Map.of("uid", uid, "name", uid, "description", "d",
                "areaUid", "order", "areaName", "订单", "areaDescription", "订单域",
                "entryUid", "a.OrderController#create()", "entryKind", "HTTP",
                "memberCount", memberCount);
    }

    private static Map<String, Object> member(String uid, String name, String role, int depth) {
        return Map.of("uid", uid, "name", name, "role", role, "depth", depth);
    }
}
