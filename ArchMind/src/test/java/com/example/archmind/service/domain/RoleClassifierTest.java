package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class RoleClassifierTest {

    @Test
    void prefersAnnotationOverPackageAndName() {
        var classes = List.of(
                cls("a.controller.OrderController", "a.controller", List.of("RestController"), List.of(), ""),
                cls("a.common.RedisUtil", "a.common", List.of(), List.of(), ""),
                cls("a.dto.Order", "a.dto", List.of(), List.of(), ""),
                cls("a.weird.Thing", "a.weird", List.of(), List.of(), "")
        );
        var r = new RoleClassifier().classify(classes);
        assertEquals(Role.ENTRY, r.get("a.controller.OrderController").role());
        assertEquals("annotation", r.get("a.controller.OrderController").basis());
        assertEquals(Role.INFRASTRUCTURE, r.get("a.common.RedisUtil").role());
        assertEquals(Role.DOMAIN_MODEL, r.get("a.dto.Order").role());
        assertEquals(Role.OTHER, r.get("a.weird.Thing").role());
    }

    @Test
    void usesSupertypeBeforePackage() {
        var r = new RoleClassifier().classify(List.of(
                cls("a.thing.Custom", "a.thing", List.of(), List.of("BaseMapper"), "")));
        assertEquals(Role.INFRASTRUCTURE, r.get("a.thing.Custom").role());
        assertEquals("supertype", r.get("a.thing.Custom").basis());
    }

    @Test
    void packageMajorityOnlyWhenSampleIsLargeEnough() {
        var enough = new RoleClassifier().classify(List.of(
                cls("a.x.AUserService", "a.x", List.of(), List.of(), ""),
                cls("a.x.BUserService", "a.x", List.of(), List.of(), ""),
                cls("a.x.CUserService", "a.x", List.of(), List.of(), ""),
                cls("a.x.Mystery", "a.x", List.of(), List.of(), "")));
        assertEquals(Role.BUSINESS, enough.get("a.x.Mystery").role());
        assertEquals("package-majority", enough.get("a.x.Mystery").basis());

        var tooFew = new RoleClassifier().classify(List.of(
                cls("b.y.AUserService", "b.y", List.of(), List.of(), ""),
                cls("b.y.Mystery", "b.y", List.of(), List.of(), "")));
        assertEquals(Role.OTHER, tooFew.get("b.y.Mystery").role());
    }

    @Test
    void classifiesByNameSuffixWhenPackageDoesNotMatch() {
        var r = new RoleClassifier().classify(List.of(
                cls("a.thing.OrderMapper", "a.thing", List.of(), List.of(), "")));
        assertEquals(Role.INFRASTRUCTURE, r.get("a.thing.OrderMapper").role());
        assertEquals("name", r.get("a.thing.OrderMapper").basis());
    }

    @Test
    void returnsClassesOrderedByUid() {
        var r = new RoleClassifier().classify(List.of(
                cls("z.Zed", "z", List.of(), List.of(), ""),
                cls("a.Aye", "a", List.of(), List.of(), "")));
        assertEquals(List.of("a.Aye", "z.Zed"), List.copyOf(((Map<String, Classified>) r).keySet()));
    }

    @Test
    void packageSegmentUsesMostSpecificSegment() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.acme.common.controller.UserHandler", "com.acme.common.controller",
                        List.of(), List.of(), "")));
        assertEquals(Role.ENTRY, r.get("com.acme.common.controller.UserHandler").role());
        assertEquals("package", r.get("com.acme.common.controller.UserHandler").basis());
    }

    @Test
    void treatsUtilsPluralAsInfrastructure() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.hmall.utils.JwtTool", "com.hmall.utils", List.of("Component"), List.of(), "")));
        assertEquals(Role.INFRASTRUCTURE, r.get("com.hmall.utils.JwtTool").role());
        assertEquals("package", r.get("com.hmall.utils.JwtTool").basis());
    }

    @Test
    void treatsEnumsPackageAsDomainModel() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.hmall.enums.PayChannel", "com.hmall.enums", List.of("Getter"), List.of(), "")));
        assertEquals(Role.DOMAIN_MODEL, r.get("com.hmall.enums.PayChannel").role());
        assertEquals("package", r.get("com.hmall.enums.PayChannel").basis());
    }

    @Test
    void gatewayFiltersAreInfrastructureNotExternal() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.hmall.gateway.filters.MyGlobalFilter", "com.hmall.gateway.filters",
                        List.of("Component"), List.of(), "")));
        assertEquals(Role.INFRASTRUCTURE, r.get("com.hmall.gateway.filters.MyGlobalFilter").role());
    }

    @Test
    void listenerPackageIsEntry() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.hmall.trade.listener.PayStatusListener", "com.hmall.trade.listener",
                        List.of("Component"), List.of(), "")));
        assertEquals(Role.ENTRY, r.get("com.hmall.trade.listener.PayStatusListener").role());
        assertEquals("package", r.get("com.hmall.trade.listener.PayStatusListener").basis());
    }

    @Test
    void stillTreatsClientPackagesAsExternal() {
        var r = new RoleClassifier().classify(List.of(
                cls("com.hmall.item.client.ItemClient", "com.hmall.item.client", List.of(), List.of(), ""),
                cls("com.hmall.api.client.FallbackFactory", "com.hmall.api.client", List.of(), List.of(), "")));
        assertEquals(Role.EXTERNAL, r.get("com.hmall.item.client.ItemClient").role());
        assertEquals(Role.EXTERNAL, r.get("com.hmall.api.client.FallbackFactory").role());
    }

    private static ClassSnapshot cls(String uid, String packageName, List<String> annotations,
                                     List<String> interfaces, String superclass) {
        String simpleName = uid.substring(uid.lastIndexOf('.') + 1);
        return new ClassSnapshot(uid, simpleName, packageName, annotations, interfaces, superclass);
    }
}
