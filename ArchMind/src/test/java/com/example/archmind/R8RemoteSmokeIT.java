package com.example.archmind;

import com.example.archmind.dto.response.ArchitectureGroupDto;
import com.example.archmind.dto.response.ArchitectureLayerDto;
import com.example.archmind.dto.response.ArchitectureLayerEdgeDto;
import com.example.archmind.dto.response.ArchitectureLayersResponse;
import com.example.archmind.dto.response.CapabilityDetailResponse;
import com.example.archmind.dto.response.CapabilityDto;
import com.example.archmind.dto.response.DomainMapResponse;
import com.example.archmind.dto.response.MemberDto;
import com.example.archmind.service.domain.ArchitectureQueryService;
import com.example.archmind.service.domain.BusinessAreaResolver;
import com.example.archmind.service.domain.Capability;
import com.example.archmind.service.domain.CapabilityResolver;
import com.example.archmind.service.domain.DomainModel;
import com.example.archmind.service.domain.DomainModelValidator;
import com.example.archmind.service.domain.DomainModelWriter;
import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshotDao;
import com.example.archmind.service.domain.RoleClassifier;
import com.example.archmind.service.domain.ValidationReport;
import com.example.archmind.service.domain.DomainQueryService;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * R8 手动集成冒烟：连 application.yml 里配置的远端 Neo4j，用一个一次性 projectId
 * 走「快照 → role → 领域 → 能力 → 对账 → 写库 → 查询接口」，跑完删干净。
 *
 * <p>类名不以 Test 结尾，`mvn test` 不会执行；必须显式开启：
 * <pre>./mvnw -Dtest=R8RemoteSmokeIT -Dr8.smoke=true test</pre>
 *
 * <p>只读写 {@link #SMOKE_PROJECT_ID}，不触碰其他项目的数据。
 */
@SpringBootTest
class R8RemoteSmokeIT {

    /** 一次性假项目；正常项目 id 远小于这个数。 */
    private static final long SMOKE_PROJECT_ID = 987654321L;

    @Autowired
    private Driver driver;
    @Autowired
    private GraphSnapshotDao snapshotDao;
    @Autowired
    private DomainQueryService domainQueryService;
    @Autowired
    private ArchitectureQueryService architectureQueryService;

    @BeforeEach
    void requireExplicitOptIn() {
        Assumptions.assumeTrue(Boolean.getBoolean("r8.smoke"),
                "需要 -Dr8.smoke=true 才执行远端写入冒烟");
    }

    @Test
    void fullDomainPipelineAgainstRemoteNeo4j() {
        clean();
        try {
            seed();
            runPipeline();
            assertDomainMap();
            assertCapabilityDetail();
            assertArchitectureLayers();
        } finally {
            clean();
        }
    }

    private void runPipeline() {
        GraphSnapshot snapshot = snapshotDao.load(SMOKE_PROJECT_ID);
        assertEquals(6, snapshot.classes().size(), "快照应读到 6 个类");

        Map<String, RoleClassifier.Classified> roles = new RoleClassifier().classify(snapshot.classes());
        BusinessAreaResolver.AreaResult areas =
                new BusinessAreaResolver().resolve(snapshot.classes(), roles, "smoke");
        assertEquals("business-modules", areas.strategy());
        assertEquals("order", areas.classToArea().get("demo.order.controller.OrderController"));

        Capability.CapabilityResult capabilities = new CapabilityResolver().resolve(
                snapshot.endpoints(), areas.classToArea(), roles, snapshot.calls());
        assertEquals(2, capabilities.capabilities().size());
        assertEquals(1, capabilities.areaCalls().size(), "订单调商品应记一条跨领域边");

        DomainModel model = new DomainModel(areas.areas(), capabilities.capabilities(),
                capabilities.areaCalls(), areas.classToArea(), roles);
        ValidationReport report = new DomainModelValidator().validate(model, snapshot.classes().size());
        assertTrue(report.ok(), "对账失败: " + report.failures());

        new DomainModelWriter(driver).write(SMOKE_PROJECT_ID, model);
    }

    private void assertDomainMap() {
        DomainMapResponse map = domainQueryService.domainMap(SMOKE_PROJECT_ID);
        List<String> areaUids = map.areas().stream().map(a -> a.uid()).sorted().toList();
        assertEquals(List.of("infrastructure", "item", "order"), areaUids);

        var orderArea = map.areas().stream()
                .filter(a -> a.uid().equals("order")).findFirst().orElseThrow();
        // 两个 Mapper 都是基础设施角色，归 infrastructure 领域，不算进业务领域类数
        assertEquals(2, orderArea.classCount(), "订单领域只含 Controller / Service 两个类");
        assertEquals(2, map.areas().stream()
                .filter(a -> a.uid().equals("infrastructure")).findFirst().orElseThrow().classCount());
        assertEquals(3, orderArea.capabilities().get(0).memberCount());

        List<String> orderCaps = orderArea.capabilities().stream()
                .map(CapabilityDto::uid).toList();
        assertEquals(List.of("POST /orders"), orderCaps);
    }

    private void assertCapabilityDetail() {
        CapabilityDetailResponse detail = domainQueryService.capability(SMOKE_PROJECT_ID, "POST /orders");
        List<String> members = detail.members().stream().map(MemberDto::uid).toList();
        assertTrue(members.contains("demo.order.controller.OrderController"));
        assertTrue(members.contains("demo.order.service.OrderService"));
        assertTrue(members.contains("demo.order.mapper.OrderMapper"), "基础设施类必须算成员");
        assertFalse(members.contains("demo.item.service.ItemService"), "跨领域类不能进本能力");

        List<String> edges = detail.edges().stream()
                .map(e -> e.fromUid() + "->" + e.toUid()).toList();
        assertTrue(edges.contains("demo.order.controller.OrderController->demo.order.service.OrderService"),
                "入口到业务类的调用边必须返回");
        assertTrue(edges.contains("demo.order.service.OrderService->demo.order.mapper.OrderMapper"),
                "业务到基础设施的调用边必须返回");

        assertEquals("item", detail.areaCalls().get(0).toAreaUid());
        assertTrue(detail.areaCalls().get(0).fromClassUids().contains("demo.order.service.OrderService"),
                "跨领域调用要记下来源类");
    }

    private void assertArchitectureLayers() {
        ArchitectureLayersResponse resp = architectureQueryService.architecture(SMOKE_PROJECT_ID);

        assertEquals(6, resp.classCount());
        assertEquals(List.of("entry", "business", "domain-model",
                        "infrastructure", "external", "other"),
                resp.layers().stream().map(ArchitectureLayerDto::key).toList());
        assertEquals(2, layer(resp, "entry").classCount());
        assertEquals(2, layer(resp, "business").classCount());
        assertEquals(2, layer(resp, "infrastructure").classCount());
        assertEquals(0, layer(resp, "domain-model").classCount());
        assertEquals(0, layer(resp, "other").classCount());

        assertEquals(List.of("order", "item"),
                layer(resp, "business").groups().stream().map(ArchitectureGroupDto::key).toList());
        assertEquals("mapper", layer(resp, "infrastructure").groups().get(0).key());

        // OrderService → ItemService 是业务层内部调用，不能算跨层边
        assertEquals(2, resp.edges().size());
        assertEquals(2, edgeCount(resp, "entry", "business"));
        assertEquals(1, edgeCount(resp, "business", "infrastructure"));
        assertTrue(resp.edges().stream()
                        .flatMap(e -> e.samples().stream())
                        .anyMatch(s -> s.fromUid().equals("demo.order.controller.OrderController")),
                "入口 → 业务的样例必须包含真实来源类");
    }

    private static ArchitectureLayerDto layer(ArchitectureLayersResponse resp, String key) {
        return resp.layers().stream().filter(l -> l.key().equals(key)).findFirst().orElseThrow();
    }

    private static int edgeCount(ArchitectureLayersResponse resp, String from, String to) {
        return resp.edges().stream()
                .filter(e -> e.fromLayer().equals(from) && e.toLayer().equals(to))
                .mapToInt(ArchitectureLayerEdgeDto::count).findFirst().orElse(0);
    }

    private void seed() {
        try (Session session = driver.session()) {
            session.run("""
                    UNWIND $rows AS row
                    CREATE (c:Class:CodeNode {projectId:$pid, uid:row.uid, name:row.name,
                        packageName:row.pkg, annotationNames:row.anns})
                    """, Map.of("pid", SMOKE_PROJECT_ID, "rows", List.of(
                    Map.of("uid", "demo.order.controller.OrderController", "name", "OrderController",
                            "pkg", "demo.order.controller", "anns", List.of("RestController")),
                    Map.of("uid", "demo.order.service.OrderService", "name", "OrderService",
                            "pkg", "demo.order.service", "anns", List.of("Service")),
                    Map.of("uid", "demo.order.mapper.OrderMapper", "name", "OrderMapper",
                            "pkg", "demo.order.mapper", "anns", List.of("Mapper")),
                    Map.of("uid", "demo.item.controller.ItemController", "name", "ItemController",
                            "pkg", "demo.item.controller", "anns", List.of("RestController")),
                    Map.of("uid", "demo.item.service.ItemService", "name", "ItemService",
                            "pkg", "demo.item.service", "anns", List.of("Service")),
                    Map.of("uid", "demo.item.mapper.ItemMapper", "name", "ItemMapper",
                            "pkg", "demo.item.mapper", "anns", List.of("Mapper"))
            ))).consume();

            session.run("""
                    UNWIND $rows AS row
                    CREATE (m:Method:CodeNode {projectId:$pid, uid:row.uid, owner:row.owner, name:row.name})
                    """, Map.of("pid", SMOKE_PROJECT_ID, "rows", List.of(
                    Map.of("uid", "demo.order.controller.OrderController#create()",
                            "owner", "demo.order.controller.OrderController", "name", "create"),
                    Map.of("uid", "demo.order.service.OrderService#create()",
                            "owner", "demo.order.service.OrderService", "name", "create"),
                    Map.of("uid", "demo.order.mapper.OrderMapper#insert()",
                            "owner", "demo.order.mapper.OrderMapper", "name", "insert"),
                    Map.of("uid", "demo.item.controller.ItemController#list()",
                            "owner", "demo.item.controller.ItemController", "name", "list"),
                    Map.of("uid", "demo.item.service.ItemService#deduct()",
                            "owner", "demo.item.service.ItemService", "name", "deduct")
            ))).consume();

            session.run("""
                    UNWIND $rows AS row
                    MATCH (a:Method {projectId:$pid, uid:row.from})
                    MATCH (b:Method {projectId:$pid, uid:row.to})
                    CREATE (a)-[:CALLS {count:1}]->(b)
                    """, Map.of("pid", SMOKE_PROJECT_ID, "rows", List.of(
                    Map.of("from", "demo.order.controller.OrderController#create()",
                            "to", "demo.order.service.OrderService#create()"),
                    Map.of("from", "demo.order.service.OrderService#create()",
                            "to", "demo.order.mapper.OrderMapper#insert()"),
                    Map.of("from", "demo.order.service.OrderService#create()",
                            "to", "demo.item.service.ItemService#deduct()"),
                    Map.of("from", "demo.item.controller.ItemController#list()",
                            "to", "demo.item.service.ItemService#deduct()")
            ))).consume();

            session.run("""
                    UNWIND $rows AS row
                    CREATE (e:Endpoint:CodeNode {projectId:$pid, uid:row.uid, kind:'http',
                        httpMethod:row.verb, path:row.path, handlerUid:row.handler, inferred:false})
                    """, Map.of("pid", SMOKE_PROJECT_ID, "rows", List.of(
                    Map.of("uid", "POST /orders", "verb", "POST", "path", "/orders",
                            "handler", "demo.order.controller.OrderController#create()"),
                    Map.of("uid", "GET /items", "verb", "GET", "path", "/items",
                            "handler", "demo.item.controller.ItemController#list()")
            ))).consume();
        }
    }

    private void clean() {
        try (Session session = driver.session()) {
            session.run("MATCH (n:CodeNode {projectId:$pid}) DETACH DELETE n",
                    Map.of("pid", SMOKE_PROJECT_ID)).consume();
        }
    }
}
