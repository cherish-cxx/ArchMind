package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class BusinessAreaResolverTest {

    @Test
    void businessPackagesBecomeAreas() {
        var classes = List.of(
                cls("com.hmall.item.ItemService", "com.hmall.item"),
                cls("com.hmall.item.ItemController", "com.hmall.item"),
                cls("com.hmall.trade.TradeService", "com.hmall.trade"));
        var r = new BusinessAreaResolver().resolve(classes, Map.of(), "hmall");
        assertEquals("business-modules", r.strategy());
        assertEquals(List.of("item", "trade"), r.areas().stream()
                .filter(a -> a.kind().equals("business"))
                .map(BusinessArea::uid).sorted().toList());
        assertEquals("trade", r.classToArea().get("com.hmall.trade.TradeService"));
    }

    @Test
    void technicalLayeredProjectFallsBackToSingleArea() {
        var classes = List.of(
                cls("com.example.archmind.controller.C", "com.example.archmind.controller"),
                cls("com.example.archmind.service.S", "com.example.archmind.service"));
        var r = new BusinessAreaResolver().resolve(classes, Map.of(), "ArchMind");
        assertEquals("single", r.strategy());
        assertEquals(List.of("ArchMind"), r.areas().stream()
                .filter(a -> a.kind().equals("business"))
                .map(BusinessArea::name).toList());
    }

    @Test
    void skipsTechnicalSegmentToFindBusinessKey() {
        var classes = List.of(
                cls("com.acme.backend.controller.OrderController", "com.acme.backend.controller"),
                cls("com.acme.backend.cart.CartService", "com.acme.backend.cart"));
        var r = new BusinessAreaResolver().resolve(classes, Map.of(), "acme");
        assertEquals("technical-layered", r.strategy());
        assertEquals(List.of("cart"), r.areas().stream()
                .filter(a -> a.kind().equals("business")).map(BusinessArea::uid).toList());
        assertEquals("infrastructure",
                r.classToArea().get("com.acme.backend.controller.OrderController"));
    }

    @Test
    void infrastructureHoldsInfraRolesAndUnassigned() {
        var classes = List.of(
                cls("com.hmall.item.ItemService", "com.hmall.item"),
                cls("com.hmall.common.RedisUtil", "com.hmall.common"));
        var roles = Map.of("com.hmall.common.RedisUtil",
                new Classified("com.hmall.common.RedisUtil", Role.INFRASTRUCTURE, "package"));
        var r = new BusinessAreaResolver().resolve(classes, roles, "hmall");
        assertEquals("infrastructure", r.classToArea().get("com.hmall.common.RedisUtil"));
        assertEquals(classes.size(), r.classToArea().size());
    }

    private static ClassSnapshot cls(String uid, String packageName) {
        String simpleName = uid.substring(uid.lastIndexOf('.') + 1);
        return new ClassSnapshot(uid, simpleName, packageName, List.of(), List.of(), "");
    }
}
