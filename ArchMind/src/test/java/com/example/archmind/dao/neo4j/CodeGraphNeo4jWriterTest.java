package com.example.archmind.dao.neo4j;

import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.impl.AstParserServiceImpl;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * CodeGraphNeo4jWriter 的行构造单测：不需要真实 Neo4j 连接，
 * 只验证写入用的行数据形状（注解名、入口 uid）。
 */
class CodeGraphNeo4jWriterTest {

    private final AstParserServiceImpl parser = new AstParserServiceImpl();

    @Test
    void classRowsCarryAnnotationNamesAndEndpointRowsAreShaped() {
        CodeGraphNeo4jWriter writer = new CodeGraphNeo4jWriter(null);
        ParsedProject p = parse("@RestController class C { @GetMapping(\"/x\") void list() {} }");

        assertEquals(List.of("RestController"), writer.classRows(1L, p).get(0).get("annotationNames"));
        assertEquals("GET /x", writer.endpointRows(1L, p).get(0).get("uid"));
    }

    private ParsedProject parse(String src) {
        return parser.parse(List.of(new SourceFileView(1L, "Demo.java", src)));
    }
}
