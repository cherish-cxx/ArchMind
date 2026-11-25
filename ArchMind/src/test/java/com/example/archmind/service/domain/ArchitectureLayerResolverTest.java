package com.example.archmind.service.domain;

import com.example.archmind.dto.response.ArchitectureGroupDto;
import com.example.archmind.dto.response.ArchitectureLayerDto;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ArchitectureLayerResolverTest {

    private final ArchitectureLayerResolver resolver = new ArchitectureLayerResolver();

    @Test
    void mapsRolesToSixLayersInFixedOrderAndKeepsUnknown() {
        var rows = List.of(
                cls("a.C", "C", "ENTRY", "a.web", null, null),
                cls("a.S", "S", "business", "a.app", "order", "订单"),
                cls("a.E", "E", null, "a.x", null, null));

        var resp = resolver.resolve(rows, List.of(), 12, 5);

        assertEquals(List.of("entry", "business", "domain-model",
                "infrastructure", "external", "other"),
                resp.layers().stream().map(ArchitectureLayerDto::key).toList());
        assertEquals(1, resp.layers().get(5).classCount());
    }

    @Test
    void groupsByAreaForBusinessLayersAndByLastPackageForInfrastructure() {
        var rows = List.of(
                cls("a.S", "S", "BUSINESS", "a.app", "order", "订单"),
                cls("b.S", "S2", "BUSINESS", "b.app", "item", "商品"),
                cls("a.R", "R", "INFRASTRUCTURE", "a.infra.mapper", null, null),
                cls("a.U", "U", "INFRASTRUCTURE", "a.infra.mapper", null, null));

        var resp = resolver.resolve(rows, List.of(), 12, 5);

        var business = resp.layers().stream()
                .filter(l -> l.key().equals("business")).findFirst().orElseThrow();
        var infrastructure = resp.layers().stream()
                .filter(l -> l.key().equals("infrastructure")).findFirst().orElseThrow();
        assertEquals(List.of("order", "item"),
                business.groups().stream().map(ArchitectureGroupDto::key).toList());
        assertEquals("mapper", infrastructure.groups().get(0).key());
    }

    @Test
    void aggregatesOnlyCrossLayerCallsAndKeepsTopSamples() {
        var rows = List.of(
                cls("a.C", "C", "ENTRY", "a.web", null, null),
                cls("a.R", "R", "INFRASTRUCTURE", "a.mapper", null, null),
                cls("a.U", "U", "INFRASTRUCTURE", "a.mapper", null, null));
        var calls = List.of(
                new ArchitectureLayerResolver.PairCount("a.C", "a.R", 12),
                new ArchitectureLayerResolver.PairCount("a.C", "a.U", 3),
                new ArchitectureLayerResolver.PairCount("a.R", "a.U", 9));

        var resp = resolver.resolve(rows, calls, 12, 1);

        assertEquals(1, resp.edges().size());
        var edge = resp.edges().get(0);
        assertEquals("entry", edge.fromLayer());
        assertEquals("infrastructure", edge.toLayer());
        assertEquals(15, edge.count());
        assertEquals("a.C", edge.samples().get(0).fromUid());
        assertEquals(12, edge.samples().get(0).count());
    }

    private static ArchitectureLayerResolver.ClassRow cls(
            String uid, String name, String role, String pkg, String areaUid, String areaName) {
        return new ArchitectureLayerResolver.ClassRow(uid, name, role, pkg, areaUid, areaName);
    }
}
