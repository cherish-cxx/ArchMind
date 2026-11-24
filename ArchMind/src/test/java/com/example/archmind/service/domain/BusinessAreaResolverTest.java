package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;

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
    void layeredProjectNeverTreatsInnerSegmentsAsAreas() {
        // service.agent / dto.response 是层内分组，不是业务领域；入口层没有业务 key 时
        // 整个项目退化为单领域，否则入口全落基础设施、能力数为 0。
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
    void layeredProjectKeepsEveryClassOutOfBusinessAreas() {
        var roles = Map.of(
                "com.example.archmind.controller.C",
                new Classified("com.example.archmind.controller.C", Role.ENTRY, "annotation"),
                "com.example.archmind.service.impl.S",
                new Classified("com.example.archmind.service.impl.S", Role.BUSINESS, "package"),
                "com.example.archmind.dao.D",
                new Classified("com.example.archmind.dao.D", Role.INFRASTRUCTURE, "package"));
        var classes = List.of(
                cls("com.example.archmind.controller.C", "com.example.archmind.controller"),
                cls("com.example.archmind.service.impl.S", "com.example.archmind.service.impl"),
                cls("com.example.archmind.dao.D", "com.example.archmind.dao"));
        var r = new BusinessAreaResolver().resolve(classes, roles, "ArchMind");
        assertEquals("single", r.strategy());
        assertEquals("main", r.classToArea().get("com.example.archmind.service.impl.S"));
        assertEquals("infrastructure", r.classToArea().get("com.example.archmind.dao.D"));
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
    void firstSegmentDecidesBusinessKey() {
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

    @Test
    void layeredProjectFallsBackToControllerAreas() {
        var classes = List.of(
                cls("x.controller.AgentController", "x.controller"),
                cls("x.controller.DomainMapController", "x.controller"),
                cls("x.service.agent.AgentGatewayService", "x.service.agent"),
                cls("x.service.domain.DomainQueryService", "x.service.domain"),
                cls("x.service.impl.SharedService", "x.service.impl"),
                cls("x.dao.GraphDao", "x.dao"));
        var roles = Map.of(
                "x.controller.AgentController",
                new Classified("x.controller.AgentController", Role.ENTRY, "annotation"),
                "x.controller.DomainMapController",
                new Classified("x.controller.DomainMapController", Role.ENTRY, "annotation"),
                "x.service.agent.AgentGatewayService",
                new Classified("x.service.agent.AgentGatewayService", Role.BUSINESS, "package"),
                "x.service.domain.DomainQueryService",
                new Classified("x.service.domain.DomainQueryService", Role.BUSINESS, "package"),
                "x.service.impl.SharedService",
                new Classified("x.service.impl.SharedService", Role.BUSINESS, "package"),
                "x.dao.GraphDao",
                new Classified("x.dao.GraphDao", Role.INFRASTRUCTURE, "package"));
        var endpoints = List.of(
                endpoint("GET /agent", "x.controller.AgentController#run()"),
                endpoint("GET /map", "x.controller.DomainMapController#map()"));
        var calls = List.of(
                call("x.controller.AgentController#run()", "x.service.agent.AgentGatewayService#go()"),
                call("x.service.agent.AgentGatewayService#go()", "x.service.impl.SharedService#x()"),
                call("x.controller.DomainMapController#map()", "x.service.domain.DomainQueryService#q()"),
                call("x.service.domain.DomainQueryService#q()", "x.service.impl.SharedService#x()"));

        var r = new BusinessAreaResolver().resolve(classes, roles, "ArchMind", endpoints, calls);

        assertEquals("controller-modules", r.strategy());
        assertEquals("agent", r.classToArea().get("x.controller.AgentController"));
        assertEquals("domain-map", r.classToArea().get("x.controller.DomainMapController"));
        assertEquals("agent", r.classToArea().get("x.service.agent.AgentGatewayService"));
        assertEquals("domain-map", r.classToArea().get("x.service.domain.DomainQueryService"));
        // 两个领域各有一个入口到达，并列 -> 留基础设施
        assertEquals("infrastructure", r.classToArea().get("x.service.impl.SharedService"));
        assertEquals("infrastructure", r.classToArea().get("x.dao.GraphDao"));
        assertEquals(List.of("agent", "domain-map"), r.areas().stream()
                .filter(a -> a.kind().equals("business")).map(BusinessArea::uid).toList());
    }

    @Test
    void moduleProjectKeepsPackageStrategyWhenEntriesAreInModules() {
        var classes = List.of(
                cls("com.hmall.item.controller.ItemController", "com.hmall.item.controller"),
                cls("com.hmall.item.service.ItemService", "com.hmall.item.service"),
                cls("com.hmall.user.controller.UserController", "com.hmall.user.controller"));
        var roles = Map.of(
                "com.hmall.item.controller.ItemController",
                new Classified("com.hmall.item.controller.ItemController", Role.ENTRY, "annotation"),
                "com.hmall.user.controller.UserController",
                new Classified("com.hmall.user.controller.UserController", Role.ENTRY, "annotation"));
        var endpoints = List.of(
                endpoint("GET /items", "com.hmall.item.controller.ItemController#list()"),
                endpoint("GET /users", "com.hmall.user.controller.UserController#list()"));

        var r = new BusinessAreaResolver().resolve(classes, roles, "hmall", endpoints, List.of());

        assertEquals("business-modules", r.strategy());
        assertEquals("item", r.classToArea().get("com.hmall.item.controller.ItemController"));
        assertEquals("user", r.classToArea().get("com.hmall.user.controller.UserController"));
    }

    @Test
    void packageKeysWithoutEntryAreasFallBackToControllers() {
        var classes = List.of(
                cls("com.acme.backend.controller.OrderController", "com.acme.backend.controller"),
                cls("com.acme.backend.cart.CartService", "com.acme.backend.cart"));
        var roles = Map.of(
                "com.acme.backend.controller.OrderController",
                new Classified("com.acme.backend.controller.OrderController", Role.ENTRY, "annotation"),
                "com.acme.backend.cart.CartService",
                new Classified("com.acme.backend.cart.CartService", Role.BUSINESS, "package"));
        var endpoints = List.of(endpoint("GET /orders",
                "com.acme.backend.controller.OrderController#list()"));

        var r = new BusinessAreaResolver().resolve(classes, roles, "acme", endpoints, List.of());

        assertEquals("controller-modules", r.strategy());
        assertEquals("order", r.classToArea().get("com.acme.backend.controller.OrderController"));
        assertEquals("infrastructure", r.classToArea().get("com.acme.backend.cart.CartService"));
    }

    @Test
    void controllerAreasTraverseThroughInfrastructureButDoNotOwnIt() {
        var classes = List.of(
                cls("x.controller.AgentController", "x.controller"),
                cls("x.common.CacheUtil", "x.common"),
                cls("x.service.agent.AgentService", "x.service.agent"));
        var roles = Map.of(
                "x.controller.AgentController",
                new Classified("x.controller.AgentController", Role.ENTRY, "annotation"),
                "x.common.CacheUtil",
                new Classified("x.common.CacheUtil", Role.INFRASTRUCTURE, "package"),
                "x.service.agent.AgentService",
                new Classified("x.service.agent.AgentService", Role.BUSINESS, "package"));
        var endpoints = List.of(endpoint("GET /agent", "x.controller.AgentController#run()"));
        var calls = List.of(
                call("x.controller.AgentController#run()", "x.common.CacheUtil#get()"),
                call("x.common.CacheUtil#get()", "x.service.agent.AgentService#go()"),
                call("x.service.agent.AgentService#go()", "x.common.CacheUtil#get()"));

        var r = new BusinessAreaResolver().resolve(classes, roles, "demo", endpoints, calls);

        assertEquals("controller-modules", r.strategy());
        assertEquals("agent", r.classToArea().get("x.service.agent.AgentService"));
        assertEquals("infrastructure", r.classToArea().get("x.common.CacheUtil"));
    }

    @Test
    void controllerKeyStripsSuffixAndProtectsReservedNames() {
        assertEquals("mcp-pat", BusinessAreaResolver.controllerKey("McpPatController"));
        assertEquals("domain-map", BusinessAreaResolver.controllerKey("DomainMapController"));
        assertEquals("x", BusinessAreaResolver.controllerKey("XResource"));
        assertEquals("y", BusinessAreaResolver.controllerKey("YAction"));
        assertEquals("main-module", BusinessAreaResolver.controllerKey("MainController"));
        assertEquals("infrastructure-module", BusinessAreaResolver.controllerKey("InfrastructureController"));
        assertNull(BusinessAreaResolver.controllerKey("Controller"));
        assertNull(BusinessAreaResolver.controllerKey("InternalMcpControllerTest"));
    }

    private static ClassSnapshot cls(String uid, String packageName) {
        String simpleName = uid.substring(uid.lastIndexOf('.') + 1);
        return new ClassSnapshot(uid, simpleName, packageName, List.of(), List.of(), "");
    }

    private static GraphSnapshot.EndpointSnapshot endpoint(String uid, String handlerUid) {
        return new GraphSnapshot.EndpointSnapshot(uid, "http", handlerUid);
    }

    private static GraphSnapshot.CallEdgeSnapshot call(String from, String to) {
        return new GraphSnapshot.CallEdgeSnapshot(from, to);
    }
}
