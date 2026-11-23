package com.example.archmind.service.ast;

import com.example.archmind.model.ast.ParsedEndpoint;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.impl.AstParserServiceImpl;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * EndpointResolver 单测：用真实 AstParserServiceImpl 解析内联源码，
 * 顺带验证 Task 1 的注解抽取，再断言入口拼装规则。
 */
class EndpointResolverTest {

    private final EndpointResolver resolver = new EndpointResolver();

    private ParsedProject project(String src) {
        return new AstParserServiceImpl().parse(List.of(
                new SourceFileView(1L, "Demo.java", "package demo;\n" + src)));
    }

    @Test
    void composesClassPrefixWithMethodPath() {
        ParsedProject p = project(
                "@RestController @RequestMapping(\"/orders\") class OrderController {"
                        + " @PostMapping(\"/{id}/cancel\") void cancel() {}"
                        + " @GetMapping void list() {} }");
        List<ParsedEndpoint> eps = resolver.resolve(p);
        assertEquals(List.of("POST /orders/{id}/cancel", "GET /orders/list"),
                eps.stream().map(ParsedEndpoint::uid).toList());
        assertEquals(List.of("POST", "GET"), eps.stream().map(ParsedEndpoint::httpMethod).toList());
        assertFalse(eps.get(0).inferred());
        assertTrue(eps.get(1).inferred());
        assertEquals("/orders/list", eps.get(1).path());
        assertEquals("demo.OrderController#list()", eps.get(1).handlerUid());
        assertEquals("http", eps.get(0).kind());
    }

    @Test
    void expandsMultiplePathsIntoMultipleEndpoints() {
        ParsedProject p = project(
                "@RestController @RequestMapping({\"/a\", \"/b\"}) class C {"
                        + " @GetMapping({\"/x\", \"/y\"}) void list() {} }");
        List<String> uids = resolver.resolve(p).stream().map(ParsedEndpoint::uid).toList();
        assertEquals(List.of("GET /a/x", "GET /a/y", "GET /b/x", "GET /b/y"), uids.stream().sorted().toList());
    }

    @Test
    void mapsScheduledToNonHttpEndpoint() {
        ParsedProject p = project("class Job { @Scheduled(\"0 0 * * *\") void run() {} }");
        ParsedEndpoint ep = resolver.resolve(p).get(0);
        assertEquals("scheduled", ep.kind());
        assertEquals("SCHEDULED " + ep.handlerUid(), ep.uid());
        assertEquals("demo.Job#run()", ep.handlerUid());
    }

    @Test
    void normalizesDoubleSlashAndTrailingSlash() {
        ParsedProject p = project(
                "@RequestMapping(\"/a/\") class C { @GetMapping(\"/\") void root() {} }"
                        + "@RequestMapping(\"//b//\") class D { @GetMapping(\"//c//\") void x() {} }");
        List<String> uids = resolver.resolve(p).stream().map(ParsedEndpoint::uid).toList();
        assertEquals(List.of("GET /a", "GET /b/c"), uids.stream().sorted().toList());
    }

    @Test
    void methodOnlyPrefixKeepsLeadingSlash() {
        ParsedProject p = project("class C { @GetMapping(\"list\") void x() {} }");
        ParsedEndpoint ep = resolver.resolve(p).get(0);
        assertEquals("GET /list", ep.uid());
        assertEquals("/list", ep.path());
        assertFalse(ep.inferred());
    }

    @Test
    void methodLevelRequestMappingBecomesAny() {
        ParsedProject p = project(
                "@RequestMapping(\"/api\") class C { @RequestMapping(\"/x\") void x() {} }");
        ParsedEndpoint ep = resolver.resolve(p).get(0);
        assertEquals("ANY /api/x", ep.uid());
        assertEquals("ANY", ep.httpMethod());
    }

    @Test
    void mapsMqAndEventAnnotations() {
        ParsedProject p = project("class L {"
                + " @RabbitListener(queues = \"q\") void onMsg() {}"
                + " @KafkaListener(topics = \"t\") void onKafka() {}"
                + " @EventListener void onEvent(Object e) {} }");
        List<ParsedEndpoint> eps = resolver.resolve(p);
        assertEquals(List.of("mq", "mq", "event"), eps.stream().map(ParsedEndpoint::kind).toList());
        assertEquals("MQ " + eps.get(0).handlerUid(), eps.get(0).uid());
        assertEquals("EVENT " + eps.get(2).handlerUid(), eps.get(2).uid());
    }

    @Test
    void mapsRunnerImplementationToManual() {
        ParsedProject p = project(
                "class B implements CommandLineRunner { public void run(String... args) {} }");
        ParsedEndpoint ep = resolver.resolve(p).get(0);
        assertEquals("manual", ep.kind());
        assertEquals("MANUAL " + ep.handlerUid(), ep.uid());
    }

    @Test
    void httpWinsOverScheduledOnSameMethod() {
        ParsedProject p = project(
                "@RequestMapping(\"/j\") class C {"
                        + " @GetMapping(\"/x\") @Scheduled(\"0 * * * *\") void both() {} }");
        List<ParsedEndpoint> eps = resolver.resolve(p);
        assertEquals(1, eps.size());
        assertEquals("GET /j/x", eps.get(0).uid());
        assertEquals("http", eps.get(0).kind());
    }
}
