package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

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
    void layeredProjectWithSubPackageKeysFallsBackToSingleArea() {
        // controller 段后没有业务段，但 service.agent / dto.response 会产生伪 key；
        // 入口层全落基础设施时必须退回单领域，否则能力数为 0。
        var roles = Map.of(
                "com.example.archmind.controller.C",
                new Classified("com.example.archmind.controller.C", Role.ENTRY, "annotation"),
                "com.example.archmind.service.agent.A",
                new Classified("com.example.archmind.service.agent.A", Role.BUSINESS, "package"),
                "com.example.archmind.dto.response.R",
                new Classified("com.example.archmind.dto.response.R", Role.DOMAIN_MODEL, "package"));
        var classes = List.of(
                cls("com.example.archmind.controller.C", "com.example.archmind.controller"),
                cls("com.example.archmind.service.agent.A", "com.example.archmind.service.agent"),
                cls("com.example.archmind.dto.response.R", "com.example.archmind.dto.response"));
        var r = new BusinessAreaResolver().resolve(classes, roles, "ArchMind");
        assertEquals("single", r.strategy());
        assertEquals("main", r.classToArea().get("com.example.archmind.controller.C"));
        assertEquals(List.of("ArchMind"), r.areas().stream()
                .filter(a -> a.kind().equals("business"))
                .map(BusinessArea::name).toList());
    }

    @Test
    void modularProjectKeepsEntryInBusinessArea() {
        var roles = Map.of(
                "com.hmall.item.controller.ItemController",
                new Classified("com.hmall.item.controller.ItemController", Role.ENTRY, "annotation"),
                "com.hmall.user.controller.UserController",
                new Classified("com.hmall.user.controller.UserController", Role.ENTRY, "annotation"));
        var classes = List.of(
                cls("com.hmall.item.controller.ItemController", "com.hmall.item.controller"),
                cls("com.hmall.item.service.ItemService", "com.hmall.item.service"),
                cls("com.hmall.user.controller.UserController", "com.hmall.user.controller"));
        var r = new BusinessAreaResolver().resolve(classes, roles, "hmall");
        assertEquals("business-modules", r.strategy());
        assertEquals("item", r.classToArea().get("com.hmall.item.controller.ItemController"));
        assertEquals("user", r.classToArea().get("com.hmall.user.controller.UserController"));
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

    @Test
    void subPackageStaysWithItsBusinessArea() {
        var classes = List.of(
                cls("com.hmall.item.ItemService", "com.hmall.item"),
                cls("com.hmall.item.dto.ItemDTO", "com.hmall.item.dto"),
                cls("com.hmall.trade.TradeService", "com.hmall.trade"));
        var r = new BusinessAreaResolver().resolve(classes, Map.of(), "hmall");
        assertEquals("business-modules", r.strategy());
        assertEquals("item", r.classToArea().get("com.hmall.item.dto.ItemDTO"));
        assertEquals(2, r.areas().stream().filter(a -> a.kind().equals("business")).count());
    }

    @Test
    void implSegmentIsTechnical() {
        var classes = List.of(
                cls("com.example.demo.service.impl.OrderServiceImpl", "com.example.demo.service.impl"),
                cls("com.example.demo.controller.OrderController", "com.example.demo.controller"));
        var r = new BusinessAreaResolver().resolve(classes, Map.of(), "demo");
        assertEquals("single", r.strategy());
        assertEquals(1, r.areas().stream().filter(a -> a.kind().equals("business")).count());
    }

    @Test
    void neverCreatesBusinessAreaWithoutClasses() {
        var classes = List.of(
                cls("com.shop.order.OrderController", "com.shop.order.controller"),
                cls("com.shop.billing.PayHelper", "com.shop.billing"));
        var roles = Map.of(
                "com.shop.billing.PayHelper", new Classified("com.shop.billing.PayHelper", Role.OTHER, "none"));
        var r = new BusinessAreaResolver().resolve(classes, roles, "shop");
        assertFalse(r.areas().stream().anyMatch(a -> a.uid().equals("billing")),
                "只有 OTHER 角色的包不应生成空领域");
        assertEquals(classes.size(), r.classToArea().size());
    }

    private static ClassSnapshot cls(String uid, String packageName) {
        String simpleName = uid.substring(uid.lastIndexOf('.') + 1);
        return new ClassSnapshot(uid, simpleName, packageName, List.of(), List.of(), "");
    }
}
