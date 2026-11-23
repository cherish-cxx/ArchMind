package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.Member;
import com.example.archmind.service.domain.GraphSnapshot.CallEdgeSnapshot;
import com.example.archmind.service.domain.GraphSnapshot.EndpointSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CapabilityResolverTest {

    @Test
    void stopsAtAnotherBusinessAreaAndRecordsCrossAreaCall() {
        var calls = List.of(
                edge("demo.OrderController#create()", "demo.OrderService#create()"),
                edge("demo.OrderService#create()", "demo.ItemService#deduct()"),
                edge("demo.OrderService#create()", "demo.PayClient#pay()"));
        var areas = Map.of("demo.OrderController", "order", "demo.OrderService", "order",
                "demo.ItemService", "item");
        var roles = Map.of("demo.PayClient", new Classified("demo.PayClient", Role.EXTERNAL, "name"));
        var eps = List.of(new EndpointSnapshot("POST /orders", "http", "demo.OrderController#create()"));

        var r = new CapabilityResolver().resolve(eps, areas, roles, calls);
        Capability cap = r.capabilities().get(0);
        assertEquals(List.of("demo.OrderController", "demo.OrderService"),
                cap.members().stream().map(Member::classUid).toList());
        assertEquals(1, r.areaCalls().size());
        assertEquals("item", r.areaCalls().get(0).toAreaUid());
        assertEquals(1, r.gaps().size());
    }

    @Test
    void survivesCycles() {
        var calls = List.of(edge("a.A#x()", "a.B#y()"), edge("a.B#y()", "a.A#x()"));
        var r = new CapabilityResolver().resolve(
                List.of(new EndpointSnapshot("GET /a", "http", "a.A#x()")),
                Map.of("a.A", "x", "a.B", "x"), Map.of(), calls);
        assertEquals(1, r.capabilities().size());
    }

    @Test
    void infrastructureMembersAreIncludedButDoNotStopTraversal() {
        var calls = List.of(
                edge("o.C#create()", "o.S#create()"),
                edge("o.S#create()", "o.M#insert()"),
                edge("o.M#insert()", "o.X#more()"));
        var areas = Map.of("o.C", "order", "o.S", "order", "o.M", "infrastructure", "o.X", "order");
        var r = new CapabilityResolver().resolve(
                List.of(new EndpointSnapshot("POST /o", "http", "o.C#create()")),
                areas, Map.of(), calls);
        var members = r.capabilities().get(0).members().stream().map(Member::classUid).toList();
        assertTrue(members.contains("o.M"));
        assertTrue(members.contains("o.X"));
        assertTrue(r.areaCalls().isEmpty());
    }

    @Test
    void infrastructureEntryProducesNoCapability() {
        var r = new CapabilityResolver().resolve(
                List.of(new EndpointSnapshot("SCHEDULED j.J#run()", "scheduled", "j.J#run()")),
                Map.of("j.J", "infrastructure"), Map.of(), List.of());
        assertTrue(r.capabilities().isEmpty());
    }

    @Test
    void memberDepthIsShortestHop() {
        var calls = List.of(
                edge("a.A#x()", "a.B#y()"),
                edge("a.A#x()", "a.C#z()"),
                edge("a.C#z()", "a.B#y()"));
        var r = new CapabilityResolver().resolve(
                List.of(new EndpointSnapshot("GET /a", "http", "a.A#x()")),
                Map.of("a.A", "x", "a.B", "x", "a.C", "x"), Map.of(), calls);
        var b = r.capabilities().get(0).members().stream()
                .filter(m -> m.classUid().equals("a.B")).findFirst().orElseThrow();
        assertEquals(1, b.depth());
    }

    private static CallEdgeSnapshot edge(String from, String to) {
        return new CallEdgeSnapshot(from, to);
    }
}
